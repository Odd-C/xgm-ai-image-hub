import base64
import json
import os
import re
import socket
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from image_hub import providers
from image_hub.app import app
from image_hub.config import settings
from image_hub.db import SessionLocal
from image_hub.models import Generation, User

# A real 1x1 PNG: validate_artifact actually parses the bytes, so a stub such as
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMB"
    "AQDJ/pLvAAAAAElFTkSuQmCC"
)


def _login(client: TestClient) -> str:
    page = client.get("/login")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={"username": "admin", "password": "test-password", "csrf": token},
        follow_redirects=False)
    assert response.status_code == 303
    # Logging in rotates the CSRF token, so the pre-login value is dead. Read
    match = re.search(r'name="csrf" value="([^"]+)"', client.get("/admin").text)
    return match.group(1) if match else token


def _dns_result(address: str):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]


def _set_dns(monkeypatch, address: str) -> None:
    monkeypatch.setattr(
        providers.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns_result(address))


@pytest.mark.parametrize(
    ("models", "message"),
    [
        # 2:1 现在是合法比例（上游支持），换一个确实不存在的。
        ("model-a|Model A|7:5|2K|4|true|auto", "不支持的比例"),
        ("model-a|Model A|1:1|8K|4|true|auto", "不支持的分辨率"),
        ("model-a|Model A|1:1|2K|4|true|ultra", "不支持的质量"),
        (
            "model-a|Model A|1:1|2K|4|true|auto,"
            + "model-a|Model B|1:1|2K|4|true|auto",
            "不得重复"),
        # 第 8 个字段是该模型的附加请求头，第 9 个是它要走的 API 路由名（人工单选），
        ("model-a|Model A|1:1|2K|4|true|auto|1K=1024", "名称:值"),
        ("model-a|Model A|1:1|2K|4|true|auto|X-Bad Name:v", "名称不合法"),
        # 这里曾接受 ``;`` 分隔的候选名单，现已收敛为单选：多值必须报错，不能静默忽略
        ("model-a|Model A|1:1|2K|4|true|auto||route-a;route-b", "只能指定一条"),
        ("model-a|Model A|1:1|2K|4|true|auto||route a", "路由名只能是"),
        # 介绍在配置串里同样是一个字段：分号会和「字段内分隔符」的语义打架，必须拒绝；
        ("model-a|Model A|1:1|2K|4|true|auto|||白底图;海报", "不能包含"),
        ("model-a|Model A|1:1|2K|4|true|auto|||" + "x" * 161, "不能超过"),
        # 超过 11 个字段才是格式无效（第 11 段已升格为上游模型名候选）。
        (
            "model-a|Model A|1:1|2K|4|true|auto|X-A:1||note|extra|too-many",
            "格式无效"),
        # 第 11 段（上游模型名候选）：空白让「写下来的名字」与「发出去的名字」不再
        ("model-a|Model A|1:1|2K|4|true|auto|||note|up stream", "不得包含空白"),
        ("model-a|Model A|1:1|2K|-1|true|auto", "0 到 14"),
        ("model-a|Model A|1:1|2K|15|true|auto", "0 到 14"),
    ])
def test_admin_rejects_unsupported_or_malformed_capabilities(
    tmp_path, monkeypatch, models, message
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    with TestClient(app) as client:
        token = _login(client)
        response = client.post(
            "/admin/providers/api",
            data={
                "csrf": token,
                "base_url": "https://images.example.test/v1",
                "api_key": "not-a-real-key",
                "models": models,
            })
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert "not-a-real-key" not in response.text
    assert not (tmp_path / "provider-config.json").exists()


def test_admin_validation_error_does_not_echo_sensitive_form_values(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    secret = "not-a-real-key-" + "x" * 1000
    base_url = "https://sensitive-route.example.test/v1"
    upstream_model = "sensitive-upstream-model"
    with TestClient(app) as client:
        token = _login(client)
        response = client.post(
            "/admin/providers/api",
            data={
                "csrf": token,
                "base_url": base_url,
                "api_key": secret,
                "models": f"{upstream_model}|Public Model",
            })
    assert response.status_code == 422
    for forbidden in (secret, base_url, upstream_model):
        assert forbidden not in response.text


def test_admin_accepts_supported_https_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.4.4")
    with TestClient(app) as client:
        token = _login(client)
        response = client.post(
            "/admin/providers/api",
            data={
                "csrf": token,
                "base_url": "https://images.example.test/v1",
                "api_key": "not-a-real-key",
                "models": "model-a|Model A|1:1;16:9|1K;4K|14|true|auto",
            },
            follow_redirects=False)
    assert response.status_code == 303


@pytest.mark.parametrize(
    "parameters",
    [
        {"ratio": "2:1", "resolution": "2K", "quality": "standard"},
        {"ratio": "1:1", "resolution": "8K", "quality": "standard"},
        {"ratio": "1:1", "resolution": "2K", "quality": "ultra"},
    ])
def test_api_executor_rejects_unknown_stored_capabilities(parameters):
    profile = providers.ModelProfile(
        id="api:opaque",
        label="Model A",
        provider="api",
        upstream_model="private-model-a",
        enabled=True,
        ratios=("1:1", "2:1"),
        resolutions=("2K", "8K"),
        qualities=("standard", "ultra"))
    generation = SimpleNamespace(
        parameters_json=json.dumps(parameters),
        references=[],
        original_prompt="test", optimized_prompt="")
    with pytest.raises(RuntimeError, match="执行器不支持"):
        providers._execute_api(generation, profile)


def test_private_json_files_are_atomic_and_private_from_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")

    providers.save_api_config(
        "https://images.example.test/v1",
        "not-a-real-key",
        "private-model-a|Model A|1:1|2K|4|true|auto")
    identity = providers._freeze_api_execution_config()

    paths = [
        tmp_path / "provider-config.json",
        tmp_path / "provider-configs" / f"{identity}.json",
    ]
    for path in paths:
        # POSIX exposes the 0600 mode directly. Windows has no mode bits to read
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text())["api_key"] == "not-a-real-key"
    assert not list(tmp_path.rglob("*.tmp"))


def test_atomic_write_failure_preserves_destination_and_removes_private_temp(
    tmp_path, monkeypatch
):
    destination = tmp_path / "provider-config.json"
    destination.write_text('{"before":true}')
    destination.chmod(0o600)
    real_open = providers.os.open
    creation_modes = []

    def observed_open(path, flags, mode=0o777):
        if flags & os.O_CREAT:
            creation_modes.append(mode)
        return real_open(path, flags, mode)

    monkeypatch.setattr(providers.os, "open", observed_open)
    monkeypatch.setattr(providers.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        providers._atomic_write_private_json(destination, {"after": True})

    assert destination.read_text() == '{"before":true}'
    assert creation_modes == [0o600]
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "169.254.10.20", "192.0.2.10", "10.20.30.40"])
def test_endpoint_validation_rejects_non_public_destinations_by_default(
    monkeypatch, address
):
    monkeypatch.setattr(settings, "api_private_network_allowlist", "")
    _set_dns(monkeypatch, address)
    with pytest.raises(providers.ProviderConfigError):
        providers._resolve_and_validate_endpoint("https://images.example.test/v1")


@pytest.mark.parametrize("allowlist", ["internal.example.test", "10.20.0.0/16"])
def test_server_allowlist_can_authorize_intended_private_destination(monkeypatch, allowlist):
    monkeypatch.setattr(settings, "api_private_network_allowlist", allowlist)
    _set_dns(monkeypatch, "10.20.30.40")
    assert providers._resolve_and_validate_endpoint("https://internal.example.test/v1") == (
        "10.20.30.40",)


def test_execution_config_rejects_dns_rebinding(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "api_private_network_allowlist", "")
    _set_dns(monkeypatch, "8.8.8.8")
    config = providers._validated_api_config(
        "https://images.example.test/v1",
        "not-a-real-key",
        "private-model-a|Model A|1:1|2K|4|true|auto")
    identity = providers._execution_config_id(config)
    path = tmp_path / "provider-configs" / f"{identity}.json"
    providers._atomic_write_private_json(path, config)

    _set_dns(monkeypatch, "8.8.4.4")
    with pytest.raises(RuntimeError, match="DNS 已变化"):
        providers._load_api_execution_config(identity)


def test_api_configuration_and_failures_do_not_leak_server_routing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    secret = "not-a-real-private-key"
    base_url = "https://private-route.example.test/v1"
    upstream_model = "raw-upstream-model-987"
    environment_name = "IMAGE_HUB_OPENAI_IMAGE_API_KEY"
    providers.save_api_config(
        base_url,
        secret,
        f"{upstream_model}|Public Model|1:1|2K|4|true|auto")

    public_profiles = json.dumps(
        [profile.public_dict() for profile in providers.model_profiles()], ensure_ascii=False
    )
    public_config = json.dumps(providers.public_api_config())
    for forbidden in (secret, base_url, upstream_model, environment_name):
        assert forbidden not in public_profiles
        assert forbidden not in public_config

    with TestClient(app) as client:
        token = _login(client)
        project_response = client.post(
            "/projects",
            data={"name": "路由保密测试", "csrf": token},
            follow_redirects=False)
        project_id = project_response.headers["location"].rsplit("/", 1)[-1]
        with SessionLocal() as session:
            user = session.query(User).filter_by(username="admin").one()
            generation = Generation(
                user_id=user.id,
                project_id=project_id,
                idempotency_key="secret-history-row",
                original_prompt="test", optimized_prompt="",
                provider="api",
                model_id=upstream_model,
                model_label=upstream_model,
                provider_snapshot_json=json.dumps(
                    {
                        "id": f"api:{upstream_model}",
                        "upstream_model": upstream_model,
                    }
                ),
                parameters_json=json.dumps(
                    {"ratio": "1:1", "resolution": "2K", "quality": "standard"}
                ),
                status="failed",
                error_message=f"{secret} {base_url} {upstream_model} {environment_name}")
            session.add(generation)
            session.commit()

        responses = [
            client.get("/admin").text,
            client.get(f"/projects/{project_id}").text,
            client.get(f"/projects/{project_id}/history").text,
            client.get(f"/api/projects/{project_id}/generations").text,
        ]
    for response_text in responses:
        for forbidden in (secret, base_url, upstream_model, environment_name):
            assert forbidden not in response_text


# ── LibTV / Lovart：官方 skill 脚本适配 ──────────────────────────────────


def _fake_skill_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Build a throwaway skill layout so path validation has something real to accept."""
    scripts = tmp_path / "libtv-skill" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("create_session.py", "query_session.py", "download_results.py", "upload_file.py"):
        (scripts / name).write_text("# probe\n", encoding="utf-8")
    lovart = tmp_path / "lovart-api" / "scripts"
    lovart.mkdir(parents=True)
    script = lovart / "agent_skill.py"
    script.write_text("# probe\n", encoding="utf-8")
    return scripts, script




def test_admin_stores_lovart_credentials_and_rejects_unsafe_values(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _, lovart_script = _fake_skill_layout(tmp_path)

    assert (
        providers.save_lovart_credentials("probe-ak", "probe-sk", str(lovart_script))
        == "saved"
    )
    public = providers.public_native_credentials()
    assert public["lovart"]["credentials_ready"] is True
    assert public["lovart"]["script_ready"] is True

    # 非 .py 脚本一律拒绝。
    with pytest.raises(providers.ProviderConfigError):
        providers.save_lovart_credentials("probe-ak", "probe-sk", str(tmp_path / "x.txt"))


def test_lovart_credentials_are_scoped_per_pool_account_not_shared(tmp_path, monkeypatch):
    """池里每个平台账号各存各的 AK/SK，**互不覆盖**，也不覆盖旧单槽。

    这是账号池的地基：两个号若共用一份凭据，「切到下一个号」就永远切不动 ——
    而且是在**用错账号出图**这种最坏的方式上失败，界面上看不出来。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "lovart_access_key", "")
    monkeypatch.setattr(settings, "lovart_secret_key", "")
    first, second = "c" * 32, "d" * 32

    providers.save_lovart_credentials("default-ak", "default-sk")
    providers.save_lovart_credentials("first-ak", "first-sk", account_id=first)
    providers.save_lovart_credentials("second-ak", "second-sk", account_id=second)

    assert providers._resolved_lovart_credentials()[:2] == ("default-ak", "default-sk")
    assert providers._resolved_lovart_credentials(first)[:2] == ("first-ak", "first-sk")
    assert providers._resolved_lovart_credentials(second)[:2] == ("second-ak", "second-sk")

    # 清除一个号既不影响池里的其它号，也不影响默认槽。
    providers.save_lovart_credentials("", "", account_id=first, clear=True)
    assert providers._resolved_lovart_credentials(first)[0] == ""
    assert providers._resolved_lovart_credentials(second)[0] == "second-ak"
    assert providers._resolved_lovart_credentials()[0] == "default-ak"


