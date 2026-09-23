"""提示词优化端点 ``POST /api/prompt-optimize`` 与 ``POST /api/prompt-reverse``。

外部模型调用一律用替身（monkeypatch ``prompt_optimize`` 里的调用函数）：本套件不联网，
也不该因为外部服务抖动而红。skill 脚本本身不参与断言，只用一份占位脚本满足「路径存在」
这一层校验。
"""

import struct
import tempfile
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from image_hub import prompt_optimize
from image_hub.app import app

STUB_DIR = Path(tempfile.mkdtemp(prefix="prompt-optimize-stub-"))
SCRIPT = STUB_DIR / "stub_skill.py"
SCRIPT.write_text("print('{}')\n", encoding="utf-8")


def _png_bytes(size: int = 64) -> bytes:
    """最小合法 PNG（不依赖 Pillow，本套件要能在最简环境跑）。

    ⚠️ 边长不能小于 64：``validate_image_payload`` 会挡掉过小的图（防的是
    「拿 1×1 占位图当参考图」），这里必须给一张它认的图。
    """
    raw = b"".join(b"\x00" + b"\x60\x90\xc0" * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _login(client: TestClient) -> str:
    import re

    page = client.get("/login")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={"username": "admin", "password": "test-password", "csrf": token},
        follow_redirects=False)
    assert response.status_code == 303
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/projects").text).group(1)


@pytest.fixture
def configured():
    """开启两个模式，并在用例结束后**删掉配置文件**。

    配置落在 ``settings.storage_root`` 里，是跨用例的进程级状态：漏给后面的用例
    会让「未配置时应当跳过优化」的那些断言悄悄失效。
    """
    prompt_optimize.save_config(
        enabled=True,
        script=str(SCRIPT),
        args="--prompt-file {prompt_file}",
        api_base_url="",
        api_key="test-key",
        model="text-model",
        timeout=30,
        agent_enabled=True,
        agent_model="vision-model")
    yield prompt_optimize
    prompt_optimize.config_path().unlink(missing_ok=True)


@pytest.fixture
def stub(monkeypatch):
    """替身：记录调用参数并返回固定结果，或按开关抛异常。"""
    calls: list[dict] = []
    state = {"raise_error": False}

    def _fake(prompt: str, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if state["raise_error"]:
            raise RuntimeError("上游模型不可用")
        return "改写后的提示词", "诊断：画面缺少接触阴影"

    monkeypatch.setattr(prompt_optimize, "optimize_prompt", _fake)
    return calls, state


def _post(client: TestClient, token: str, **data):
    files = data.pop("files", None)
    return client.post(
        "/api/prompt-optimize",
        data=data,
        files=files,
        headers={"X-CSRF-Token": token})


# ---------------------------------------------------------------------------


def test_failure_is_reported_to_the_user(configured, stub):
    _calls, state = stub
    state["raise_error"] = True
    with TestClient(app) as client:
        token = _login(client)
        response = _post(client, token, prompt="一只白色陶瓷马克杯", mode="optimize")

    assert response.status_code == 422
    assert "提示词优化失败" in response.json()["detail"]


def test_agent_mode_without_reference_images_is_rejected(configured, stub):
    """agent 模式要读图，没有图就不该跑。"""
    calls, _ = stub
    with TestClient(app) as client:
        token = _login(client)
        response = _post(client, token, prompt="一只白色陶瓷马克杯", mode="agent")

    assert response.status_code == 422
    assert "参考图" in response.json()["detail"]
    assert not calls


def test_disabled_mode_returns_service_unavailable(configured, stub):
    """模式级开关：文本开着的同一个配置里，agent 可以单独关掉。"""
    calls, _ = stub
    prompt_optimize.save_config(
        enabled=True,
        script=str(SCRIPT),
        args="--prompt-file {prompt_file}",
        api_base_url="",
        api_key="test-key",
        model="text-model",
        timeout=30,
        agent_enabled=False)
    with TestClient(app) as client:
        token = _login(client)
        response = _post(
            client,
            token,
            prompt="一只白色陶瓷马克杯",
            mode="agent",
            files={"files": ("ref.png", _png_bytes(), "image/png")})

    assert response.status_code == 503
    assert not calls


def test_csrf_is_required(configured, stub):
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/prompt-optimize", data={"prompt": "x", "mode": "optimize"}
        )
    assert response.status_code in (400, 403)


@pytest.fixture
def reverse_stub(monkeypatch):
    """替身：记录收到几张图，可切换成抛错。"""
    calls: list[list[str]] = []
    state = {"raise_error": False}

    def _fake(*, image_paths, config=None):
        calls.append(list(image_paths))
        if state["raise_error"]:
            raise RuntimeError("视觉模型不可用")
        return "a matte white mug on a walnut table, soft window light", {"total_tokens": 120}

    monkeypatch.setattr(prompt_optimize, "reverse_prompt", _fake)
    return calls, state


def _post_reverse(client: TestClient, token: str, **data):
    files = data.pop("files", None)
    return client.post(
        "/api/prompt-reverse",
        data=data,
        files=files,
        headers={"X-CSRF-Token": token})


def test_reverse_without_an_image_is_rejected(configured, reverse_stub):
    calls, _ = reverse_stub
    with TestClient(app) as client:
        token = _login(client)
        response = _post_reverse(client, token)

    assert response.status_code == 422
    assert not calls, "没有图就不该调用外部模型"


def test_reverse_failure_is_reported(configured, reverse_stub):
    _calls, state = reverse_stub
    state["raise_error"] = True
    with TestClient(app) as client:
        token = _login(client)
        response = _post_reverse(
            client, token, files={"file": ("ref.png", _png_bytes(), "image/png")}
        )

    assert response.status_code == 422
    assert "反推提示词失败" in response.json()["detail"]


def test_reverse_needs_the_vision_model_switched_on(configured, reverse_stub):
    """反推用的是 **agent 那套视觉模型**，所以共用 agent 的开关。"""
    prompt_optimize.save_config(
        enabled=True,
        script=str(SCRIPT),
        args="--prompt-file {prompt_file}",
        api_base_url="",
        api_key="test-key",
        model="text-model",
        timeout=30,
        agent_enabled=False)
    with TestClient(app) as client:
        token = _login(client)
        response = _post_reverse(
            client, token, files={"file": ("ref.png", _png_bytes(), "image/png")}
        )
    assert response.status_code == 503
