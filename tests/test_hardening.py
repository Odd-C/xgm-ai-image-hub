"""本轮安全加固的回归测试。

每个用例对应一个已复现的缺陷，用来把修复钉住。分组编号与评审报告一致：

* P1 启动时必须拒绝占位密钥/口令（不再只在 env == production 时检查）
* P2 普通管理员不得重置总管理员的口令（提权）
* P3 上游回传的图片地址必须过公网/协议校验（SSRF）
* P4 结果下载必须有体积上限
* P6 普通管理员只能看见自己团队的账号
* P7 重试守卫必须与前端 can_retry 一致
* P9 降级管理员必须迁移其名下子账号；退款不得因归属失效而抛异常
* P11 全仓库审计（2026-09-22）：API 结果下载的地址守卫与体积上限、写端点不得跨越
  租户边界、artifact 不得对管理员无条件放行、参考图落盘原子化、skill JSON 取最后一个
"""

import asyncio
import io
import re
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from image_hub import providers as provider_module
from image_hub import storage
from image_hub.app import app
from image_hub.auth import hash_password
from image_hub.config import (
    PLACEHOLDER_ADMIN_PASSWORDS,
    PLACEHOLDER_SESSION_SECRETS,
    Settings,
    assert_bootstrap_password_is_usable,
    assert_session_secret_is_strong,
    settings,
)
from image_hub.db import SessionLocal
from image_hub.models import Generation, Project, User, utcnow
from image_hub.providers import ProviderConfigError, _assert_download_url_allowed

ADMIN = "admin"
PASSWORD = "test-password"


# ---------------------------------------------------------------------------


def login(client: TestClient, username: str = ADMIN) -> str:
    page = client.get("/login")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={"username": username, "password": PASSWORD, "csrf": token},
        follow_redirects=False)
    assert response.status_code == 303, response.text
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/projects").text).group(1)


def make_user(
    username: str, role: str = "user", *, owner_id: str = "", credits: int = 0
) -> str:
    """直接建库内账号。``must_change_password=0`` 是为了能直接登录做 HTTP 断言。"""
    with SessionLocal() as session:
        user = User(
            username=username,
            display_name=username,
            password_hash=hash_password(PASSWORD),
            role=role,
            must_change_password=0)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id


def root_id() -> str:
    with SessionLocal() as session:
        return session.scalar(select(User.id).where(User.username == ADMIN))


def make_project(user_id: str, name: str) -> str:
    with SessionLocal() as session:
        project = Project(user_id=user_id, name=name)
        session.add(project)
        session.commit()
        session.refresh(project)
        return project.id


def seed_generation(
    user_id: str,
    *,
    project_id: str,
    prompt: str = "回归测试提示词",
    status: str = "failed",
    provider: str = "api",
    external_project_id: str = "",
    external_task_id: str = "",
    credits_charged: int = 0,
    credits_state: str = "none",
    parameters_json: str = "{}") -> str:
    """直接落一条任务行，绕开提交接口 —— 避免和并发配额、模板渲染纠缠。"""
    with SessionLocal() as session:
        generation = Generation(
            user_id=user_id,
            project_id=project_id,
            idempotency_key=f"seed-{uuid.uuid4().hex}",
            original_prompt=prompt,
            provider=provider,
            model_id="test-model",
            model_label="Test Model",
            parameters_json=parameters_json,
            status=status,
            external_project_id=external_project_id,
            external_task_id=external_task_id,
            credits_charged=credits_charged,
            credits_state=credits_state)
        session.add(generation)
        session.commit()
        session.refresh(generation)
        return generation.id


# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret", sorted(PLACEHOLDER_SESSION_SECRETS) + ["", "   "])
def test_placeholder_session_secret_is_refused(secret):
    with pytest.raises(RuntimeError):
        assert_session_secret_is_strong(secret)


def test_short_session_secret_is_refused():
    assert_session_secret_is_strong("s" * 32)
    with pytest.raises(RuntimeError):
        assert_session_secret_is_strong("s" * 31)


@pytest.mark.parametrize("password", sorted(PLACEHOLDER_ADMIN_PASSWORDS) + ["", "  "])
def test_placeholder_bootstrap_password_is_refused(password):
    with pytest.raises(RuntimeError):
        assert_bootstrap_password_is_usable(password)


@pytest.mark.parametrize("env", ["development", "test", "production"])
def test_startup_guard_does_not_depend_on_env(env, tmp_path):
    """原缺陷：ensure_directories 只在 env == production 时检查，
    于是以 development 跑起来的实例，Session 签名密钥仍是公开已知值。"""
    configured = Settings(
        _env_file=None,
        env=env,
        session_secret="development-only-change-me",
        storage_root=tmp_path / "storage")
    with pytest.raises(RuntimeError):
        configured.ensure_directories()