def test_env_lovart_credentials_are_never_a_runtime_fallback(tmp_path, monkeypatch):
    """环境变量里的平台凭据**不是**运行时兜底。

    用户 2026-09-22 定性：「**平台账号都需要隔离**」—— 总管理员与管理员之间、
    管理员彼此之间都要隔离。而 env 那对凭据是**部署级**的，一旦当运行时兜底，
    任何租户都能读到它、等于全体共用同一个平台账号，隔离就假掉了。

    ⇒ 运行时读不到就是「未配置」；env 只在启动时一次性归入根总管理员
    （见 ``seed_root_native_credentials``）。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "lovart_access_key", "env-ak")
    monkeypatch.setattr(settings, "lovart_secret_key", "env-sk")

    assert providers._resolved_lovart_credentials() == ("", "", "未配置")
    assert providers._resolved_lovart_credentials() == ("", "", "未配置")
    assert providers._resolved_lovart_credentials( "c" * 32) == ("", "", "未配置")


class _StubSession:
    """``_root_superadmin_id`` 只用到 ``scalar``，按需返回指定根账号。"""

    def __init__(self, root_id: str) -> None:
        self._root_id = root_id

    def scalar(self, _statement):
        return self._root_id


def test_lovart_skill_state_is_isolated_per_account(tmp_path, monkeypatch):
    """官方 skill 的本地状态是**机器级**的，必须按平台账号关进各自目录。

    Lovart 的 ``agent_skill.py`` 把状态路径硬编码成
    ``os.path.expanduser("~/.lovart/state.json")``，而 CLI 没有 ``--state`` 参数。
    所以只能改写子进程的 ``HOME`` / ``USERPROFILE`` 来隔离 —— 不隔离的话，同一个服务
    进程下不同账号会共用同一份「活跃项目 + 线程历史」，互相串味。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    captured: dict[str, str] = {}

    def fake_run_skill_script(script, args, *, env_overrides, **kwargs):
        captured[env_overrides["LOVART_ACCESS_KEY"]] = env_overrides["HOME"]
        # Windows 的 expanduser 取 USERPROFILE、POSIX 取 HOME，两处必须指同一个地方。
        assert env_overrides["USERPROFILE"] == env_overrides["HOME"]
        return json.dumps({"ok": True})

    monkeypatch.setattr(providers, "_run_skill_script", fake_run_skill_script)
    script = tmp_path / "agent_skill.py"
    script.write_text("# probe\n", encoding="utf-8")

    def config(access_key: str, account: str = "") -> dict:
        return {
            "provider": "lovart",
            "provider_account_id": account,
            "access_key": access_key,
            "secret_key": "sk",
            "base_url": "https://lgw.lovart.ai",
            "skill_script": str(script),
            "mode": "fast",
            "chat_timeout": 60,
        }

    account_a, account_b = "a" * 32, "b" * 32
    providers._run_lovart("query-mode", execution_config=config("ak-default"))
    providers._run_lovart("query-mode", execution_config=config("ak-a", account_a))
    providers._run_lovart("query-mode", execution_config=config("ak-b", account_b))

    homes = {
        Path(captured["ak-default"]),
        Path(captured["ak-a"]),
        Path(captured["ak-b"]),
    }
    assert len(homes) == 3, "不同账号的 skill 状态目录必须互不相同"
    for home in homes:
        assert home.is_dir()
        # 落在本部署的私有目录下，且**不能**是真实用户的家目录。
        assert tmp_path in home.parents
        assert home != Path.home()


def test_pool_account_ids_are_validated_before_touching_private_paths(tmp_path, monkeypatch):
    """账号 id 直接拼进私有凭据路径，必须先校验。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    for bad in ("../evil", "not-hex", "A" * 32):
        with pytest.raises(providers.ProviderConfigError):
            providers._credentials_path(bad)


def test_native_profiles_are_only_enabled_when_credentials_and_scripts_agree(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _, lovart_script = _fake_skill_layout(tmp_path)
    monkeypatch.setattr(settings, "lovart_skill_script", tmp_path / "absent.py")
    monkeypatch.setattr(settings, "lovart_access_key", "")
    monkeypatch.setattr(settings, "lovart_secret_key", "")

    def status():
        return {
            provider: [p.enabled for p in providers.model_profiles() if p.provider == provider]
            for provider in ("libtv", "lovart")
        }

    # 还没配置 → 两个渠道都不启用。
    assert not any(status()["lovart"])

    # 只有凭据不够，脚本也必须存在。
    providers.save_lovart_credentials("probe-ak", "probe-sk")
    assert not any(status()["lovart"])

    providers.save_lovart_credentials("", "", str(lovart_script))
    assert all(status()["lovart"])

    # 空提交是 no-op，已配好的契约保持启用。
    assert providers.save_lovart_credentials("", "", "") == "kept"
    assert all(status()["lovart"])


def test_clearing_native_configuration_removes_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _, lovart_script = _fake_skill_layout(tmp_path)
    providers.save_lovart_credentials( "probe-ak", "probe-sk", str(lovart_script))

    assert providers.save_lovart_credentials( "", "", "", clear=True) == "cleared"

    public = providers.public_native_credentials()
    assert public["lovart"]["credentials_ready"] is False
    assert public["lovart"]["source"] == "未配置"

    # A clear request must not smuggle a new value in at the same time.
    with pytest.raises(providers.ProviderConfigError):
        providers.save_lovart_credentials( "sneaky-ak", "", clear=True)


def test_blank_submit_keeps_stored_credentials_instead_of_wiping_them(
    tmp_path, monkeypatch
):
    """Pressing save with an empty password field must be a no-op.

    Regression: the "kept" branch used to write the payload back anyway, so a
    plain mis-click with an empty password input replaced a working credential
    file with an empty object — silently disabling every native provider.
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _, lovart_script = _fake_skill_layout(tmp_path)

    providers.save_lovart_credentials( "probe-ak", "probe-sk", str(lovart_script))
    credential_file = (
        tmp_path / "private" / "native-provider-credentials" / "default.json"
    )
    before = credential_file.read_text(encoding="utf-8")

    # Both routes submit every field, so an untouched password box arrives as "".
    assert providers.save_lovart_credentials( "", "", "") == "kept"

    assert credential_file.read_text(encoding="utf-8") == before
    public = providers.public_native_credentials()
    assert public["lovart"]["credentials_ready"] is True

    # A blank submit against a clean slate must not create an empty file either.
    credential_file.unlink()
    assert providers.save_lovart_credentials( "", "", "") == "kept"
    assert not credential_file.exists()


def test_skill_script_runner_passes_arguments_and_env_and_parses_noisy_output(
    tmp_path, monkeypatch
):
    """Drive a real script through the same runner the executors use."""
    script = tmp_path / "probe_skill.py"
    script.write_text(
        "import json, os, sys\n"
        "print('log line that is not json')\n"
        "print(json.dumps({'argv': sys.argv[1:], 'key': os.environ.get('LIBTV_ACCESS_KEY', '')}))\n",
        encoding="utf-8")
    payload = providers._parse_skill_json(
        providers._run_skill_script(
            script,
            ["query_session", "--after-seq", "3"],
            env_overrides={"LIBTV_ACCESS_KEY": "probe-key"},
            timeout=30,
            script_dir=script),
        "unreachable")
    assert payload == {"argv": ["query_session", "--after-seq", "3"], "key": "probe-key"}


def test_skill_script_runner_reports_failure_without_leaking_the_secret(
    tmp_path, monkeypatch
):
    secret = "probe-secret-key-value"
    script = tmp_path / "failing.py"
    script.write_text(
        "import os, sys\n"
        "sys.stderr.write('upstream rejected ' + os.environ['LIBTV_ACCESS_KEY'])\n"
        "sys.exit(1)\n",
        encoding="utf-8")
    with pytest.raises(RuntimeError) as excinfo:
        providers._run_skill_script(
            script, [], env_overrides={"LIBTV_ACCESS_KEY": secret}, timeout=30,
            secret_values=(secret))
    message = str(excinfo.value)
    assert secret not in message
    assert "[已隐藏]" in message


def test_lovart_execution_config_freezes_the_route(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _, lovart_script = _fake_skill_layout(tmp_path)
    providers.save_lovart_credentials("probe-ak", "probe-sk", str(lovart_script))

    lovart = providers._current_native_execution_config("lovart")
    assert lovart["skill_script"] == str(lovart_script)
    assert lovart["mode"] == settings.lovart_thread_mode
    assert lovart["provider_account_id"] == ""


def test_native_execution_config_refuses_to_freeze_without_scripts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "lovart_skill_script", tmp_path / "missing.py")
    providers.save_lovart_credentials( "probe-ak", "probe-sk")

    with pytest.raises(providers.ProviderConfigError):
        providers._current_native_execution_config("lovart")


def test_queued_native_task_without_a_route_fails_at_run_time_not_at_submit(
    tmp_path, monkeypatch
):
    """A missing upstream route is a normal task failure, not an opaque submit 503."""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "lovart_skill_script", tmp_path / "missing.py")
    profile = providers.ModelProfile(
        id="lovart:nano-banana", label="Nano Banana", provider="lovart",
        upstream_model="generate_image_nano_banana", enabled=True)
    frozen = providers.freeze_profile_execution(profile)
    assert frozen.execution_config_id == ""  # submit must not raise
    with pytest.raises(providers.ProviderConfigError):
        providers._native_execution_config(frozen)


def test_admin_providers_page_exposes_no_skill_paths_or_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _, lovart_script = _fake_skill_layout(tmp_path)
    secret = "probe-secret-never-render"
    providers.save_lovart_credentials( secret, secret, str(lovart_script))

    with TestClient(app) as client:
        token = _login(client)
        page = client.get("/admin/providers")
    assert page.status_code == 200
    body = page.text
    assert "\u6d4b\u8bd5\u8fde\u63a5" in body
    # 用户要求：skills 路径不暴露在前端；凭据本身也绝不回显。
    assert str(lovart_script) not in body
    assert "agent_skill.py" not in body
    assert secret not in body
    assert token in body


def test_check_buttons_submit_to_a_registered_route():
    """The 「测试连接」 buttons must post to a route that actually exists.

    ``app.routes`` does not expand lazily-mounted routers in this FastAPI version,
    so the route list is probed with a real authenticated POST instead of
    inspecting ``app.routes``.
    """
    with TestClient(app) as client:
        _login(client)
        page = client.get("/admin/providers")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        for path in ("/admin/providers/libtv/check", "/admin/providers/lovart/credentials"):
            # A real POST must be routed, not 404. It reaches the connectivity
            data = {"csrf": token, "action": "check", "clear_credentials": "", "confirm_clear": ""}
            response = client.post(path, data=data, follow_redirects=False)
            assert response.status_code != 404, f"{path} 未注册路由"
            assert response.status_code == 303, f"{path} 未按预期重定向"


def test_libtv_connect_renders_a_link_instead_of_cross_origin_redirect(monkeypatch):
    """授权页必须用链接打开，不能靠表单 303 外跳。

    Regression（2026-09-21 实测）：我们的安全头有 ``form-action 'self'``，而
    Chrome/Safari 会把表单提交后的重定向也纳入该指令检查 —— 表单 POST 之后
    303 跳到 ``mcp.liblib.tv`` 会被拦掉，界面表现是「点了没反应」（Firefox 不拦）。
    所以这里断言：返回 200 的中间页 + 页面里带可点击的授权链接。
    """
    authorize_url = "https://mcp.liblib.tv/authorize?client_id=probe&state=probe"
    monkeypatch.setattr(
        "image_hub.libtv_mcp.build_authorization_url", lambda account_id="": authorize_url
    )
    with TestClient(app) as client:
        _login(client)
        page = client.get("/admin/providers")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/admin/providers/libtv/connect",
            data={"csrf": token},
            follow_redirects=False)
    assert response.status_code == 200
    # Jinja 自动转义会把 & 写成 &amp;，所以只断言前缀与 href 形态。
    assert "https://mcp.liblib.tv/authorize" in response.text
    assert 'href="https://mcp.liblib.tv/authorize' in response.text
    # 绝不能是跳到上游的 303（那正是被 CSP 拦掉的那条路径）。
    assert response.headers.get("location") in (None, "")
    # 中间页自带粘贴回调表单，远程部署时不用回上一页。
    assert 'name="callback_url"' in response.text


def test_libtv_connect_rejects_a_missing_csrf_token():
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/admin/providers/libtv/connect", data={}, follow_redirects=False
        )
    # 缺 csrf 字段由请求校验先挡下（422）；带上空值才是 403。
    assert response.status_code in (403, 422)


_CATALOG_ENTRY = {
    "key": "z-image",
    "name": "Z Image Turbo",
    "ratios": ["1:1", "16:9"],
    "resolutions": ["1K", "2K"],
    "qualities": ["low", "medium"],
    "defaults": {"ratio": "16:9", "resolution": "2K", "quality": "medium"},
}


