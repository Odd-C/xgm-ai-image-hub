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
# b"png" would be rejected as "上游返回的结果不是有效图片".
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
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Logging in rotates the CSRF token, so the pre-login value is dead. Read
    # the live token off an authenticated page instead.
    match = re.search(r'name="csrf" value="([^"]+)"', client.get("/admin").text)
    return match.group(1) if match else token


def _dns_result(address: str):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]


def _set_dns(monkeypatch, address: str) -> None:
    monkeypatch.setattr(
        providers.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _dns_result(address),
    )


@pytest.mark.parametrize(
    ("models", "message"),
    [
        ("model-a|Model A|2:1|2K|4|true|standard", "不支持的比例"),
        ("model-a|Model A|1:1|8K|4|true|standard", "不支持的分辨率"),
        ("model-a|Model A|1:1|2K|4|true|ultra", "不支持的质量"),
        (
            "model-a|Model A|1:1|2K|4|true|standard,"
            + "model-a|Model B|1:1|2K|4|true|standard",
            "不得重复",
        ),
        ("model-a|Model A|1:1|2K|4|true|standard|extra", "格式无效"),
        ("model-a|Model A|1:1|2K|-1|true|standard", "0 到 14"),
        ("model-a|Model A|1:1|2K|15|true|standard", "0 到 14"),
    ],
)
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
            },
        )
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
            },
        )
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
                "models": "model-a|Model A|1:1;16:9|1K;4K|14|true|standard",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303


@pytest.mark.parametrize(
    "parameters",
    [
        {"ratio": "2:1", "resolution": "2K", "quality": "standard"},
        {"ratio": "1:1", "resolution": "8K", "quality": "standard"},
        {"ratio": "1:1", "resolution": "2K", "quality": "ultra"},
    ],
)
def test_api_executor_rejects_unknown_stored_capabilities(parameters):
    profile = providers.ModelProfile(
        id="api:opaque",
        label="Model A",
        provider="api",
        upstream_model="private-model-a",
        enabled=True,
        ratios=("1:1", "2:1"),
        resolutions=("2K", "8K"),
        qualities=("standard", "ultra"),
    )
    generation = SimpleNamespace(
        parameters_json=json.dumps(parameters),
        references=[],
        original_prompt="test",
    )
    with pytest.raises(RuntimeError, match="执行器不支持"):
        providers._execute_api(generation, profile)


def test_private_json_files_are_atomic_and_private_from_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")

    providers.save_api_config(
        "https://images.example.test/v1",
        "not-a-real-key",
        "private-model-a|Model A|1:1|2K|4|true|standard",
    )
    identity = providers._freeze_api_execution_config()

    paths = [
        tmp_path / "provider-config.json",
        tmp_path / "provider-configs" / f"{identity}.json",
    ]
    for path in paths:
        # POSIX exposes the 0600 mode directly. Windows has no mode bits to read
        # back (os.chmod only toggles the read-only flag), so the mode guarantee
        # is only asserted where the platform can express it.
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
    ["127.0.0.1", "169.254.10.20", "192.0.2.10", "10.20.30.40"],
)
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
        "10.20.30.40",
    )


def test_execution_config_rejects_dns_rebinding(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "api_private_network_allowlist", "")
    _set_dns(monkeypatch, "8.8.8.8")
    config = providers._validated_api_config(
        "https://images.example.test/v1",
        "not-a-real-key",
        "private-model-a|Model A|1:1|2K|4|true|standard",
    )
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
        f"{upstream_model}|Public Model|1:1|2K|4|true|standard",
    )

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
            follow_redirects=False,
        )
        project_id = project_response.headers["location"].rsplit("/", 1)[-1]
        with SessionLocal() as session:
            user = session.query(User).filter_by(username="admin").one()
            generation = Generation(
                user_id=user.id,
                project_id=project_id,
                idempotency_key="secret-history-row",
                original_prompt="test",
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
                error_message=f"{secret} {base_url} {upstream_model} {environment_name}",
            )
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