def test_startup_guard_accepts_a_strong_secret_and_creates_directories(tmp_path):
    configured = Settings(
        _env_file=None,
        session_secret="a-strong-enough-session-secret-value",
        storage_root=tmp_path / "storage",
        database_url=f"sqlite:///{tmp_path / 'guarded.db'}")
    configured.ensure_directories()
    assert (tmp_path / "storage").is_dir()
    assert (tmp_path / "guarded.db").parent.is_dir()


def test_relative_sqlite_url_is_anchored_to_the_repo_root():
    """原缺陷：只锚定了 storage_root，sqlite 文件仍跟着进程 CWD 走 ——
    换个目录启动就会静默打开另一个空库。"""
    configured = Settings(_env_file=None, database_url="sqlite:///./data/anchored.db")
    assert "./" not in configured.database_url
    path = configured.sqlite_file_path()
    assert path is not None
    assert path.is_absolute()
    assert path.name == "anchored.db"


# ---------------------------------------------------------------------------


def test_plain_admin_can_reset_its_own_member_password():
    owner = make_user("p2-owner-admin", role="admin")
    member = make_user("p2-own-member", owner_id=owner)
    with TestClient(app) as client:
        token = login(client, "p2-owner-admin")
        response = client.post(
            f"/admin/users/{member}/reset-password",
            data={
                "temporary_password": "new-temp-123",
                "confirm_password": "new-temp-123",
                "csrf": token,
            },
            follow_redirects=False)
        assert response.status_code == 303

    with SessionLocal() as session:
        assert session.get(User, member).must_change_password == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://93.184.216.34/a.png",  # 非 HTTPS
        "ftp://93.184.216.34/a.png",  # 非 HTTP
        "https://127.0.0.1/a.png",  # 本机
        "https://127.0.0.1:5190/admin",  # 本机 + 后台端口
        "https://169.254.169.254/latest/meta-data/",  # 云元数据
        "https://10.0.0.1/a.png",  # 私网
        "https://192.168.1.10/a.png",  # 私网
        "https://[::1]/a.png",  # IPv6 本机
        "https://0.0.0.0/a.png",  # 未指定地址
        "https:///no-host.png",  # 缺主机名
    ])
def test_result_download_urls_must_be_public_https(url):
    with pytest.raises(ProviderConfigError):
        _assert_download_url_allowed(url)


def test_allowlist_can_authorize_an_explicit_private_result_host(monkeypatch):
    monkeypatch.setattr(settings, "api_private_network_allowlist", "192.168.0.0/16")
    _assert_download_url_allowed("https://192.168.1.10/result.png")


# ---------------------------------------------------------------------------


class _FakeResponse:
    """够用的响应替身：只实现 _fetch_urls 用到的那几个成员。"""

    def __init__(self, chunks, content_length: str = ""):
        self._chunks = chunks
        self.headers = {"content-length": content_length}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield from self._chunks


class _FakeClient:
    """假客户端，但仍然走真实的地址校验钩子 —— 让「被拒地址」这条路径也被覆盖。

    钩子由 ``stream()`` 触发：产品代码用 ``httpx.Client`` 而不是 ``httpx.stream()``，
    因为后者不接受 ``event_hooks``，传进去只会 TypeError。
    """

    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url):
        provider_module._guard_download_request(httpx.Request(method, url))
        return self._response


def _use_fake_client(monkeypatch, response):
    monkeypatch.setattr(provider_module, "_download_client", lambda: _FakeClient(response))


def test_download_client_installs_the_address_guard():
    """钩子必须挂在 Client 上 —— httpx.stream() 会让你直接 TypeError。"""
    client = provider_module._download_client()
    try:
        hooks = client.event_hooks["request"]
        assert hooks == [provider_module._guard_download_request]
        with pytest.raises(ProviderConfigError):
            hooks[0](httpx.Request("GET", "http://127.0.0.1/a.png"))
    finally:
        client.close()


def test_fetch_urls_aborts_and_cleans_up_when_the_stream_exceeds_the_limit(
    tmp_path, monkeypatch
):
    """上游可以无限塞字节，不设上限就是拿服务端磁盘做赌注。"""
    monkeypatch.setattr(settings, "max_artifact_bytes", 1024)
    _use_fake_client(monkeypatch, _FakeResponse([b"x" * 512] * 8))

    files = provider_module._fetch_urls(["https://example.com/a.png"], tmp_path, "result")

    assert files == []
    assert list(tmp_path.iterdir()) == []