def test_libtv_profiles_follow_the_live_catalog(monkeypatch):
    """LibTV 的入口来自上游活目录（agent 编排已下线，只剩具体模型）。"""
    monkeypatch.setattr("image_hub.libtv_mcp.authorized", lambda account_id="": True)
    monkeypatch.setattr(
        "image_hub.libtv_mcp.model_catalog",
        lambda allow_fetch=True, account_id="": ([_CATALOG_ENTRY], False))
    profiles = {profile.id: profile for profile in providers.model_profiles()}

    model = profiles["libtv:z-image"]
    assert model.label == "Z Image Turbo"
    assert model.upstream_model == "z-image"
    # 上游给的默认档位排在最前（前端取 [0] 当默认值）。
    assert model.ratios == ("16:9", "1:1")
    assert model.resolutions == ("2K", "1K")
    assert model.qualities == ("medium", "low")


class _FakeLibTVMCPModule:
    """Stand-in for libtv_mcp used by the executor tests."""

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments=None, *, timeout=60, account_id=""):
        self.calls.append((name, arguments or {}))
        return self._handler(name, arguments or {})

    def __getattr__(self, name):
        raise AttributeError(f"unexpected libtv_mcp access: {name}")


def _model_generation(ratio="1:1", resolution="2K", quality="medium"):
    return SimpleNamespace(
        id="g1",
        optimized_prompt="",
        original_prompt="a cat",
        parameters_json=json.dumps(
            {"ratio": ratio, "resolution": resolution, "quality": quality}
        ),
        external_project_id="",
        external_task_id="",
        artifact_storage_key="")


def test_libtv_model_channel_submits_the_chosen_model(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)

    def handler(name, arguments):
        if name == "generation_submit":
            return {"task_id": "task-1", "status": "accepted"}
        if name == "task_status":
            return {"outcome": "PROCESSING", "readiness": "PROCESSING", "artifacts": []}
        raise AssertionError(f"unexpected tool {name}")

    fake = _FakeLibTVMCPModule(handler)
    monkeypatch.setattr(providers, "libtv_mcp", fake)
    monkeypatch.setattr(providers, "_native_execution_config",
                        lambda profile: {"provider_account_id": "", "mcp_base": "x"})
    monkeypatch.setattr(providers, "_libtv_ensure_project", lambda account_id="": "project-1")
    monkeypatch.setattr(providers, "_checkpoint", lambda generation: None)

    profile = providers.ModelProfile(
        id="libtv:z-image", label="Z Image Turbo", provider="libtv",
        upstream_model="z-image", enabled=True,
        ratios=("1:1", "16:9"), resolutions=("1K", "2K"), qualities=("low", "medium"))
    generation = _model_generation()
    with pytest.raises(RuntimeError):
        # 还没出图 → 抛错交给自动复查（但提交已经发生，断言它的形状）。
        providers._execute_libtv(generation, profile)

    names = [name for name, _ in fake.calls]
    assert names == ["generation_submit", "task_status"]
    submit = fake.calls[0][1]
    assert submit["kind"] == "image"
    assert submit["project_id"] == "project-1"
    assert submit["idempotency_key"] == "image-hub-gen-g1"
    assert submit["request"]["model"] == "z-image"
    assert submit["request"]["params"] == {
        "prompt": "a cat", "ratio": "1:1", "resolution": "2K", "quality": "medium",
    }
    assert generation.external_task_id == "task-1"
    assert generation.external_project_id == "project-1"


def test_libtv_model_request_drops_unsupported_parameters(monkeypatch, tmp_path):
    """能力面之外的档位静默丢弃，别让上游 400 变成用户可见的任务失败。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    fake = _FakeLibTVMCPModule(lambda name, arguments: {"task_id": "t"} if name == "generation_submit" else {})
    monkeypatch.setattr(providers, "libtv_mcp", fake)
    monkeypatch.setattr(providers, "_native_execution_config",
                        lambda profile: {"provider_account_id": "", "mcp_base": "x"})
    monkeypatch.setattr(providers, "_libtv_ensure_project", lambda account_id="": "project-1")
    monkeypatch.setattr(providers, "_checkpoint", lambda generation: None)

    profile = providers.ModelProfile(
        id="libtv:z-image", label="Z Image Turbo", provider="libtv",
        upstream_model="z-image", enabled=True,
        ratios=("1:1"), resolutions=("1K"), qualities=())
    monkeypatch.setattr(providers, "_poll_libtv_task", lambda *args, **kwargs: None)
    providers._execute_libtv(_model_generation(ratio="21:9", resolution="4K", quality="max"), profile)
    params = fake.calls[0][1]["request"]["params"]
    assert params == {"prompt": "a cat"}  # 三个不支持的值全部丢弃


def test_libtv_task_poll_collects_media_and_reports_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    output_dir = tmp_path / "generations" / "g1" / "results"
    output_dir.mkdir(parents=True)
    artifact = output_dir / "result_01.png"
    artifact.write_bytes(b"png-bytes")

    states = [
        {"outcome": "SUCCEEDED", "readiness": "TERMINAL_READY",
         "artifacts": [{"kind": "image", "node_key": "i-1", "resource_id": "res_1"}]},
    ]

    def handler(name, arguments):
        if name == "task_status":
            return states[0]
        if name == "inspect_media":
            assert arguments["node"] == "i-1"  # resource_id 不能当 file_ref 用
            return {"media": {"urls": ["https://libtv-res.liblib.art/x/a.png"]}}
        raise AssertionError(name)

    monkeypatch.setattr(providers, "libtv_mcp", _FakeLibTVMCPModule(handler))
    monkeypatch.setattr(providers, "_fetch_urls", lambda urls, out, prefix: [artifact])
    monkeypatch.setattr(providers, "_validated_images", lambda paths: list(paths))

    generation = _model_generation()
    providers._poll_libtv_task(generation, "project-1", "task-1", output_dir, "Z Image Turbo")
    assert generation.artifact_storage_key == "generations/g1/results/result_01.png"

    # 终态失败 → 直接报错（任务会被判 failed 并退款）。
    states[0] = {"outcome": "FAILED", "readiness": "TERMINAL_FAILED", "error": "content blocked",
                 "artifacts": []}
    with pytest.raises(RuntimeError) as excinfo:
        providers._poll_libtv_task(generation, "project-1", "task-1", output_dir, "Z")
    assert "content blocked" in str(excinfo.value)

    # 仍在生成 → 交给自动复查（不判死、不退款）。
    states[0] = {"outcome": None, "readiness": "PROCESSING", "artifacts": []}
    with pytest.raises(providers.UpstreamPendingError) as excinfo:
        providers._poll_libtv_task(generation, "project-1", "task-1", output_dir, "Z")
    assert "仍在生成中" in str(excinfo.value)
    # ⚠️ 类型本身是契约：执行器/恢复路径靠它区分"等待"与"失败"，
    assert issubclass(providers.UpstreamPendingError, RuntimeError)


def test_libtv_retired_agent_snapshot_fails_with_a_clear_message(monkeypatch, tmp_path):
    """「智能编排」已下线：老快照要给出明确提示，而不是拿 runId 去查 durable task。

    2026-09-21 按用户要求下线 run_agent 通道（只接传统模型模式）。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    fake = _FakeLibTVMCPModule(lambda name, arguments: (_ for _ in ()).throw(AssertionError(name)))
    monkeypatch.setattr(providers, "libtv_mcp", fake)
    monkeypatch.setattr(providers, "_native_execution_config",
                        lambda profile: {"provider_account_id": "", "mcp_base": "x"})

    for legacy_model in ("agent", "LibTV 智能编排"):
        profile = providers.ModelProfile(
            id="libtv:agent", label="LibTV 智能编排", provider="libtv",
            upstream_model=legacy_model, enabled=True)
        assert providers._libtv_retired_agent(profile) is True
        with pytest.raises(RuntimeError) as excinfo:
            providers._execute_libtv(_model_generation(), profile)
        assert "已下线" in str(excinfo.value)
    # 具体模型不受影响。
    assert providers._libtv_retired_agent(
        SimpleNamespace(upstream_model="z-image")
    ) is False


def test_libtv_profiles_never_publish_the_retired_agent_entry(monkeypatch):
    monkeypatch.setattr("image_hub.libtv_mcp.authorized", lambda account_id="": True)
    monkeypatch.setattr(
        "image_hub.libtv_mcp.model_catalog",
        lambda allow_fetch=True, account_id="": ([_CATALOG_ENTRY], False))
    ids = {profile.id for profile in providers.model_profiles()}
    assert "libtv:agent" not in ids
    assert "libtv:z-image" in ids


def test_libtv_task_context_expiry_is_not_treated_as_a_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    from image_hub import libtv_mcp as real_libtv_mcp

    def handler(name, arguments):
        raise real_libtv_mcp.LibTVMCPError(
            "LibTV 工具执行失败：TASK_CONTEXT_UNAVAILABLE: Task context is absent or expired"
        )

    monkeypatch.setattr(providers, "libtv_mcp", _FakeLibTVMCPModule(handler))
    with pytest.raises(RuntimeError) as excinfo:
        providers._poll_libtv_task(
            _model_generation(), "project-1", "task-1", tmp_path, "Z Image Turbo"
        )
    message = str(excinfo.value)
    assert "上下文已过期" in message
    assert "人工处理" in message


def test_lovart_download_failure_is_retried_and_reported_clearly(tmp_path, monkeypatch):
    """A chat whose inline download failed must fall back to `result`, not crash.

    Regression: the skill records a failed download as {"local_path": None} and
    still exits 0, so the executor found nothing, skipped the retry, and later
    raised a bare WinError 2 from shutil.copy2.
    """
    ok_dir = tmp_path / "downloads"
    ok_dir.mkdir()
    good = ok_dir / "lovart_abc.png"
    good.write_bytes(PNG_BYTES)

    # (a) `result` recovered the file that `chat` could not download.
    assert providers._lovart_downloaded_images(
        {"downloaded": [{"type": "image", "local_path": str(good)}]}
    ) == [good]
    # A failed download entry (local_path None) and a missing file are both ignored.
    assert providers._lovart_downloaded_images(
        {"downloaded": [
            {"type": "image", "local_path": None, "error": "download failed"},
            {"type": "image", "local_path": str(ok_dir / "gone.png")},
            {"type": "video", "local_path": str(good)},
        ]}
    ) == []

    # (b) The reason surfaced to the operator is the upstream explanation.
    reason = providers._lovart_no_image_reason(
        {"final_status": "done", "agent_message": "the model refused the prompt"}
    )
    assert "the model refused the prompt" in reason
    done_reason = providers._lovart_no_image_reason({"final_status": "done"})
    assert "已完成但未产出图片" in done_reason
    assert "done" not in providers._lovart_no_image_reason({"final_status": "queued"})


def test_store_lovart_result_copies_into_the_generation_directory(tmp_path, monkeypatch):
    """The artifact must land under storage_root so the relative key is valid."""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    source_dir = tmp_path / "scratch"
    source_dir.mkdir()
    source = source_dir / "lovart_xyz.png"
    source.write_bytes(PNG_BYTES)
    output_dir = tmp_path / "generations" / "g1" / "results"
    output_dir.mkdir(parents=True)

    class FakeGeneration:
        optimized_prompt = ""
        id = "g1"
        artifact_storage_key = ""

    generation = FakeGeneration()
    providers._store_lovart_result(source, output_dir, generation)

    stored = tmp_path / generation.artifact_storage_key
    assert stored.is_file()
    assert stored.read_bytes() == PNG_BYTES
    # Copying again onto its own target must not fail or truncate the file.
    providers._store_lovart_result(stored, output_dir, generation)
    assert stored.read_bytes() == PNG_BYTES


def test_relative_artifact_paths_are_resolved_against_the_output_directory(tmp_path):
    """The skill echoes back paths that are relative to its *own* cwd.

    Regression: skill scripts run with ``cwd`` set to their script directory, so
    a relative ``--output-dir`` makes the skill write the artifact next to its
    script and echo a path like ``storage\\generations\\...``. Checking that path
    against the *server's* cwd always failed, so a perfectly good image was
    reported as "upstream returned nothing".
    """
    output_dir = tmp_path / "storage" / "generations" / "g1" / "results"
    output_dir.mkdir(parents=True)
    artifact = output_dir / "lovart_abc.png"
    artifact.write_bytes(PNG_BYTES)

    # What the skill prints: a relative path ending in the real file name.
    echoed = {"downloaded": [{
        "type": "image",
        "url": "https://a.lovart.ai/artifacts/agent/abc.png",
        "local_path": r"storage\generations\g1\results\lovart_abc.png",
        "new": False,
    }]}

    # The bare path does not exist relative to the process cwd...
    assert providers._lovart_downloaded_images(echoed) == []
    # ...but it resolves correctly once the intended output_dir is supplied.
    assert providers._lovart_downloaded_images(echoed, output_dir) == [artifact]


def test_zero_byte_artifacts_are_not_treated_as_a_result(tmp_path):
    """A truncated/empty download must not be accepted as a usable image."""
    output_dir = tmp_path / "results"
    output_dir.mkdir()
    empty = output_dir / "lovart_empty.png"
    empty.write_bytes(b"")
    result = {"downloaded": [{"type": "image", "local_path": str(empty)}]}
    assert providers._lovart_downloaded_images(result, output_dir) == []