def test_admin_stores_skill_paths_and_rejects_unsafe_values(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    scripts_dir, lovart_script = _fake_skill_layout(tmp_path)

    assert providers.save_libtv_credentials("probe-token", str(scripts_dir)) == "saved"
    assert (
        providers.save_lovart_credentials("probe-ak", "probe-sk", str(lovart_script))
        == "saved"
    )
    public = providers.public_native_credentials()
    assert public["has_libtv_token"] is True
    assert public["has_lovart_access_key"] is True
    assert str(scripts_dir) in public["libtv_scripts_label"]

    # A relative dir, a missing dir, and a non-.py script are all refused.
    with pytest.raises(providers.ProviderConfigError):
        providers.save_libtv_credentials("", "relative/scripts")
    with pytest.raises(providers.ProviderConfigError):
        providers.save_libtv_credentials("", str(tmp_path / "does-not-exist"))
    with pytest.raises(providers.ProviderConfigError):
        providers.save_lovart_credentials("probe-ak", "probe-sk", str(tmp_path / "x.txt"))


def test_native_profiles_are_only_enabled_when_credentials_and_scripts_agree(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    scripts_dir, lovart_script = _fake_skill_layout(tmp_path)
    # Point the environment fallback at a directory that cannot exist, so the
    # assertions below describe the admin-managed contract only and do not
    # depend on whatever skills happen to be installed on the machine.
    monkeypatch.setattr(settings, "libtv_skill_scripts", tmp_path / "absent-libtv")
    monkeypatch.setattr(settings, "lovart_skill_script", tmp_path / "absent.py")
    monkeypatch.setattr(settings, "libtv_access_key", "")
    monkeypatch.setattr(settings, "lovart_access_key", "")
    monkeypatch.setattr(settings, "lovart_secret_key", "")

    def status():
        profiles = providers.model_profiles()
        return {
            provider: [p.enabled for p in profiles if p.provider == provider]
            for provider in ("libtv", "lovart")
        }

    # Nothing configured yet -> nothing enabled.
    assert not any(status()["libtv"])
    assert not any(status()["lovart"])

    # Credentials alone are not enough; the scripts must exist too.
    providers.save_libtv_credentials("probe-token")
    providers.save_lovart_credentials("probe-ak", "probe-sk")
    assert not any(status()["libtv"])
    assert not any(status()["lovart"])

    providers.save_libtv_credentials("", str(scripts_dir))
    providers.save_lovart_credentials("", "", str(lovart_script))
    assert all(status()["libtv"])
    assert all(status()["lovart"])

    # A blank submit is a no-op, so a fully configured contract stays enabled.
    assert providers.save_libtv_credentials("", "") == "kept"
    assert providers.save_lovart_credentials("", "", "") == "kept"
    assert all(status()["libtv"])
    assert all(status()["lovart"])


def test_clearing_native_configuration_removes_token_and_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    scripts_dir, lovart_script = _fake_skill_layout(tmp_path)
    providers.save_libtv_credentials("probe-token", str(scripts_dir))
    providers.save_lovart_credentials("probe-ak", "probe-sk", str(lovart_script))

    assert providers.save_libtv_credentials("", "", clear=True) == "cleared"
    assert providers.save_lovart_credentials("", "", "", clear=True) == "cleared"

    public = providers.public_native_credentials()
    assert public["has_libtv_token"] is False
    assert public["has_lovart_access_key"] is False
    assert public["libtv_source"] == "未配置"
    assert public["lovart_source"] == "未配置"

    # A clear request must not smuggle a new value in at the same time.
    with pytest.raises(providers.ProviderConfigError):
        providers.save_libtv_credentials("sneaky-token", "", clear=True)


def test_blank_submit_keeps_stored_credentials_instead_of_wiping_them(
    tmp_path, monkeypatch
):
    """Pressing save with an empty password field must be a no-op.

    Regression: the "kept" branch used to write the payload back anyway, so a
    plain mis-click with an empty password input replaced a working credential
    file with an empty object — silently disabling every native provider.
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    scripts_dir, lovart_script = _fake_skill_layout(tmp_path)

    providers.save_libtv_credentials("probe-token", str(scripts_dir))
    providers.save_lovart_credentials("probe-ak", "probe-sk", str(lovart_script))
    credential_file = tmp_path / "private" / "native-provider-credentials.json"
    before = credential_file.read_text(encoding="utf-8")

    # Both routes submit every field, so an untouched password box arrives as "".
    assert providers.save_libtv_credentials("", "") == "kept"
    assert providers.save_lovart_credentials("", "", "") == "kept"

    assert credential_file.read_text(encoding="utf-8") == before
    public = providers.public_native_credentials()
    assert public["has_libtv_token"] is True
    assert public["has_lovart_access_key"] is True
    assert public["has_lovart_secret_key"] is True
    assert all(p.enabled for p in providers.model_profiles() if p.provider == "libtv")
    assert all(p.enabled for p in providers.model_profiles() if p.provider == "lovart")

    # A blank submit against a clean slate must not create an empty file either.
    credential_file.unlink()
    assert providers.save_libtv_credentials("", "") == "kept"
    assert not credential_file.exists()


def test_libtv_scripts_are_resolved_from_private_config_then_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    scripts_dir, _ = _fake_skill_layout(tmp_path)
    fallback = tmp_path / "env-scripts"
    fallback.mkdir()
    monkeypatch.setattr(settings, "libtv_skill_scripts", fallback)

    assert providers._resolved_libtv_scripts_dir()[0] == fallback
    providers.save_libtv_credentials("", str(scripts_dir))
    resolved, source = providers._resolved_libtv_scripts_dir()
    assert resolved == scripts_dir
    assert source == "管理后台已配置"
    assert settings.libtv_skill_scripts == fallback  # the frozen config must not mutate


def test_skill_script_runner_passes_arguments_and_env_and_parses_noisy_output(
    tmp_path, monkeypatch
):
    """Drive a real script through the same runner the executors use."""
    script = tmp_path / "probe_skill.py"
    script.write_text(
        "import json, os, sys\n"
        "print('log line that is not json')\n"
        "print(json.dumps({'argv': sys.argv[1:], 'key': os.environ.get('LIBTV_ACCESS_KEY', '')}))\n",
        encoding="utf-8",
    )
    payload = providers._parse_skill_json(
        providers._run_skill_script(
            script,
            ["query_session", "--after-seq", "3"],
            env_overrides={"LIBTV_ACCESS_KEY": "probe-key"},
            timeout=30,
            script_dir=script,
        ),
        "unreachable",
    )
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
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError) as excinfo:
        providers._run_skill_script(
            script, [], env_overrides={"LIBTV_ACCESS_KEY": secret}, timeout=30,
            secret_values=(secret,),
        )
    message = str(excinfo.value)
    assert secret not in message
    assert "[已隐藏]" in message


def test_skill_script_paths_cannot_escape_the_configured_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    scripts_dir, _ = _fake_skill_layout(tmp_path)
    providers.save_libtv_credentials("probe-token", str(scripts_dir))
    config = providers._current_native_execution_config("libtv")
    with pytest.raises(RuntimeError):
        providers._libtv_script_path("../../etc/passwd")
    assert providers._libtv_scripts(config) == scripts_dir


def test_libtv_message_urls_collects_images_and_videos():
    messages = [
        {"role": "user", "content": "\u751f\u4e00\u5f20\u56fe"},
        {"role": "assistant", "content": "\u597d\u4e86 https://libtv-res.liblib.art/claw/x/a.png"},
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "task_result": {
                        "images": [{"previewPath": "https://libtv-res.liblib.art/claw/x/b.jpg"}],
                        "videos": [{"previewPath": "https://libtv-res.liblib.art/claw/x/c.mp4"}],
                    }
                }
            ),
        },
        {"role": "assistant", "content": "https://example.test/not-a-result.png"},
    ]
    urls = providers._libtv_message_urls(messages)
    assert urls == [
        "https://libtv-res.liblib.art/claw/x/a.png",
        "https://libtv-res.liblib.art/claw/x/b.jpg",
        "https://libtv-res.liblib.art/claw/x/c.mp4",
    ]


def test_libtv_and_lovart_execution_configs_freeze_the_script_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    scripts_dir, lovart_script = _fake_skill_layout(tmp_path)
    providers.save_libtv_credentials("probe-token", str(scripts_dir))
    providers.save_lovart_credentials("probe-ak", "probe-sk", str(lovart_script))

    libtv = providers._current_native_execution_config("libtv")
    assert libtv["scripts_dir"] == str(scripts_dir)
    assert libtv["im_base"] == settings.libtv_im_base
    assert "cli_path" not in libtv

    lovart = providers._current_native_execution_config("lovart")
    assert lovart["skill_script"] == str(lovart_script)
    assert lovart["mode"] == settings.lovart_thread_mode


def test_native_execution_config_refuses_to_freeze_without_scripts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "libtv_skill_scripts", tmp_path / "missing")
    monkeypatch.setattr(settings, "lovart_skill_script", tmp_path / "missing.py")
    providers.save_libtv_credentials("probe-token")
    providers.save_lovart_credentials("probe-ak", "probe-sk")

    with pytest.raises(providers.ProviderConfigError):
        providers._current_native_execution_config("libtv")
    with pytest.raises(providers.ProviderConfigError):
        providers._current_native_execution_config("lovart")


def test_queued_native_task_without_a_route_fails_at_run_time_not_at_submit(
    tmp_path, monkeypatch
):
    """A missing upstream route is a normal task failure, not an opaque submit 503."""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "libtv_skill_scripts", tmp_path / "missing")
    profile = providers.ModelProfile(
        id="libtv:agent", label="LibTV \u667a\u80fd\u7f16\u6392", provider="libtv",
        upstream_model="LibTV \u667a\u80fd\u7f16\u6392", enabled=True,
    )
    frozen = providers.freeze_profile_execution(profile)
    assert frozen.execution_config_id == ""  # submit must not raise
    with pytest.raises(providers.ProviderConfigError):
        providers._native_execution_config(frozen)


def test_admin_providers_page_renders_skill_fields_without_echoing_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    scripts_dir, lovart_script = _fake_skill_layout(tmp_path)
    secret = "probe-secret-never-render"
    providers.save_libtv_credentials(secret, str(scripts_dir))
    providers.save_lovart_credentials(secret, secret, str(lovart_script))

    with TestClient(app) as client:
        token = _login(client)
        page = client.get("/admin/providers")
    assert page.status_code == 200
    body = page.text
    assert "skill \u811a\u672c\u76ee\u5f55" in body
    assert "skill \u811a\u672c\u8def\u5f84" in body
    assert "\u6d4b\u8bd5\u8fde\u63a5" in body
    assert str(scripts_dir) in body          # the path is a safe, useful hint
    assert secret not in body                # the credential itself never is
    assert token in body


def test_check_buttons_submit_to_a_registered_route():
    """The 「测试连接」 buttons must post to a route that actually exists.

    Regression: both buttons carried a ``formaction`` of /admin/providers/<p>/check,
    but only .../credentials is registered, so the browser got {"detail":"Not Found"}.

    ``app.routes`` does not expand lazily-mounted routers in this FastAPI version,
    so the route list is probed with a real authenticated POST instead of
    inspecting ``app.routes``.
    """
    template = (settings.template_dir / "admin_providers.html").read_text(encoding="utf-8")
    # Nothing may point the browser at an ad-hoc URL; the check buttons reuse the
    # credential form's own action and mark themselves with action=check.
    assert "formaction" not in template
    assert template.count('name="action" value="check"') == 2

    with TestClient(app) as client:
        _login(client)
        for path in (
            "/admin/providers/libtv/credentials",
            "/admin/providers/lovart/credentials",
        ):
            page = client.get("/admin/providers")
            token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
            # A real POST with action=check must be routed, not 404. It reaches the
            # connectivity probe, which fails cleanly without live credentials.
            response = client.post(
                path,
                data={
                    "libtv_token": "", "libtv_scripts_dir": "",
                    "lovart_access_key": "", "lovart_secret_key": "",
                    "lovart_skill_script": "",
                    "clear_credentials": "", "confirm_clear": "",
                    "action": "check", "csrf": token,
                },
                follow_redirects=False,
            )
            assert response.status_code != 404, f"{path} 未注册路由"
            assert response.status_code == 303, f"{path} 未按预期重定向"


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
            upstream_model="generate_image_nano_banana_pro", execution_config_id="cfg",
        ),
    )
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
        # public profile id and stay opaque about the upstream tool name.
        assert key and "/" not in key and ":" not in key
        assert label and label.strip() == label
        # `--prefer-models` receives exactly this string.
        assert tool.startswith("generate_image_")


def test_every_lovart_catalog_model_is_exposed_and_resolvable(monkeypatch):
    """The catalog must be what `model_profiles()` actually publishes."""
    monkeypatch.setattr(settings, "lovart_access_key", "ak")
    monkeypatch.setattr(settings, "lovart_secret_key", "sk")
    monkeypatch.setattr(settings, "lovart_skill_script", Path(__file__))

    profiles = providers.model_profiles()
    lovart = {profile.id: profile for profile in profiles if profile.provider == "lovart"}

    for key, label, tool in providers.LOVART_MODELS:
        profile = lovart.get(f"lovart:{key}")
        assert profile is not None, f"{key} 未出现在 model_profiles()"
        assert profile.label == label
        assert profile.upstream_model == tool
        assert profile.enabled is True
        # These are the dials the picker renders; empty ones would hide the control.
        # The exact set is model-specific, so assert it against the dispatcher
        # rather than one global constant.
        assert (profile.ratios, profile.resolutions) == providers._lovart_capabilities(tool)
        assert profile.ratios, f"{key} 没有可选比例"
        assert profile.resolutions, f"{key} 没有可选分辨率"
        # A duplicated profile would make get_profile() fail closed.
        assert providers.get_profile(f"lovart:{key}") is not None


def test_gpt_image_generations_are_offered():
    """The GPT Image family the operator asked for must be selectable."""
    tools = {tool for _, _, tool in providers.LOVART_MODELS}
    for tool in (
        "generate_image_gpt_image_2_5_flare",
        "generate_image_gpt_image_2_5_sunburst",
        "generate_image_gpt_image_2",
        "generate_image_gpt_image_1_5",
    ):
        assert tool in tools, f"{tool} 缺失"


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
        enabled=True,
    )
    with pytest.raises(RuntimeError):
        # No image came back, so the executor reports the reason — but the
        # outgoing command has already been captured.
        providers._execute_lovart(FakeGeneration(), profile)

    chat = captured["args"]
    payload = json.loads(chat[chat.index("--prefer-models") + 1])
    assert payload == {"IMAGE": ["generate_image_gpt_image_2"]}

    # The size must travel with the prompt: `chat` has no --size flag, so losing
    # this is exactly how every image came back at the wrong aspect ratio.
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
    # button that would imply a tier the model does not have.
    for ratio in providers.LOVART_RATIOS_GPT_IMAGE_2:
        assert providers._lovart_tiers_for(
            "generate_image_gpt_image_2", ratio
        ) == providers.LOVART_SINGLE_TIER


def test_lovart_prompt_appends_the_size_without_rewriting_the_text():
    """The user's prompt is the user's; the size is appended on its own line."""

    class FakeGeneration:
        original_prompt = "多行提示词\n第二行  含  <特殊> & 符"

        def __init__(self, ratio, resolution):
            self.parameters_json = json.dumps({"ratio": ratio, "resolution": resolution})

    profile = providers.ModelProfile(
        id="lovart:x", label="x", provider="lovart",
        upstream_model="generate_image_gpt_image_2_5_flare", enabled=True,
    )
    result = providers._lovart_prompt(FakeGeneration("16:9", "2K"), profile)
    assert result.startswith(FakeGeneration.original_prompt)
    assert result == f"{FakeGeneration.original_prompt}\n\n画面比例：16:9（2K，2048x1152）"


def test_auto_ratio_leaves_the_prompt_untouched():
    """`auto` means "let the agent decide", so a constraint would contradict it."""

    class FakeGeneration:
        original_prompt = "a cat"
        parameters_json = '{"ratio": "auto", "resolution": "2K"}'

    profile = providers.ModelProfile(
        id="lovart:x", label="x", provider="lovart",
        upstream_model="generate_image_gpt_image_2_5_flare", enabled=True,
    )
    assert providers._lovart_prompt(FakeGeneration(), profile) == "a cat"


def test_public_dict_exposes_per_ratio_tiers_for_lovart_only():
    """The frontend narrows its chips from this map, so it must be present."""
    flare = providers.ModelProfile(
        id="lovart:x", label="x", provider="lovart",
        upstream_model="generate_image_gpt_image_2_5_flare", enabled=True,
        ratios=providers.LOVART_RATIOS_GPT_IMAGE_2_5,
        resolutions=providers.LOVART_RESOLUTIONS,
    )
    payload = flare.public_dict()
    assert payload["tiers"]["1:3"] == ["1K", "2K"]
    assert payload["tiers"]["1:1"] == ["1K", "2K", "4K"]
    assert "upstream_model" not in payload

    single = providers.ModelProfile(
        id="lovart:gpt-image-2", label="x", provider="lovart",
        upstream_model="generate_image_gpt_image_2", enabled=True,
        ratios=providers.LOVART_RATIOS_GPT_IMAGE_2,
        resolutions=providers.LOVART_SINGLE_TIER,
    )
    assert {tuple(v) for v in single.public_dict()["tiers"].values()} == {("自动",)}

    api = providers.ModelProfile(
        id="api:x", label="x", provider="api", upstream_model="gpt-image-1", enabled=True
    )
    assert api.public_dict()["tiers"] == {}