def test_fetch_urls_rejects_an_oversized_content_length_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_artifact_bytes", 1024)
    _use_fake_client(monkeypatch, _FakeResponse([b"x"], content_length="99999999"))

    files = provider_module._fetch_urls(["https://example.com/a.png"], tmp_path, "result")

    assert files == []
    assert list(tmp_path.iterdir()) == []


def test_fetch_urls_keeps_a_result_within_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_artifact_bytes", 4096)
    _use_fake_client(monkeypatch, _FakeResponse([b"x" * 100]))

    files = provider_module._fetch_urls(["https://example.com/a.png"], tmp_path, "result")

    assert len(files) == 1
    assert files[0].read_bytes() == b"x" * 100


def test_fetch_urls_skips_a_rejected_address(tmp_path, monkeypatch):
    """被拒绝的地址不能落盘，也不能让整批下载崩掉。"""
    monkeypatch.setattr(settings, "max_artifact_bytes", 4096)
    _use_fake_client(monkeypatch, _FakeResponse([b"x"]))

    files = provider_module._fetch_urls(["https://127.0.0.1/a.png"], tmp_path, "result")

    assert files == []
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------


def test_plain_admin_cannot_read_other_pool_balances():
    """账号表的积分列是泄露点：普通管理员不该读到别的管理员乃至总管理员的池子余额。"""
    make_user("p1-balance-admin", role="admin")
    make_user("p1-rich-admin", role="admin")

    with TestClient(app) as client:
        login(client, "p1-balance-admin")
        page = client.get("/admin/users")

    assert "424242" not in page.text


def test_plain_admin_overview_does_not_leak_other_pools():
    """总览的每个数字都要按可见范围收窄，否则管理员从摘要就能推出别人的规模。"""
    make_user("p1-ov-admin", role="admin")
    make_user("p1-ov-other", role="admin")

    with TestClient(app) as client:
        login(client, "p1-ov-admin")
        page = client.get("/admin")

    assert page.status_code == 200
    assert "8888" not in page.text


# ---------------------------------------------------------------------------


def test_deleting_an_account_requires_explicit_confirmation():
    """按钮就挨着「停用」，没有二次确认极易误点，而删除是不可逆的对外动作。"""
    target = make_user("d1-target", role="admin")
    with TestClient(app) as client:
        token = login(client, ADMIN)
        response = client.post(
            f"/admin/users/{target}/delete",
            data={"csrf": token},  # 故意不带 confirm
            follow_redirects=False)
    assert response.status_code == 422
    with SessionLocal() as session:
        assert session.get(User, target).deleted_at is None


def test_deleted_account_leaves_the_list_and_cannot_log_in():
    target = make_user("d2-target", role="admin")
    with TestClient(app) as client:
        token = login(client, ADMIN)
        response = client.post(
            f"/admin/users/{target}/delete",
            data={"csrf": token, "confirm": "delete"},
            follow_redirects=False)
        assert response.status_code == 303
        assert "d2-target" not in client.get("/admin/users").text

    with TestClient(app) as fresh:
        page = fresh.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        attempt = fresh.post(
            "/login",
            data={"username": "d2-target", "password": PASSWORD, "csrf": csrf},
            follow_redirects=False)
        assert attempt.status_code == 401


def _create_account(client, token: str, username: str, role: str = "user"):
    return client.post(
        "/admin/users",
        data={"username": username, "password": "password123", "role": role, "csrf": token},
        follow_redirects=False)


def test_deleting_an_account_frees_its_username():
    """注销后必须能再用这个名字建号。

    Regression（用户实测反馈"账号无法重名"）：账号是软删（行还在），而 username
    有全局唯一约束且创建时只查"这行是否存在" —— 结果**任何用过的用户名都被永久占住**。
    """
    target = make_user("d4-free", role="admin")
    with TestClient(app) as client:
        token = login(client, ADMIN)
        assert client.post(
            f"/admin/users/{target}/delete",
            data={"csrf": token, "confirm": "delete"},
            follow_redirects=False).status_code == 303
        assert _create_account(client, token, "d4-free").status_code == 303

    with SessionLocal() as session:
        live = session.query(User).filter_by(username="d4-free").one()
        assert live.id != target            # 是一个新账号
        assert live.deleted_at is None
        old = session.get(User, target)
        assert old is not None              # 老行还在（历史归属不变）
        assert old.username.startswith("d4-free#deleted#")
        assert old.deleted_at is not None