def test_storage_root_is_absolute_so_subprocesses_cannot_reinterpret_it():
    """storage_root must never stay relative — provider subprocesses chdir away."""
    from image_hub.config import Settings

    assert Settings(storage_root=Path("storage")).storage_root.is_absolute()
    # An explicit absolute path is preserved untouched.
    absolute = Settings(storage_root=Path("C:/tmp/keep")).storage_root
    assert absolute.is_absolute()


def test_server_does_not_500_when_recovery_cannot_find_an_artifact(tmp_path, monkeypatch):
    """`recover` must return a readable message, not an unhandled 500.

    Regression: the admin "查询并恢复" button surfaced a bare
    `FileNotFoundError: [WinError 2]` as "Internal Server Error".
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    captured: list[str] = []

    class FakeGeneration:
        optimized_prompt = ""
        id = "g1"
        provider = "lovart"
        status = "recovery_required"
        external_task_id = "thread-1"
        external_project_id = ""
        artifact_storage_key = ""
        error_message = ""

    def fake_run(*args, **kwargs):
        captured.append(args[0] if args else "")
        return {"downloaded": [{"type": "image", "local_path": None, "error": "download failed"}]}

    monkeypatch.setattr(providers, "_run_lovart", fake_run)
    monkeypatch.setattr(
        providers, "_generation_frozen_profile",
        lambda generation: SimpleNamespace(
            id="lovart:nano-banana-pro", provider="lovart", enabled=True,
            upstream_model="generate_image_nano_banana_pro", execution_config_id="cfg"))
    monkeypatch.setattr(providers, "_native_execution_config", lambda profile: {})

    class FakeSession:
        def get(self, model, key):
            return FakeGeneration()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import image_hub.db as db_module

    monkeypatch.setattr(db_module, "SessionLocal", lambda: FakeSession())

    ok, message = providers.recover_generation("g1")
    assert ok is False
    assert "未返回图片" in message, message


def test_lovart_catalog_entries_are_unique_and_describe_their_upstream_tool():
    """Every catalog row must be usable as a profile and as a --prefer-models value.

    A duplicated id would make ``get_profile`` return None (it fails closed on
    ambiguous matches), silently removing a model from the picker.
    """
    ids = [key for key, _, _ in providers.LOVART_MODELS]
    assert len(ids) == len(set(ids)), f"重复的模型 key：{ids}"

    upstream = [tool for _, _, tool in providers.LOVART_MODELS]
    assert len(upstream) == len(set(upstream)), f"重复的上游工具名：{upstream}"

    for key, label, tool in providers.LOVART_MODELS:
        # The id is what the browser sends back; it must round-trip through the
        assert key and "/" not in key and ":" not in key
        assert label and label.strip() == label
        # `--prefer-models` receives exactly this string.
        assert tool.startswith("generate_image_")


def test_every_lovart_catalog_model_is_exposed_and_resolvable(monkeypatch, tmp_path):
    """The catalog must be what `model_profiles()` actually publishes."""
    # 凭据按租户落盘配置
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "lovart_skill_script", Path(__file__))
    providers.save_lovart_credentials( "ak", "sk")

    profiles = providers.model_profiles()
    lovart = {profile.id: profile for profile in profiles if profile.provider == "lovart"}

    for key, label, tool in providers.LOVART_MODELS:
        profile = lovart.get(f"lovart:{key}")
        assert profile is not None, f"{key} 未出现在 model_profiles()"
        assert profile.label == label
        assert profile.upstream_model == tool
        assert profile.enabled is True
        # These are the dials the picker renders; empty ones would hide the control.
        assert (profile.ratios, profile.resolutions) == providers._lovart_capabilities(tool)
        assert profile.ratios, f"{key} 没有可选比例"
        assert profile.resolutions, f"{key} 没有可选分辨率"
        # A duplicated profile would make get_profile() fail closed.
        assert providers.get_profile(f"lovart:{key}") is not None


def test_lovart_catalog_offers_exactly_the_families_we_use():
    """画布上只暴露实际在用的 Lovart 模型（2026-09-21 的用法裁剪）。

    ⚠️ 这条断言曾经要求 ``gpt-image-1-5`` 也在列表里：那次裁剪把它连同另外 14 个
    一起从 ``LOVART_MODELS`` 移除了（见提交 5d12798），但**测试没跟着改**，
    于是 main 一直是红的 —— 合并时才暴露出来。现在钉住**保留集**本身，
    并反向断言被裁掉的不许悄悄回来。
    """
    tools = {tool for _, _, tool in providers.LOVART_MODELS}
    for tool in (
        # gpt-image-2.5 系（前缀匹配驱动三档比例表）
        "generate_image_gpt_image_2_5_flare",
        "generate_image_gpt_image_2_5_sunburst",
        # VIP 的单档表
        "generate_image_gpt_image_2_vip",
        "generate_image_gpt_image_2",
        # nano-banana 2 系与 pro
        "generate_image_nano_banana_pro",
        "generate_image_nano_banana_2",
        "generate_image_nano_banana_2_lite"):
        assert tool in tools, f"{tool} 缺失"
    for removed in (
        "generate_image_gpt_image_1_5",
        "generate_image_nano_banana",
        "generate_image_seedream_v4",
        "generate_image_seedream_v5_pro",
        "generate_image_flux_2_pro",
        "generate_image_luma_uni_1",
        "generate_image_midjourney",
        "generate_image_ideogram_v4",
        "generate_image_qwen_image3"):
        assert removed not in tools, f"{removed} 已被裁剪，不该回到画布上"


def test_lovart_prefer_models_payload_uses_the_catalog_tool_name(monkeypatch):
    """The executor must forward the upstream tool name, not our opaque key."""
    captured: dict = {}

    monkeypatch.setattr(providers, "_native_execution_config", lambda profile: {})
    monkeypatch.setattr(providers, "_checkpoint", lambda generation: None)
    monkeypatch.setattr(providers, "_references", lambda generation: [])

    def routing(*args, **kwargs):
        if args and args[0] == "create-project":
            return {"project_id": "p1"}
        if args and args[0] == "chat":
            captured["args"] = args
        return {"thread_id": "t1", "final_status": "done", "downloaded": []}

    monkeypatch.setattr(providers, "_run_lovart", routing)

    class FakeGeneration:
        optimized_prompt = ""
        id = "g1"
        original_prompt = "a cat"
        parameters_json = '{"ratio": "16:9", "resolution": "1K"}'
        external_project_id = ""
        external_task_id = ""
        artifact_storage_key = ""

    profile = providers.ModelProfile(
        id="lovart:gpt-image-2",
        label="GPT Image 2 Auto",
        provider="lovart",
        upstream_model="generate_image_gpt_image_2",
        enabled=True)
    with pytest.raises(RuntimeError):
        # No image came back, so the executor reports the reason — but the
        providers._execute_lovart(FakeGeneration(), profile)

    chat = captured["args"]
    payload = json.loads(chat[chat.index("--prefer-models") + 1])
    assert payload == {"IMAGE": ["generate_image_gpt_image_2"]}

    # The size must travel with the prompt: `chat` has no --size flag, so losing
    sent_prompt = chat[chat.index("--prompt") + 1]
    assert sent_prompt.startswith("a cat")
    assert "1672x941" in sent_prompt


def _size_hint(tool: str, ratio: str, resolution: str) -> str:
    return providers._lovart_size_hint(tool, ratio, resolution)


def test_gpt_image_2_5_sizes_are_16_multiples_and_within_bounds():
    """Every published pixel size must satisfy the upstream's hard limits.

    These are constraints, not suggestions: the upstream rejects an off-spec
    canvas instead of rounding it, so a typo in the table below would surface as
    a confusing generation failure rather than an obviously wrong image.
    """
    for ratio, tiers in providers.LOVART_SIZES_GPT_IMAGE_2_5.items():
        for tier, pixels in zip(providers.LOVART_RESOLUTIONS, tiers):
            if pixels is None:
                continue
            width, height = (int(part) for part in pixels.split("x"))
            long_edge, short_edge = max(width, height), min(width, height)
            assert long_edge <= 3840, f"{ratio} {tier} 长边越界"
            assert width % 16 == 0 and height % 16 == 0, f"{ratio} {tier} 不是 16 的倍数"
            assert long_edge <= short_edge * 3, f"{ratio} {tier} 长短比超过 3:1"
            assert 655_360 <= width * height <= 8_294_400, f"{ratio} {tier} 总像素越界"


def test_vip_shares_the_three_tier_table_with_flare_and_sunburst():
    """VIP does not start with `_2_5`, so prefix matching alone would drop it.

    Upstream groups it with Flare/Sunburst under the same 1K/2K/4K table, so a
    silent fallback to the tier-less branch would ship wrong sizes for it alone.
    """
    vip = "generate_image_gpt_image_2_vip"
    flare = "generate_image_gpt_image_2_5_flare"
    for ratio, resolution in (("16:9", "2K"), ("21:9", "4K"), ("1:3", "1K")):
        assert _size_hint(vip, ratio, resolution) == _size_hint(flare, ratio, resolution)
    assert providers._lovart_tiers_for(vip, "1:3") == ("1K", "2K")
    assert providers._lovart_capabilities(vip) == providers._lovart_capabilities(flare)
    assert vip in providers._LOVART_THREE_TIER_TOOLS


def test_size_hint_is_omitted_exactly_when_no_size_was_published():
    """A missing tier must produce no hint rather than an invented size."""
    flare = "generate_image_gpt_image_2_5_flare"
    # 1:3 and 3:1 stop at 2K upstream.
    assert _size_hint(flare, "1:3", "2K") == "1:3（2K，1280x3840）"
    assert _size_hint(flare, "1:3", "4K") == ""
    assert _size_hint(flare, "3:1", "4K") == ""
    # A ratio the model does not expose at all.
    assert _size_hint("generate_image_gpt_image_2", "1:3", "1K") == ""


def test_single_tier_model_reports_pixels_without_a_tier_name():
    """gpt-image-2 has one size per ratio, so quoting a tier would mislead."""
    hint = _size_hint("generate_image_gpt_image_2", "16:9", "1K")
    assert hint == "16:9（1672x941）"
    assert "1K" not in hint


def test_tier_chips_match_the_sizes_a_ratio_actually_has():
    """The picker must not offer a tier the chosen ratio cannot produce."""
    flare = "generate_image_gpt_image_2_5_flare"
    assert providers._lovart_tiers_for(flare, "1:3") == ("1K", "2K")
    assert providers._lovart_tiers_for(flare, "1:1") == providers.LOVART_RESOLUTIONS
    assert providers._lovart_tiers_for("generate_image_nano_banana", "1:1") == (
        providers.LOVART_RESOLUTIONS
    )
    # A single-tier model has one neutral chip for every ratio — never a 4K
    for ratio in providers.LOVART_RATIOS_GPT_IMAGE_2:
        assert providers._lovart_tiers_for(
            "generate_image_gpt_image_2", ratio
        ) == providers.LOVART_SINGLE_TIER


def test_lovart_prompt_appends_the_size_without_rewriting_the_text():
    """The user's prompt is the user's; the size is appended on its own line."""

    class FakeGeneration:
        optimized_prompt = ""
        original_prompt = "多行提示词\n第二行  含  <特殊> & 符"

        def __init__(self, ratio, resolution):
            self.parameters_json = json.dumps({"ratio": ratio, "resolution": resolution})

    profile = providers.ModelProfile(
        id="lovart:x", label="x", provider="lovart",
        upstream_model="generate_image_gpt_image_2_5_flare", enabled=True)
    result = providers._lovart_prompt(FakeGeneration("16:9", "2K"), profile)
    assert result.startswith(FakeGeneration.original_prompt)
    assert result == f"{FakeGeneration.original_prompt}\n\n画面比例：16:9（2K，2048x1152）"


def test_auto_ratio_leaves_the_prompt_untouched():
    """`auto` means "let the agent decide", so a constraint would contradict it."""

    class FakeGeneration:
        optimized_prompt = ""
        original_prompt = "a cat"
        parameters_json = '{"ratio": "auto", "resolution": "2K"}'

    profile = providers.ModelProfile(
        id="lovart:x", label="x", provider="lovart",
        upstream_model="generate_image_gpt_image_2_5_flare", enabled=True)
    assert providers._lovart_prompt(FakeGeneration(), profile) == "a cat"


def test_public_dict_exposes_per_ratio_tiers_for_lovart_only():
    """The frontend narrows its chips from this map, so it must be present."""
    flare = providers.ModelProfile(
        id="lovart:x", label="x", provider="lovart",
        upstream_model="generate_image_gpt_image_2_5_flare", enabled=True,
        ratios=providers.LOVART_RATIOS_GPT_IMAGE_2_5,
        resolutions=providers.LOVART_RESOLUTIONS)
    payload = flare.public_dict()
    assert payload["tiers"]["1:3"] == ["1K", "2K"]
    assert payload["tiers"]["1:1"] == ["1K", "2K", "4K"]
    assert "upstream_model" not in payload

    single = providers.ModelProfile(
        id="lovart:gpt-image-2", label="x", provider="lovart",
        upstream_model="generate_image_gpt_image_2", enabled=True,
        ratios=providers.LOVART_RATIOS_GPT_IMAGE_2,
        resolutions=providers.LOVART_SINGLE_TIER)
    assert {tuple(v) for v in single.public_dict()["tiers"].values()} == {("自动",)}

    api = providers.ModelProfile(
        id="api:x", label="x", provider="api", upstream_model="gpt-image-1", enabled=True
    )
    assert api.public_dict()["tiers"] == {}


# ── API 通道：quality / size 真正下发、第 8 字段 edges、历史快照回退 ──────


def _run_api_executor_with_fake_client(
    profile, generation, monkeypatch, tmp_path, models="", family=""
):
    """用假 httpx.Client 驱动 _execute_api，返回捕获到的请求体。

    ``models`` 透传进冻结配置，让执行器能按模型查到它自己的附加请求头。

    ``family`` 是**请求形状**（``grsai_native`` / ``openai_compat``）。默认留空 =
    沿用「按模型名推断」的老行为 —— 内置模型一律被推断成 ``grsai_native``，
    于是测 OpenAI 兼容端点（同一个模型挂在别家的中转上）必须显式声明它。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    # 这个 helper 只关心「请求体长什么样」，与回复方式无关 —— 钉死 sync，免得
    monkeypatch.setattr(settings, "api_reply_mode", "sync")
    monkeypatch.setattr(
        providers, "_load_api_execution_config",
        lambda _id: {
            "base_url": "https://api.test/v1", "api_key": "k",
            "models": models, "family": family,
        })
    captured: dict = {}

    class _FakeResponse:
        #: 执行器现在会读 status_code 做错误分类（瞬时 vs 永久），不再靠
        def __init__(self, payload=None, content=PNG_BYTES, status_code=200):
            self._payload = payload or {}
            self.content = content
            self.status_code = status_code

        @property
        def headers(self):
            # 结果下载走流式接口，它先看 Content-Length；空表即「上游没声明」。
            return {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield self.content

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return _FakeResponse(payload={"data": [{"url": "https://cdn.test/r.png"}]})

        def get(self, url):
            return _FakeResponse(content=PNG_BYTES)

        def stream(self, method, url):
            """结果下载走的是**流式**接口（地址校验与体积上限都挂在那条路上）。

            ⚠️ 这个替身刻意**不**触发地址守卫：本组用例只关心请求体形状，守卫本身在
            ``test_hardening`` 的 P3/P4（含 API 渠道那条新用例）里有专门覆盖。
            缺了 ``stream()``，``_execute_api`` 会直接 AttributeError —— 替身与真实
            ``httpx.Client`` 的接口脱节时，坏的是测试而不是产品。
            """
            assert method == "GET"
            captured["download_url"] = url
            return _FakeResponse(content=PNG_BYTES)

    monkeypatch.setattr(providers.httpx, "Client", _FakeClient)
    providers._execute_api(generation, profile)
    return captured


def test_api_executor_sends_quality_and_omits_legacy_params(tmp_path, monkeypatch):
    """原生端点：没有 n / response_format / watermark / size，quality 无条件下发。"""
    profile = providers.ModelProfile(
        id="api:1", label="M", provider="api", upstream_model="gpt-image-2.5-sunburst",
        enabled=True, ratios=("1:1"), resolutions=("2K"),
        qualities=("low", "medium", "high", "xhigh", "max"))
    generation = SimpleNamespace(
        id="gen-1",
        parameters_json=json.dumps({"ratio": "1:1", "resolution": "2K", "quality": "high"}),
        references=[], original_prompt="a cat", optimized_prompt="")
    captured = _run_api_executor_with_fake_client(profile, generation, monkeypatch, tmp_path)

    body = captured["body"]
    assert body["quality"] == "high"
    assert body["model"] == "gpt-image-2.5-sunburst"
    assert body["replyType"] == "json"
    # pixel_only：比例 + 档位查表换算成像素值。1:1 的 2K 是 2048x2048。
    assert body["aspectRatio"] == "2048x2048"
    assert "imageSize" not in body
    for stale in ("n", "response_format", "watermark", "size"):
        assert stale not in body
    assert captured["url"].endswith("/api/generate")
    assert captured["headers"]["Authorization"] == "Bearer k"


def test_api_executor_converts_ratio_and_tier_through_the_pixel_table(tmp_path, monkeypatch):
    """16:9 的 2K 是 2048x1152 —— 查表值，不是按边长等比算出来的。"""
    profile = providers.ModelProfile(
        id="api:2", label="M", provider="api", upstream_model="gpt-image-2.5-flare",
        enabled=True, ratios=("16:9"), resolutions=("2K"),
        qualities=("low", "medium", "high"))
    generation = SimpleNamespace(
        id="gen-2",
        parameters_json=json.dumps({"ratio": "16:9", "resolution": "2K", "quality": "medium"}),
        references=[], original_prompt="a cat", optimized_prompt="")
    captured = _run_api_executor_with_fake_client(profile, generation, monkeypatch, tmp_path)
    assert captured["body"]["aspectRatio"] == "2048x1152"
    assert captured["body"]["quality"] == "medium"


def test_api_executor_uses_ratio_tier_for_nano_banana(tmp_path, monkeypatch):
    """nano-banana 系列把比例与档位分开传，且不发送它根本没有的质量维度。"""
    profile = providers.ModelProfile(
        id="api:3", label="M", provider="api", upstream_model="nano-banana-2",
        enabled=True, ratios=("16:9"), resolutions=("2K"), qualities=())
    generation = SimpleNamespace(
        id="gen-3",
        # 模型没有质量维度时前端会兜底传一个 standard，执行器必须忽略它。
        parameters_json=json.dumps({"ratio": "16:9", "resolution": "2K", "quality": "standard"}),
        references=[], original_prompt="a cat", optimized_prompt="")
    captured = _run_api_executor_with_fake_client(profile, generation, monkeypatch, tmp_path)
    assert captured["body"]["aspectRatio"] == "16:9"
    assert captured["body"]["imageSize"] == "2K"
    assert "quality" not in captured["body"]


def test_api_executor_refuses_a_combination_the_upstream_does_not_offer(tmp_path, monkeypatch):
    """1:3 在 flare/sunburst 上没有 1K 档位，必须明确拒绝而不是猜一个尺寸。"""
    profile = providers.ModelProfile(
        id="api:4", label="M", provider="api", upstream_model="gpt-image-2.5-sunburst",
        enabled=True, ratios=("1:3"), resolutions=("1K"), qualities=("high"))
    generation = SimpleNamespace(
        id="gen-4",
        parameters_json=json.dumps({"ratio": "1:3", "resolution": "1K", "quality": "high"}),
        references=[], original_prompt="a cat", optimized_prompt="")
    with pytest.raises(RuntimeError, match="不提供"):
        _run_api_executor_with_fake_client(profile, generation, monkeypatch, tmp_path)


def test_frozen_profile_tolerates_a_snapshot_with_retired_fields(monkeypatch, tmp_path):
    """历史快照可能带着已经删掉的字段（例如 size_edges）。

    过滤掉未知键，而不是让 TypeError 被当成"缺少有效快照"—— 否则一个本来完全
    能跑的历史任务会被直接判死。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    snapshot = {
        "id": "api:old", "label": "M", "provider": "api",
        "upstream_model": "gpt-image-2.5-sunburst", "enabled": True,
        "ratios": ["1:1"], "resolutions": ["2K"], "qualities": ["high"],
        "max_references": 4, "execution_config_id": "cfg-1",
        "size_edges": [["1K", 1024], ["2K", 2048]],
    }
    generation = SimpleNamespace(
        provider_snapshot_json=json.dumps(snapshot),
        provider="api", model_id="gpt-image-2.5-sunburst")
    profile = providers._generation_frozen_profile(generation)
    assert profile is not None
    assert profile.upstream_model == "gpt-image-2.5-sunburst"
    # 快照从 JSON 反序列化，seq 字段是 list 而不是 tuple —— 断言按值比较即可。
    assert list(profile.qualities) == ["high"]


def test_admin_accepts_a_model_without_a_quality_axis(tmp_path, monkeypatch):
    """nano-banana 系列没有质量维度：留空 qualities 必须被接受，否则配不出来。

    回归：多选表单要求每行至少勾一个质量，而内置的 nano-banana 模型没有质量维度，
    页面默认不勾任何一项 —— 直接保存会被前端和后端同时拦下。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.4.4")
    with TestClient(app) as client:
        token = _login(client)
        response = client.post(
            "/admin/providers/api",
            data={
                "csrf": token,
                "base_url": "https://images.example.test/v1",
                "api_key": "not-a-real-key",
                # 第 7 字段留空 = 该模型没有质量维度
                "models": "nano-banana-2|Nano Banana 2|1:1;16:9|1K;2K|4|true|",
            },
            follow_redirects=False)
    assert response.status_code == 303
    parsed = providers._parse_api_models("m|M|1:1|2K|4|true|")
    assert parsed[0]["qualities"] == ()


def test_api_executor_keeps_the_openai_endpoint_for_non_builtin_models(tmp_path, monkeypatch):
    """非内置模型（火山方舟、自建中转）必须继续走 OpenAI 兼容端点。

    回归：曾经把端点无条件换成 Grsai 的 /api/generate，把所有非 Grsai 的配置
    一起打断了 —— 默认的 doubao 模型就是这样失败的。
    """
    profile = providers.ModelProfile(
        id="api:x", label="M", provider="api",
        upstream_model="doubao-seedream-5-0-pro-260628",
        enabled=True, ratios=("1:1"), resolutions=("2K"), qualities=())
    generation = SimpleNamespace(
        id="gen-x",
        parameters_json=json.dumps({"ratio": "1:1", "resolution": "2K", "quality": "standard"}),
        references=[], original_prompt="a cat", optimized_prompt="")
    captured = _run_api_executor_with_fake_client(profile, generation, monkeypatch, tmp_path)
    body = captured["body"]
    assert captured["url"].endswith("/images/generations")
    assert body["size"] == "2048x2048"
    assert "aspectRatio" not in body
    assert "replyType" not in body
    assert "quality" not in body  # 这个模型没有质量维度


def test_api_executor_uses_the_official_pixel_table_on_a_compat_relay(tmp_path, monkeypatch):
    """兼容端点上跑内置模型：size 取**官方表**，不按边长等比推算。

    回归：官方表里 1:1 的 4K 是 2880x2880，等比推算会给出 4096x4096，而中转站
    认前者、不认后者（后者被**静默**降级成 2048x2048，不报错也不留痕）——
    等于用户点了 4K、拿到的却是 2K，账面上还写着成功。
    """
    profile = providers.ModelProfile(
        id="api:compat", label="M", provider="api", upstream_model="gpt-image-2.5-flare",
        enabled=True, ratios=("1:1"), resolutions=("4K"), qualities=("medium"))
    generation = SimpleNamespace(
        id="gen-compat",
        parameters_json=json.dumps({"ratio": "1:1", "resolution": "4K", "quality": "medium"}),
        references=[], original_prompt="a cat", optimized_prompt="")
    captured = _run_api_executor_with_fake_client(
        profile, generation, monkeypatch, tmp_path, family="openai_compat"
    )
    body = captured["body"]
    assert captured["url"].endswith("/images/generations")
    assert body["size"] == "2880x2880"
    assert "aspectRatio" not in body
    assert "imageSize" not in body


def test_api_executor_refuses_a_table_gap_on_a_compat_relay(tmp_path, monkeypatch):
    """有官方表、但这一组不在表里 → 明确拒绝，**不再**退回等比推算。

    1:3 在这张表里只有 2K/4K，没有 1K（上游的真实约束）。旧行为会算出一个
    688x... 之外的等比尺寸发过去，然后被上游静默降级 —— 拒绝比猜一个尺寸诚实。
    """
    profile = providers.ModelProfile(
        id="api:compat-gap", label="M", provider="api",
        upstream_model="gpt-image-2.5-flare",
        enabled=True, ratios=("1:3"), resolutions=("1K"), qualities=("medium"))
    generation = SimpleNamespace(
        id="gen-compat-gap",
        parameters_json=json.dumps({"ratio": "1:3", "resolution": "1K", "quality": "medium"}),
        references=[], original_prompt="a cat", optimized_prompt="")
    with pytest.raises(RuntimeError, match="上游不提供该比例与分辨率的组合"):
        _run_api_executor_with_fake_client(
            profile, generation, monkeypatch, tmp_path, family="openai_compat"
        )


def test_per_model_headers_are_isolated_from_each_other(tmp_path, monkeypatch):
    """每个模型只拿到自己那一份请求头，A 的绝不能出现在 B 的请求里。

    这是「逐模型请求头」这个需求的核心契约：头随模型走，按 model_id 精确查。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    providers.save_api_config(
        "https://api.example.com/v1",
        "fallback-secret",
        "model-a|Model A|1:1|2K|4|true|auto|X-Only-A:aaa,"
        "model-b|Model B|1:1|2K|4|true|auto|X-Only-B:bbb;X-Shared:from-b")
    config = providers._api_config()

    headers_a = providers._api_request_headers(config, "model-a")
    headers_b = providers._api_request_headers(config, "model-b")
    headers_unknown = providers._api_request_headers(config, "model-c")

    assert headers_a["X-Only-A"] == "aaa"
    assert "X-Only-B" not in headers_a  # B 的头没渗进 A
    assert headers_b["X-Only-B"] == "bbb"
    assert "X-Only-A" not in headers_b  # A 的头没渗进 B
    # 没配过的模型只有默认鉴权，不会继承别人的头。
    assert headers_unknown == {"Authorization": "Bearer fallback-secret"}


def test_a_model_header_can_override_or_drop_the_default_authorization(tmp_path, monkeypatch):
    """逐模型的头要能覆盖默认 Bearer，也能把它整个去掉。

    换上游时鉴权方式常常完全不一样（有的要 ``X-Api-Key``，有的根本不要鉴权），
    只能覆盖不能删除的话，默认那张 Authorization 就会一直跟着发过去。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    providers.save_api_config(
        "https://api.example.com/v1",
        "fallback-secret",
        "model-a|Model A|1:1|2K|4|true|auto|X-Api-Key:own-key,"
        "model-b|Model B|1:1|2K|4|true|auto|Authorization:")
    config = providers._api_config()

    # 覆盖：换一种鉴权方式，但默认头不该被留着。
    assert providers._api_request_headers(config, "model-a") == {
        "Authorization": "Bearer fallback-secret",
        "X-Api-Key": "own-key",
    }
    # 空值 = 删除该头。
    assert providers._api_request_headers(config, "model-b") == {}


def test_header_values_reject_control_characters_and_bad_names(tmp_path, monkeypatch):
    """请求头是拼进 HTTP 报文的：控制字符会导致头注入，非法名称会截断配置串。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    with pytest.raises(providers.ProviderConfigError, match="控制字符"):
        providers._parse_api_headers("X-A:ok\r\nX-Injected:1")
    # 分隔符出现在名称里必须拒：竖线/逗号会截断配置串。分号会先被切成两段，
    for bad_name in ("X|A", "X,A"):
        with pytest.raises(providers.ProviderConfigError, match="名称不合法"):
            providers._parse_api_headers(f"{bad_name}:v")
    with pytest.raises(providers.ProviderConfigError):
        providers._parse_api_headers("X;A:v")
    with pytest.raises(providers.ProviderConfigError, match="名称:值"):
        providers._parse_api_headers("NoColonHere")


def test_execute_api_applies_per_model_headers_and_keeps_them_isolated(
    tmp_path, monkeypatch
):
    """执行层也要兑现隔离：跑 model-a 只带 A 的头，绝不带 B 的。

    单测 ``_api_request_headers`` 只验证了查表逻辑；这里走真实 ``_execute_api``，
    确认拼出来的 HTTP 请求确实只带本模型的头。
    """
    models = (
        "model-a|Model A|1:1|2K|4|true|auto|X-Only-A:aaa;X-Shared:from-a|,"
        "model-b|Model B|1:1|2K|4|true|auto|X-Only-B:bbb;X-Shared:from-b|"
    )
    generation = SimpleNamespace(
        id="gen-x",
        parameters_json=json.dumps({"ratio": "1:1", "resolution": "2K", "quality": "auto"}),
        references=[], original_prompt="a cat", optimized_prompt="")
    profile_a = providers.ModelProfile(
        id="api:a", label="A", provider="api", upstream_model="model-a",
        enabled=True, ratios=("1:1"), resolutions=("2K"), qualities=("auto"))
    captured_a = _run_api_executor_with_fake_client(
        profile_a, generation, monkeypatch, tmp_path, models=models
    )
    assert captured_a["headers"]["X-Only-A"] == "aaa"
    assert captured_a["headers"]["X-Shared"] == "from-a"
    assert "X-Only-B" not in captured_a["headers"]

    profile_b = providers.ModelProfile(
        id="api:b", label="B", provider="api", upstream_model="model-b",
        enabled=True, ratios=("1:1"), resolutions=("2K"), qualities=("auto"))
    captured_b = _run_api_executor_with_fake_client(
        profile_b, generation, monkeypatch, tmp_path, models=models
    )
    assert captured_b["headers"]["X-Only-B"] == "bbb"
    assert captured_b["headers"]["X-Shared"] == "from-b"
    assert "X-Only-A" not in captured_b["headers"]


def test_execute_api_drops_default_authorization_when_header_value_is_empty(
    tmp_path, monkeypatch
):
    """执行层：值为空 ⇒ 默认 Authorization 被删掉（上游不走 Bearer 鉴权时）。"""
    models = "model-x|Model X|1:1|2K|4|true|auto|Authorization:|"
    generation = SimpleNamespace(
        id="gen-x",
        parameters_json=json.dumps({"ratio": "1:1", "resolution": "2K", "quality": "auto"}),
        references=[], original_prompt="a cat", optimized_prompt="")
    profile = providers.ModelProfile(
        id="api:x", label="X", provider="api", upstream_model="model-x",
        enabled=True, ratios=("1:1"), resolutions=("2K"), qualities=("auto"))
    captured = _run_api_executor_with_fake_client(
        profile, generation, monkeypatch, tmp_path, models=models
    )
    assert "Authorization" not in captured["headers"]
    # 非空值仍能覆盖默认头。
    models_override = "model-x|Model X|1:1|2K|4|true|auto|Authorization:Custom-x|"
    captured_override = _run_api_executor_with_fake_client(
        profile, generation, monkeypatch, tmp_path, models=models_override
    )
    assert captured_override["headers"]["Authorization"] == "Custom-x"


def test_an_api_channel_configured_by_environment_can_actually_execute(tmp_path, monkeypatch):
    """用 IMAGE_HUB_OPENAI_IMAGE_* 环境变量配的 API 通道必须能真正跑起来。

    回归：``execution_config_id`` 原先只在 ``resolved_ips`` 存在时才生成，而
    ``resolved_ips`` 只有走管理后台保存的配置才有。环境变量这条路永远拿到空串，
    执行器一进来就抛「API 任务缺少有效的冻结配置标识」—— 也就是说**用环境变量配
    API 通道从来就没跑通过**，管理后台存过一次之后才"突然好了"。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "openai_image_base_url", "https://api.example.com/v1")
    monkeypatch.setattr(settings, "openai_image_api_key", "env-provided-key")
    monkeypatch.setattr(settings, "openai_image_models", "")
    _set_dns(monkeypatch, "8.8.8.8")

    profiles = [p for p in providers.model_profiles() if p.provider == "api"]
    assert profiles, "环境变量配好了就该有内置模型可用"
    for profile in profiles:
        assert profile.enabled, profile.upstream_model
        assert profile.execution_config_id, profile.upstream_model
        # 执行器就是按这个标识取配置的；取不到任务必失败。
        loaded = providers._load_api_execution_config(profile.execution_config_id)
        assert loaded["api_key"] == "env-provided-key"


@pytest.fixture(autouse=True)
def _clear_env_api_config_cache():
    """`_ENV_API_CONFIG_IDS` 是模块级缓存，跨用例会串味（尤其环境变量的冻结标识）。

    项目已踩过「测试共用 runtime 目录互相污染」的坑，这里在每个用例前后都清空它，
    避免某用例冻结出的标识泄漏进下一个用例。
    """
    providers._ENV_API_CONFIG_IDS.clear()
    yield
    providers._ENV_API_CONFIG_IDS.clear()


# ── 逐模型请求头回显（管理后台保存时不可静默清空）──────────────────────────


def test_public_api_model_headers_echoes_only_configured_models(tmp_path, monkeypatch):
    """public_api_model_headers() 只回显配过头的模型，键是 api:<16位hex>。

    没配头的模型（model-b）不得出现在结果里；值是该模型的「名称:值;名称:值」串。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    providers.save_api_config(
        "https://api.example.com/v1",
        "fallback-secret",
        "model-a|Model A|1:1|2K|4|true|auto|X-Api-Key:abc;X-Client:xyz,"
        "model-b|Model B|1:1|2K|4|true|auto")
    headers = providers.public_api_model_headers()
    key = f"api:{providers._public_api_model_id('model-a')}"
    # 键形如 api:<16位十六进制>
    assert re.fullmatch(r"api:[0-9a-f]{16}", key)
    assert headers == {key: "X-Api-Key:abc;X-Client:xyz"}
    # 没配头的模型不应出现；值里也不该泄露真实模型 ID
    assert "model-b" not in headers
    assert "model-a" not in headers[key]


def test_public_api_model_headers_empty_for_builtin_fallback(tmp_path, monkeypatch):
    """models 串为空（回落内置模型）时，没有任何模型配过头，应返回 {}。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    providers.save_api_config("https://api.example.com/v1", "fallback-secret", "")
    assert providers.public_api_model_headers() == {}