def test_a_live_account_still_reserves_its_username():
    make_user("d5-live", role="admin")
    with TestClient(app) as client:
        token = login(client, ADMIN)
        assert _create_account(client, token, "d5-live").status_code == 409


def test_legacy_deleted_account_no_longer_blocks_the_username():
    """注销释放是后加的：老库里已注销的账号还占着名字，创建时要现场释放。"""
    legacy = make_user("d6-legacy", role="admin")
    with SessionLocal() as session:
        row = session.get(User, legacy)
        row.deleted_at = utcnow()
        row.is_active = 0
        session.commit()

    with TestClient(app) as client:
        token = login(client, ADMIN)
        assert _create_account(client, token, "d6-legacy").status_code == 303

    with SessionLocal() as session:
        fresh = session.query(User).filter_by(username="d6-legacy").one()
        assert fresh.id != legacy
        released = session.get(User, legacy)
        assert released.username != "d6-legacy"
        assert released.deleted_at is not None
        assert released.is_active == 0  # 释放名字不改「已注销」这件事


def test_deleting_a_project_needs_confirmation_then_hides_it_but_keeps_the_row():
    owner = make_user("d5-owner", role="admin")
    project_id = make_project(owner, "d5-项目")
    with TestClient(app) as client:
        token = login(client, "d5-owner")

        missing_confirm = client.post(
            f"/projects/{project_id}/delete",
            data={"csrf": token},
            follow_redirects=False)
        assert missing_confirm.status_code == 422
        assert project_id in client.get("/projects").text

        confirmed = client.post(
            f"/projects/{project_id}/delete",
            data={"csrf": token, "confirm": "delete"},
            follow_redirects=False)
        assert confirmed.status_code == 303
        assert project_id not in client.get("/projects").text

    with SessionLocal() as session:
        row = session.get(Project, project_id)
        assert row is not None
        assert row.deleted_at is not None


def _seed_account(username: str, *, must_change_password: int) -> str:
    with SessionLocal() as session:
        user = User(
            username=username,
            display_name=username,
            password_hash=hash_password(PASSWORD),
            role="user",
            must_change_password=must_change_password)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id


def test_a_pending_password_change_no_longer_blocks_the_canvas():
    """初始密码不再是硬门槛：登录直接进项目，而不是被弹到 /account 逼着改密。"""
    _seed_account("fresh-account", must_change_password=1)

    with TestClient(app) as client:
        page = client.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/login",
            data={"username": "fresh-account", "password": PASSWORD, "csrf": csrf},
            follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/projects"

        # 画布本身也进得去（旧实现里 current_user 会对未改密的账号抛 403）
        with SessionLocal() as session:
            owner = session.scalar(select(User).where(User.username == "fresh-account"))
            project_id = session.scalar(
                select(Project.id).where(Project.user_id == owner.id)
            )
        assert project_id is None  # 还没访问过 /projects，所以项目尚未创建
        assert client.get("/projects", follow_redirects=True).status_code == 200


def test_an_account_without_any_project_gets_one_automatically():
    """空列表不该是新账号看到的第一屏 —— 访问项目页时自动建一个未命名项目。"""
    owner_id = _seed_account("fresh-owner", must_change_password=0)
    with SessionLocal() as session:
        before = session.scalar(
            select(func.count()).select_from(Project).where(Project.user_id == owner_id)
        )
    assert before == 0

    with TestClient(app) as client:
        login(client, "fresh-owner")  # 内部会 GET /projects，那一步就触发自动建项目

    with SessionLocal() as session:
        projects = session.scalars(
            select(Project).where(Project.user_id == owner_id)
        ).all()
        assert len(projects) == 1
        assert projects[0].name == "未命名项目"


# ---------------------------------------------------------------------------


def _two_teams(prefix: str) -> tuple[str, str, str, str]:
    """造两个互不相干的租户；返回 (A 管理员, A 子账号, B 管理员, B 子账号)。"""
    a_admin = make_user(f"{prefix}-a", role="admin")
    a_member = make_user(f"{prefix}-a1", owner_id=a_admin)
    b_admin = make_user(f"{prefix}-b", role="admin")
    b_member = make_user(f"{prefix}-b1", owner_id=b_admin)
    return a_admin, a_member, b_admin, b_member


# --- 1. API 结果下载的地址守卫与体积上限 -------------------------------------