def test_admin_providers_page_echoes_model_headers(tmp_path, monkeypatch):
    """管理后台渲染时，配过头的那一行 headers 输入框必须回显请求头串。

    核心回归：模板若把 `model_headers.get(profile.id)` 写错（比如键名漂移），
    保存别的字段时就会静默清空所有模型的请求头。这里必须把值锁进
    `data-field="headers"` 输入框的 value 里。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    providers.save_api_config(
        "https://api.example.com/v1", "fallback-secret",
        "model-a|Model A|1:1|2K|4|true|auto|X-Api-Key:abc,"
        "model-b|Model B|1:1|2K|4|true|auto")
    with TestClient(app) as client:
        _login(client)
        page = client.get("/admin/providers")
    assert page.status_code == 200
    match = re.search(r'data-field="headers"[^>]*value="([^"]*)"', page.text)
    assert match, "找不到带 value 的 headers 输入框（请求头没有被回显）"
    assert "X-Api-Key:abc" in match.group(1)


def test_model_header_with_html_special_chars_is_escaped_on_render(
    tmp_path, monkeypatch
):
    """请求头值含 HTML 特殊字符时，Jinja 必须自动转义，不能原样吐出。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    # 值里带双引号：属性值上下文里必须转义成 &#34;，不能出现裸引号。
    providers.save_api_config(
        "https://api.example.com/v1", "fallback-secret",
        'model-a|Model A|1:1|2K|4|true|auto|X-Test:a"b')
    with TestClient(app) as client:
        _login(client)
        page = client.get("/admin/providers")
    assert page.status_code == 200
    # 安全取出：未转义的原文绝不能出现在页面里
    assert 'X-Test:a"b' not in page.text
    # 但值确实被回显了（只是转义后）
    assert "X-Test:a" in page.text


def test_model_note_is_echoed_and_shipped_to_the_frontend(tmp_path, monkeypatch):
    """模型介绍（第 10 字段）要存下来、回显给管理页，并随模型列表下发给前端。

    两个方向都是坑：不回显 → 管理员改别的字段时把介绍静默清空；不进 ``public_dict``
    → 前端「可用」后面永远是空的（介绍是展示文案，必须能和请求头、路由一样往返）。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    providers.save_api_config(
        "https://api.example.com/v1", "fallback-secret",
        "model-a|Model A|1:1|2K|4|true|auto|||适合电商白底图,"
        "model-b|Model B|1:1|2K|4|true|auto")

    notes = providers.public_api_model_notes()
    assert list(notes.values()) == ["适合电商白底图"]

    profiles = {item.upstream_model: item for item in providers.model_profiles()}
    assert profiles["model-a"].note == "适合电商白底图"
    assert profiles["model-a"].public_dict()["note"] == "适合电商白底图"
    # 没填介绍的模型是空串而不是 None：前端直接拼字符串，不需要再判空类型。
    assert profiles["model-b"].note == ""

    with TestClient(app) as client:
        _login(client)
        page = client.get("/admin/providers")
    assert page.status_code == 200
    match = re.search(r'data-field="note"[^>]*value="([^"]*)"', page.text)
    assert match, "找不到带 value 的 note 输入框（模型介绍没有被回显）"
    assert "适合电商白底图" in match.group(1)


def test_public_api_model_notes_empty_for_builtin_fallback(tmp_path, monkeypatch):
    """models 串为空（回落内置模型）时没人填过介绍，应返回 {} 而不是报错。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    providers.save_api_config("https://api.example.com/v1", "fallback-secret", "")
    assert providers.public_api_model_notes() == {}


# ── 环境变量路径的冻结标识（纯 .env 配置也要能跑）──────────────────────────


def test_env_only_api_profiles_all_share_a_nonempty_execution_config_id(
    tmp_path, monkeypatch
):
    """纯环境变量配置（无 provider-config.json）时，每个 api profile 都必须拿到
    非空的 execution_config_id，且彼此一致（同一份配置 => 同一个冻结标识）。

    回归：历史上这个标识只在带 resolved_ips 的后台配置里才生成，纯 .env 配置
    永远拿到空串，提交后必失败于「API 任务缺少有效的冻结配置标识」。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "openai_image_base_url", "https://api.example.com/v1")
    monkeypatch.setattr(settings, "openai_image_api_key", "env-provided-key")
    monkeypatch.setattr(settings, "openai_image_models", "")
    _set_dns(monkeypatch, "8.8.8.8")
    profiles = [p for p in providers.model_profiles() if p.provider == "api"]
    assert profiles, "环境变量配好了就该有内置模型可用"
    ids = {p.execution_config_id for p in profiles}
    assert all(p.execution_config_id for p in profiles)
    assert len(ids) == 1


def test_env_api_freeze_file_is_written_and_loadable(tmp_path, monkeypatch):
    """冻结文件要真落到 storage_root 下（含 resolved_ips），且能成功加载回 base_url。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "openai_image_base_url", "https://api.example.com/v1")
    monkeypatch.setattr(settings, "openai_image_api_key", "env-provided-key")
    monkeypatch.setattr(settings, "openai_image_models", "")
    _set_dns(monkeypatch, "8.8.8.8")
    profiles = [p for p in providers.model_profiles() if p.provider == "api"]
    identity = profiles[0].execution_config_id
    assert identity
    frozen_path = tmp_path / "provider-configs" / f"{identity}.json"
    assert frozen_path.is_file()
    loaded = providers._load_api_execution_config(identity)
    assert loaded["base_url"] == "https://api.example.com/v1"
    assert loaded["resolved_ips"]


@pytest.mark.parametrize(
    ("base_url", "api_key"),
    [("", "k"), ("https://api.example.com/v1", ""), ("", "")])
def test_missing_api_key_or_base_url_yields_empty_execution_config_id(
    tmp_path, monkeypatch, base_url, api_key
):
    """缺 base_url 或 api_key 时返回空串，且不能被误缓存成非空。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "openai_image_base_url", base_url)
    monkeypatch.setattr(settings, "openai_image_api_key", api_key)
    monkeypatch.setattr(settings, "openai_image_models", "")
    _set_dns(monkeypatch, "8.8.8.8")
    profiles = [p for p in providers.model_profiles() if p.provider == "api"]
    assert profiles
    assert all(not p.execution_config_id for p in profiles)


# ── API 通道：同模型多 API 路由（模型串第 9 字段）───────────────────────────

#: 两条路由刻意选了不同的族与异步声明：route-a 是 Grsai 原生（可异步），route-b
ROUTE_LIST = (
    "route-a|https://a.example.test/v1|key-a|grsai_native|true|true\n"
    "route-b|https://b.example.test/v1|key-b|openai_compat||true"
)
#: model-a 选了 route-a（人工指定，只能一条），model-b 没选（走默认路由）。
ROUTED_MODELS = (
    "model-a|Model A|1:1|2K|4|true|auto||route-a,"
    "model-b|Model B|1:1|2K|4|true|auto"
)


def _save_routed_api_config(models: str, routes: str = ROUTE_LIST) -> None:
    providers.save_api_config(
        "https://default.example.test/v1", "key-default", models, routes
    )


def test_api_route_field_selects_exactly_one_route(tmp_path, monkeypatch):
    """第 9 字段 = 本模型走的那**一条**路由（人工指定）；留空 = 走默认路由。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    _save_routed_api_config(ROUTED_MODELS)
    parsed = {
        item["model_id"]: item
        for item in providers._parse_api_models(providers._api_config()["models"])
    }
    assert parsed["model-a"]["route"] == "route-a"
    assert parsed["model-b"]["route"] == ""
    routes = {
        item["name"]: item for item in providers._api_routes(providers._api_config())
    }
    assert set(routes) == {"route-a", "route-b"}
    assert routes["route-a"]["resolved_ips"]
    assert routes["route-a"]["family"] == "grsai_native"
    assert routes["route-b"]["family"] == "openai_compat"
    assert routes["route-b"]["async"] is False


def test_frozen_config_follows_the_selected_route(tmp_path, monkeypatch):
    """冻结的是**选中的那一条**路由：地址、密钥、族、异步全跟着它走。

    默认路由（第 9 字段留空）必须逐字节保持旧行为 —— 旧部署的冻结标识不会因为这次
    改动而漂移。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    _save_routed_api_config(ROUTED_MODELS)
    default_id = providers._freeze_api_execution_config("model-b", "")
    loaded_default = providers._load_api_execution_config(default_id)
    assert loaded_default["base_url"] == "https://default.example.test/v1"
    assert loaded_default["api_key"] == "key-default"
    assert not loaded_default.get("family")
    routed_id = providers._freeze_api_execution_config("model-a", "route-a")
    assert routed_id != default_id
    loaded = providers._load_api_execution_config(routed_id)
    assert loaded["base_url"] == "https://a.example.test/v1"
    assert loaded["api_key"] == "key-a"
    assert loaded["route"] == "route-a"
    assert loaded["family"] == "grsai_native"
    assert loaded["async"] is True and loaded["async_declared"] is True


def test_api_model_referencing_an_undefined_route_is_rejected(tmp_path, monkeypatch):
    """引用不存在的路由是**静默故障**：模型看着正常、执行时才找不到路，保存就该拒。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    with pytest.raises(providers.ProviderConfigError) as excinfo:
        _save_routed_api_config("model-a|Model A|1:1|2K|4|true|auto||ghost")
    assert "未定义的路由" in str(excinfo.value)


def test_async_route_must_be_a_grsai_native_endpoint(tmp_path, monkeypatch):
    """OpenAI 兼容端点没有可轮询的接口，标成异步只会让任务卡到超时 —— 保存即拒。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    with pytest.raises(providers.ProviderConfigError) as excinfo:
        _save_routed_api_config(
            "model-a|Model A|1:1|2K|4|true|auto||route-a",
            "route-a|https://a.example.test/v1|key-a|openai_compat|true|true")
    assert "不支持异步" in str(excinfo.value)


def test_api_route_keys_survive_a_save_that_leaves_them_blank(tmp_path, monkeypatch):
    """管理页不回填密钥：留空 = 保持该路由的现有密钥；字段缺失（None）不删路由。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    _save_routed_api_config(ROUTED_MODELS)

    providers.save_api_config(
        "https://default.example.test/v1", "new-default-key", ROUTED_MODELS
    )
    routes = {
        item["name"]: item for item in providers._api_routes(providers._api_config())
    }
    assert set(routes) == {"route-a", "route-b"}
    assert routes["route-a"]["api_key"] == "key-a"
    assert providers._api_config()["api_key"] == "new-default-key"

    # 页面回显原文（密钥一律留空）再原样提交：按同名继承，密钥不能丢。
    providers.save_api_config(
        "https://default.example.test/v1", "", ROUTED_MODELS, providers.api_routes_text()
    )
    routes = {
        item["name"]: item for item in providers._api_routes(providers._api_config())
    }
    assert routes["route-a"]["api_key"] == "key-a"
    assert routes["route-b"]["api_key"] == "key-b"
    assert providers._api_config()["api_key"] == "new-default-key"


def test_admin_page_echoes_routes_but_never_a_route_key(tmp_path, monkeypatch):
    """路由清单与模型行第 9 字段都要回显 —— 否则「改个别的字段再保存」会静默删掉路由。

    回显的是脱敏视图：地址/族/异步/启停原样带回来，**密钥一律留空**（留空 = 保持）。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    _save_routed_api_config(ROUTED_MODELS)
    with TestClient(app) as client:
        _login(client)
        page = client.get("/admin/providers").text
    assert 'name="api_routes"' in page
    assert "route-a|https://a.example.test/v1||grsai_native|true|true" in page
    assert "route-b|https://b.example.test/v1||openai_compat||true" in page
    # 模型行第 9 字段：选中的那条必须**选中**地回来，否则「改个别的字段再保存」
    assert 'data-field="routes"' in page
    assert '<option value="route-a" selected>' in page
    assert '<option value="route-b" selected>' not in page
    for secret in ("key-a", "key-b", "key-default", "not-a-real"):
        assert secret not in page


def test_admin_page_shows_route_states_and_clears_a_mark(tmp_path, monkeypatch):
    """失效标记要能看见、能手工清除；清除只是**放行这条路由**，不改任何模型的选择。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    _save_routed_api_config(ROUTED_MODELS)
    with SessionLocal() as session:
        providers.api_routing.mark_exhausted(
            session, "model-a", "route-a", "insufficient balance"
        )
        session.commit()

    with TestClient(app) as client:
        token = _login(client)
        page = client.get("/admin/providers").text
        # 上游原文要显示出来：校准识别词表靠的就是它，而不是我们自己转述的文案。
        assert "insufficient balance" in page
        assert "额度耗尽" in page
        assert 'action="/admin/providers/api/routes/clear_exhausted"' in page
        # 表格回填的是**公开**模型 id，真实上游模型名不外传。
        public_id = f"api:{providers._public_api_model_id('model-a')}"
        assert public_id in page
        assert "model-a" not in page

        response = client.post(
            "/admin/providers/api/routes/clear_exhausted",
            data={"csrf": token, "model": public_id, "route": "route-a"},
            follow_redirects=False)
        assert response.status_code == 303
        assert "route-a 的额度耗尽标记已清除" in client.get("/admin/providers").text

        # 不属于该模型的路由：如实报错，绝不退化成「把这个模型全部标记清掉」。
        client.post(
            "/admin/providers/api/routes/clear_exhausted",
            data={"csrf": token, "model": public_id, "route": "ghost"},
            follow_redirects=False)
        assert "该路由不属于这个模型" in client.get("/admin/providers").text

        # 认不出的模型（配置改过之后的旧 id）：同样只报错，不按「全清」处理。
        client.post(
            "/admin/providers/api/routes/clear_exhausted",
            data={"csrf": token, "model": "api:deadbeefdeadbeef", "route": ""},
            follow_redirects=False)
        assert "不存在或已被移除" in client.get("/admin/providers").text
    with SessionLocal() as session:
        states = providers.api_routing.route_states(session, "model-a", ("route-a",))
        assert states[0]["exhausted"] is False
        # 清除只把关，不替管理员做决定：模型选定的那条**依旧**是它，不会因为清标记
        assert providers.api_routing.route_available(
            session, "model-a", "route-a", enabled=("route-a", "route-b")
        ) == "route-a"
        assert providers.routed_api_routes(session)["model-a"] == "route-a"


def test_public_model_id_round_trips_to_the_real_model_id(tmp_path, monkeypatch):
    """表单回传的是公开 id，反查必须命中同一个模型；改过配置的旧 id 必须失配。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    _save_routed_api_config(ROUTED_MODELS)
    public_id = f"api:{providers._public_api_model_id('model-a')}"
    assert providers.api_model_id_for_public(public_id) == "model-a"
    assert providers.api_model_id_for_public("api:0000000000000000") == ""
    assert providers.api_model_id_for_public("model-a") == ""


def test_unresolvable_env_endpoint_returns_empty_instead_of_raising(tmp_path, monkeypatch):
    """域名校验失败（ProviderConfigError）时返回空串而非抛异常。

    不能让模型列表整个 500 —— 这条通道只要降级为不可用即可。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "api_private_network_allowlist", "")
    _set_dns(monkeypatch, "127.0.0.1")
    config = {
        "base_url": "https://api.example.com/v1",
        "api_key": "env-provided-key",
        "models": "",
    }
    assert providers._api_execution_config_id(config) == ""


def test_env_api_execution_config_id_is_cached_across_calls(tmp_path, monkeypatch):
    """同一份配置第二次调用应命中缓存，不再触发 _freeze_api_execution_config。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "openai_image_base_url", "https://api.example.com/v1")
    monkeypatch.setattr(settings, "openai_image_api_key", "env-provided-key")
    monkeypatch.setattr(settings, "openai_image_models", "")
    _set_dns(monkeypatch, "8.8.8.8")
    calls = {"n": 0}
    original = providers._freeze_api_execution_config

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(providers, "_freeze_api_execution_config", counting)
    config = {
        "base_url": "https://api.example.com/v1",
        "api_key": "env-provided-key",
        "models": "",
    }
    first = providers._api_execution_config_id(config)
    second = providers._api_execution_config_id(config)
    assert first
    assert first == second
    assert calls["n"] == 1  # 第二次命中缓存，没有再冻结


def test_an_unresolvable_environment_endpoint_disables_the_channel_not_the_app(
    tmp_path, monkeypatch
):
    """环境变量指到内网/不可解析的地址时，通道降级为不可用，而不是让页面 500。

    补上上面那条修复的同时不能把 DNS 重绑定保护一起丢了：后台保存的配置自带
    ``resolved_ips``，环境变量的没有，所以校验是在这里补做的 —— 这里验证补做的
    那一环确实会拦住内网地址。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "openai_image_base_url", "https://api.example.com/v1")
    monkeypatch.setattr(settings, "openai_image_api_key", "env-provided-key")
    monkeypatch.setattr(settings, "openai_image_models", "")
    _set_dns(monkeypatch, "127.0.0.1")

    profiles = [p for p in providers.model_profiles() if p.provider == "api"]
    assert profiles
    # 不抛异常（页面照常渲染），但拿不到冻结标识，通道因此不可用。
    assert all(not p.execution_config_id for p in profiles)