def test_api_result_download_goes_through_the_address_guard(tmp_path, monkeypatch):
    """API 渠道取结果图必须与平台渠道受同等约束。

    此前是 ``with httpx.Client(timeout=120) as client: client.get(image["url"])``：
    上游（或被篡改的响应）只要把 ``results[0].url`` 指向
    ``http://169.254.169.254/...``，服务端就替它去打云元数据 —— 构造盲 SSRF。
    这条路径此前**没有任何测试覆盖**（``test_provider_config`` 只测到能力拒绝）。
    """
    monkeypatch.setattr(settings, "max_artifact_bytes", 4096)
    # 假客户端的 stream() 会跑**真实**的地址守卫，所以「被拒地址不落盘」也被覆盖。
    _use_fake_client(monkeypatch, _FakeResponse([b"x" * 32]))
    target = tmp_path / "result.png"

    with pytest.raises(ProviderConfigError):
        provider_module._download_result_image("https://127.0.0.1/a.png", target)

    assert not target.exists()


def test_api_result_download_aborts_when_the_stream_exceeds_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_artifact_bytes", 1024)
    _use_fake_client(monkeypatch, _FakeResponse([b"x" * 512] * 8))

    with pytest.raises(RuntimeError, match="超过大小限制"):
        provider_module._download_result_image("https://example.com/a.png", tmp_path / "r.png")


def test_api_result_download_rejects_an_oversized_content_length(tmp_path, monkeypatch):
    """先看 Content-Length：整包读进内存之后才判，等于拿内存做赌注。"""
    monkeypatch.setattr(settings, "max_artifact_bytes", 1024)
    _use_fake_client(monkeypatch, _FakeResponse([b"x"], content_length="99999999"))

    with pytest.raises(RuntimeError, match="超过大小限制"):
        provider_module._download_result_image("https://example.com/a.png", tmp_path / "r.png")


def test_api_result_download_writes_a_small_result(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_artifact_bytes", 4096)
    _use_fake_client(monkeypatch, _FakeResponse([b"x" * 100]))
    target = tmp_path / "nested" / "result.png"

    provider_module._download_result_image("https://example.com/a.png", target)

    assert target.read_bytes() == b"x" * 100


# --- 2. 写端点不得跨越租户边界 -----------------------------------------------


def test_an_admin_still_manages_its_own_members():
    """正向对照：收了归属这道口之后，管理员对自己子账号的操作必须照旧可用。"""
    _, a_member, _, _ = _two_teams("p11-own")

    with TestClient(app) as client:
        token = login(client, "p11-own-a")
        response = client.post(
            f"/admin/users/{a_member}/toggle",
            data={"csrf": token},
            follow_redirects=False)

    assert response.status_code == 303
    with SessionLocal() as session:
        assert session.get(User, a_member).is_active == 0


def _png_payload() -> bytes:
    # 64×64 是 ``validate_image_payload`` 的最小边长，小了会被判 «不是有效图片»。
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeUpload:
    def __init__(self, payload: bytes):
        self._payload = payload

    async def read(self, _size: int = -1) -> bytes:
        return self._payload


def test_reference_upload_publishes_the_file_atomically(tmp_path, monkeypatch):
    """参考图落盘必须「先写临时名、再 rename 到正式路径」。

    直接写正式路径的话，中途崩/被中断会留下一个**截断**的文件，而
    ``web.reference_file`` 会把它当正常参考图返回。这里把 rename 打断：
    正式路径必须**不存在** —— 那就证明它只可能由 rename 产生。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)

    def refuse_replace(*_args, **_kwargs):
        raise OSError("rename 被测试打断")

    monkeypatch.setattr(storage.os, "replace", refuse_replace)

    with pytest.raises(OSError, match="rename 被测试打断"):
        asyncio.run(storage.store_reference("g-p11", "r-p11", _FakeUpload(_png_payload())))

    assert not (tmp_path / "generations/g-p11/references/r-p11.png").exists()


def test_skill_json_takes_the_last_object_not_the_first():
    """文档承诺「脚本可以先打日志，真正的结果在最后」；此前实现返回的是**第一个**。

    参数模板是可配置的（用户可以换成自己的脚本），所以「先打一行 JSON 日志」不是纸上
    风险：那一行会被当成结果。
    """
    stdout = '{"status": "warming up"}\n{"prompt": "真正的结果"}'

    assert provider_module._parse_skill_json(stdout, "boom") == {"prompt": "真正的结果"}


def test_skill_json_still_accepts_a_single_object():
    assert provider_module._parse_skill_json('{"prompt": "只有一个"}', "boom") == {
        "prompt": "只有一个"
    }
    with pytest.raises(RuntimeError, match="boom"):
        provider_module._parse_skill_json("只是日志，一个 JSON 都没有", "boom")