# ── API 通道：瞬时/永久错误重试、异步提交+轮询、replyType、轮询超时 ──────


def _run_api_executor(
    generation, profile, monkeypatch, tmp_path, responder, *, reply_mode="sync", models="", poll_timeout=600
):
    """用可定制的假 httpx.Client 驱动 ``_execute_api``，返回捕获到的请求记录。

    ``responder(url, json, call_index)`` 必须返回 ``(status_code, payload)``；
    ``call_index`` 是「同一 Client 实例」内的第几次 POST/GET（从 1 开始）。
    下载走 GET（``json is None``），提交与轮询走 POST。

    ``poll_timeout`` 用来改 ``provider_command_timeout_seconds``（一次任务的**总**
    预算）。轮询的实际 deadline 是 ``min(总预算, 现在 + api_poll_timeout_seconds)``，
    所以把它调小同样能让轮询立刻超时。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "api_reply_mode", reply_mode)
    monkeypatch.setattr(settings, "provider_command_timeout_seconds", poll_timeout)
    monkeypatch.setattr(
        providers, "_load_api_execution_config",
        lambda _id: {"base_url": "https://api.test/v1", "api_key": "k", "models": models})
    # 这条路径的空闲复查会真连上游；本组只测提交/轮询，禁掉它。
    monkeypatch.setattr(providers, "recover_generation", lambda _id: (False, "skipped"))
    captured = {"posts": [], "gets": []}

    class _Resp:
        def __init__(self, payload=None, content=PNG_BYTES, status_code=200):
            self._payload = payload or {}
            self.content = content
            self.status_code = status_code

        @property
        def headers(self):
            return {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield self.content

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            self._n = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            self._n += 1
            captured["posts"].append({"url": url, "headers": headers, "body": json})
            status, payload = responder(url, json, self._n)
            return _Resp(payload=payload, status_code=status)

        def get(self, url):
            captured["gets"].append(url)
            status, payload = responder(url, None, 0)
            return _Resp(content=PNG_BYTES, payload=payload, status_code=status)

        def stream(self, method, url):
            """同 ``_FakeClient.stream``：补上流式接口，但不触发地址守卫。"""
            assert method == "GET"
            captured["gets"].append(url)
            status, payload = responder(url, None, 0)
            return _Resp(content=PNG_BYTES, payload=payload, status_code=status)

    monkeypatch.setattr(providers.httpx, "Client", _Client)
    providers._execute_api(generation, profile)
    return captured


def _builtin_profile(upstream_model="nano-banana-2-lite"):
    """一个走 Grsai 原生端点的内置模型 profile（ratio_tier，无质量维度）。"""
    return providers.ModelProfile(
        id="api:nb", label="M", provider="api", upstream_model=upstream_model,
        enabled=True, ratios=("1:1"), resolutions=("2K"), qualities=())


def _sync_generation(generation_id="gen-x"):
    return SimpleNamespace(
        id=generation_id,
        parameters_json=json.dumps({"ratio": "1:1", "resolution": "2K", "quality": "standard"}),
        references=[], original_prompt="a cat", optimized_prompt="")


def test_api_transient_overload_retries_with_backoff(tmp_path, monkeypatch):
    """A4. 瞬时错误（上游过载）会退避重试：N 次失败后第 N+1 次成功，sleep 序列 = backoff*n。

    ⚠️ 不真睡：``providers.time.sleep`` 被换成记录器（轮询循环也因此不耗时）。
    """
    monkeypatch.setattr(settings, "api_reply_mode", "sync")
    monkeypatch.setattr(settings, "api_submit_retries", 3)
    monkeypatch.setattr(settings, "api_retry_backoff_seconds", 5.0)
    sleeps: list[float] = []
    monkeypatch.setattr(providers.time, "sleep", lambda d: sleeps.append(d))

    n_fail = 3  # 总尝试 = 1 + 3 = 4，前 3 次失败、第 4 次成功

    def responder(url, json, idx):
        if json is None:  # 下载 GET
            return (200, {})
        if idx <= n_fail:
            return (400, {"status": "failed", "error": "The model load is too high, please try again later"})
        return (200, {"data": [{"url": "https://cdn.test/r.png"}]})

    _set_dns(monkeypatch, "8.8.8.8")
    captured = _run_api_executor(_sync_generation(), _builtin_profile(), monkeypatch, tmp_path, responder)
    # 1 次首提 + 3 次重试 = 4 次提交
    assert len(captured["posts"]) == n_fail + 1
    # 退避序列：第 n 次退避 = backoff * n（仅对失败的那几次）
    assert sleeps == [5.0, 10.0, 15.0]


def test_api_transient_overload_retries_with_backoff_lands_artifact(tmp_path, monkeypatch):
    """A4 的落盘补充：成功响应后产物确实写到 storage_root 下。"""
    monkeypatch.setattr(settings, "api_reply_mode", "sync")
    monkeypatch.setattr(settings, "api_submit_retries", 3)
    monkeypatch.setattr(settings, "api_retry_backoff_seconds", 5.0)
    monkeypatch.setattr(providers.time, "sleep", lambda d: None)

    n_fail = 3

    def responder(url, json, idx):
        if json is None:
            return (200, {})
        if idx <= n_fail:
            return (400, {"status": "failed", "error": "The model load is too high, please try again later"})
        return (200, {"data": [{"url": "https://cdn.test/r.png"}]})

    generation = _sync_generation("gen-a4-land")
    _set_dns(monkeypatch, "8.8.8.8")
    _run_api_executor(generation, _builtin_profile(), monkeypatch, tmp_path, responder)
    assert generation.artifact_storage_key
    assert (tmp_path / generation.artifact_storage_key).is_file()


def test_api_permanent_error_does_not_retry(tmp_path, monkeypatch):
    """A5. 永久错误（额度不足）不重试：只调一次 post，抛 RuntimeError 且不是 ApiTransientError。"""
    monkeypatch.setattr(settings, "api_reply_mode", "sync")
    posts: list = []

    def responder(url, json, idx):
        posts.append(json)
        if json is None:
            return (200, {})
        return (400, {"error": "apikey credits not enough"})

    _set_dns(monkeypatch, "8.8.8.8")
    with pytest.raises(RuntimeError) as exc:
        _run_api_executor(_sync_generation(), _builtin_profile(), monkeypatch, tmp_path, responder)
    # 抛的是普通 RuntimeError，不是可重试的 ApiTransientError
    assert not isinstance(exc.value, providers.ApiTransientError)
    # 文案带上游原文，方便排查
    assert "apikey credits not enough" in str(exc.value)
    # 提交只发了一次（无重试）；下载是 GET 不计入 posts
    assert sum(1 for j in posts if j is not None) == 1


def test_api_async_submit_then_poll_to_succeeded(tmp_path, monkeypatch):
    """A6. 异步：提交拿 id → 轮询到 succeeded，产物落盘，且轮询打到 {base}/api/result。

    同样 patch 掉 sleep（不真睡）。
    """
    monkeypatch.setattr(providers.time, "sleep", lambda d: None)
    poll_state = {"n": 0}

    def responder(url, json, idx):
        if json is None:
            return (200, {})
        if url.endswith("/api/result"):
            poll_state["n"] += 1
            if poll_state["n"] == 1:
                return (200, {"id": "t1", "status": "running", "progress": 40})
            return (200, {"id": "t1", "status": "succeeded", "results": [{"url": "https://cdn.test/r.png"}]})
        # 提交：返回 running 的 task id
        return (200, {"id": "t1", "status": "running"})

    generation = _sync_generation("gen-a6")
    _set_dns(monkeypatch, "8.8.8.8")
    captured = _run_api_executor(generation, _builtin_profile(), monkeypatch, tmp_path, responder, reply_mode="async")

    submit = next(p for p in captured["posts"] if p["url"].endswith("/api/generate"))
    assert submit["body"]["replyType"] == "async"
    # 轮询地址必须**精确**是 {api_base}/api/result。
    poll_urls = [p["url"] for p in captured["posts"] if "/api/result" in p["url"]]
    assert poll_urls, "异步路径必须发出轮询请求"
    assert set(poll_urls) == {"https://api.test/v1/api/result"}, poll_urls
    # 最终产物落盘
    assert generation.artifact_storage_key
    assert (tmp_path / generation.artifact_storage_key).is_file()


def test_api_async_submit_without_id_raises(tmp_path, monkeypatch):
    """A7. 异步提交返回 200 但没有 id → 抛 RuntimeError 且文案提到「未返回任务 ID」。

    不能静默当成排队中等下去（否则会一直轮询到超时）。
    """
    monkeypatch.setattr(providers.time, "sleep", lambda d: None)

    def responder(url, json, idx):
        if json is None:
            return (200, {})
        # 200 但没有 id
        return (200, {"status": "running"})

    _set_dns(monkeypatch, "8.8.8.8")
    with pytest.raises(RuntimeError) as exc:
        _run_api_executor(_sync_generation(), _builtin_profile(), monkeypatch, tmp_path, responder, reply_mode="async")
    assert not isinstance(exc.value, providers.ApiTransientError)
    assert "未返回任务 ID" in str(exc.value)


def test_reply_type_follows_mode_and_skips_openai_branch(tmp_path, monkeypatch):
    """A8. replyType 随模式切换；非内置（OpenAI 兼容）分支两种模式都不带 replyType。

    那条分支没有 /api/result，不能被异步模式误伤成去轮询。
    """
    monkeypatch.setattr(providers.time, "sleep", lambda d: None)

    def responder(url, json, idx):
        if json is None:
            return (200, {})
        if url.endswith("/api/result"):
            return (200, {"id": "t1", "status": "succeeded", "results": [{"url": "https://cdn.test/r.png"}]})
        return (200, {"id": "t1", "status": "running", "data": [{"url": "https://cdn.test/r.png"}]})

    def run(upstream_model, reply_mode):
        profile = providers.ModelProfile(
            id="api:x", label="M", provider="api", upstream_model=upstream_model,
            enabled=True, ratios=("1:1"), resolutions=("2K"), qualities=())
        generation = _sync_generation(f"gen-{upstream_model}-{reply_mode}")
        _set_dns(monkeypatch, "8.8.8.8")
        captured = _run_api_executor(generation, profile, monkeypatch, tmp_path, responder, reply_mode=reply_mode)
        return captured["posts"][0]["body"]

    # 内置 Grsai 模型：sync -> "json"，async -> "async"
    assert run("nano-banana-2-lite", "sync")["replyType"] == "json"
    assert run("nano-banana-2-lite", "async")["replyType"] == "async"
    # 非内置（OpenAI 兼容，如 doubao）：两种模式都不带 replyType
    assert "replyType" not in run("doubao-seedream-5-0-pro-260628", "sync")
    assert "replyType" not in run("doubao-seedream-5-0-pro-260628", "async")


def test_api_poll_timeout_is_not_retried_and_records_the_task_id(tmp_path, monkeypatch):
    """A9. 轮询超时 → **不重提**，并把上游 task id 记到行上。

    为什么不重提：此刻上游的任务**已经创建**、可能已经在计费；重提会再建一个任务，
    等于为同一张图付两次钱 —— 与 worker「租约失效不自动重跑，因为上一次可能已经
    在计费」是同一条约定。所以超时抛的是 ``ApiPollTimeoutError``，**故意不是**
    ``ApiTransientError``（后者会走退避重提），并且把 ``external_task_id`` 写进行里：
    ``execute_generation`` 见到这个字段非空就判成 ``recovery_required`` 而不是
    ``failed`` —— 既不退款也不盲目重跑，留给 ``/admin/tasks`` 人工收敛。

    ⚠️ 轮询 deadline = ``min(provider_command_timeout_seconds, 现在 + api_poll_timeout_seconds)``。
    这里把总预算（``poll_timeout``）调极小、再把 ``time.monotonic`` 换成跳变时钟，
    让第一次轮询就判定超时。
    """
    monkeypatch.setattr(providers.time, "sleep", lambda d: None)
    clock = {"t": 1000.0}

    def fake_monotonic():
        clock["t"] += 10.0  # 每次调用跳 10s，第一次轮询即可越过 deadline
        return clock["t"]

    monkeypatch.setattr(providers.time, "monotonic", fake_monotonic)

    submits = {"n": 0}

    def responder(url, json, idx):
        if json is None:
            return (200, {})
        if url.endswith("/api/result"):
            return (200, {"id": "t1", "status": "running", "progress": 10})
        submits["n"] += 1
        return (200, {"id": "t1", "status": "running"})

    _set_dns(monkeypatch, "8.8.8.8")
    generation = _sync_generation()
    with pytest.raises(providers.ApiPollTimeoutError) as exc:
        _run_api_executor(
            generation, _builtin_profile(), monkeypatch, tmp_path, responder,
            reply_mode="async", poll_timeout=0.05)
    assert "超时" in str(exc.value)
    assert exc.value.task_id == "t1"
    # 没有重提（提交只发生了一次）——这正是双重扣费防线。
    assert submits["n"] == 1
    # task id 落进行里，任务才会进 recovery_required 而不是被判定失败并退款。
    assert generation.external_task_id == "t1"

