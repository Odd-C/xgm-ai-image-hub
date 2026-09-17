import json
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from fastapi.testclient import TestClient

from image_hub import providers as provider_module
from image_hub import web as web_module
from image_hub.app import app
from image_hub.config import settings
from image_hub.db import SessionLocal
from image_hub.models import Generation, Project, User
from image_hub.providers import ModelProfile, execute_generation


def login(client: TestClient, username: str = "admin") -> str:
    page = client.get("/login")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={"username": username, "password": "test-password", "csrf": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Logging in rotates the CSRF token, so the pre-login value is dead. Read
    # the live token off an authenticated page instead.
    return csrf_from(client.get("/projects").text)


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    return match.group(1) if match else ""


def create_project(client: TestClient, token: str, name: str) -> str:
    response = client.post(
        "/projects", data={"name": name, "csrf": token}, follow_redirects=False
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


def create_member(username: str, display_name: str = "") -> User:
    """建一个普通成员账号，密码沿用 bootstrap 管理员那份。"""
    with SessionLocal() as session:
        admin = session.query(User).filter_by(username="admin").one()
        member = User(
            username=username,
            display_name=display_name,
            password_hash=admin.password_hash,
        )
        session.add(member)
        session.commit()
        session.refresh(member)
        return member


def install_submission_race_gate(
    monkeypatch, profile: ModelProfile
) -> tuple[Event, Event, Event]:
    first_profile_entered = Event()
    release_first = Event()
    second_lock_attempted = Event()
    attempt_lock = Lock()
    attempt_count = 0
    original_lock = web_module._locked_generation_user

    def observed_lock(request, session):
        nonlocal attempt_count
        with attempt_lock:
            attempt_count += 1
            if attempt_count == 2:
                second_lock_attempted.set()
        return original_lock(request, session)

    def delayed_profile(_):
        if not first_profile_entered.is_set():
            first_profile_entered.set()
            assert release_first.wait(5)
        return profile

    monkeypatch.setattr(web_module, "_locked_generation_user", observed_lock)
    monkeypatch.setattr(web_module, "get_profile", delayed_profile)
    return first_profile_entered, release_first, second_lock_attempted


def test_health_login_and_project_workspace():
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/api/projects/unknown/generations").status_code == 401
        token = login(client)
        assert "我的项目" in client.get("/projects").text
        project_id = create_project(client, token, "测试项目")
        workspace = client.get(f"/projects/{project_id}")
        assert workspace.status_code == 200
        assert "拖入图片，或新建生图" in workspace.text
        assert "测试项目" in workspace.text


def test_prompt_history_and_three_state_marking(monkeypatch):
    profile = ModelProfile(
        id="api:test-image", label="Test Image", provider="api",
        upstream_model="test-image", enabled=True,
    )
    monkeypatch.setattr("image_hub.web.get_profile", lambda _: profile)
    with TestClient(app) as client:
        token = login(client)
        project_id = create_project(client, token, "提示词历史项目")
        response = client.post(
            f"/api/projects/{project_id}/generations",
            data={"prompt": "原始提示词，不要改写", "profile_id": profile.id,
                  "ratio": "1:1", "resolution": "2K", "quality": "standard",
                  "idempotency_key": "1234567890abcdef"},
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 202
        generation_id = response.json()["id"]
        with SessionLocal() as session:
            generation = session.get(Generation, generation_id)
            assert generation.original_prompt == "原始提示词，不要改写"
            assert generation.project_id == project_id
            generation.status = "succeeded"
            session.commit()
        history = client.get(f"/api/projects/{project_id}/generations").json()["items"]
        assert history[0]["prompt"] == "原始提示词，不要改写"
        for sentiment in ("satisfied", "adopted", "dissatisfied"):
            marked = client.post(
                f"/api/projects/{project_id}/generations/{generation_id}/sentiment",
                json={"sentiment": sentiment},
                headers={"X-CSRF-Token": token},
            )
            assert marked.status_code == 200
            assert marked.json()["sentiment"] == sentiment


def test_projects_and_users_are_isolated():
    with TestClient(app) as client:
        token = login(client)
        own_project_id = create_project(client, token, "自己的项目")
        with SessionLocal() as session:
            admin = session.query(User).filter_by(username="admin").one()
            foreign = User(
                username="other", display_name="Other", password_hash=admin.password_hash
            )
            session.add(foreign)
            session.flush()
            foreign_project = Project(user_id=foreign.id, name="其他用户项目")
            session.add(foreign_project)
            session.flush()
            generation = Generation(
                user_id=foreign.id, project_id=foreign_project.id,
                idempotency_key="foreign-idempotent", original_prompt="不可见提示词",
                provider="api", model_id="x", model_label="x", status="succeeded",
            )
            session.add(generation)
            session.commit()
            foreign_project_id = foreign_project.id
        assert client.get(f"/projects/{foreign_project_id}").status_code == 404
        assert client.get(f"/api/projects/{foreign_project_id}/generations").status_code == 404
        own_history = client.get(f"/api/projects/{own_project_id}/generations").json()["items"]
        assert own_history == []


def test_canvas_state_is_unique_per_project():
    with TestClient(app) as client:
        token = login(client)
        first_id = create_project(client, token, "画布甲")
        second_id = create_project(client, token, "画布乙")
        first_state = {"viewport": {"x": 10, "y": 20, "zoom": 0.8}, "nodes": [{"id": "a"}]}
        saved = client.put(
            f"/api/projects/{first_id}/canvas",
            json=first_state,
            headers={"X-CSRF-Token": token},
        )
        assert saved.status_code == 200
        assert client.get(f"/api/projects/{first_id}/canvas").json()["state"] == first_state
        assert client.get(f"/api/projects/{second_id}/canvas").json()["state"] == {}

        second_state = {
            "viewport": {"x": -90, "y": 45, "zoom": 1.25},
            "nodes": [{"id": "second-only", "type": "generation_request"}],
            "clipboard": {"selectedIds": ["second-only"]},
            "selection": ["second-only"],
            "draft": {"prompt": "乙项目草稿"},
        }
        assert client.put(
            f"/api/projects/{second_id}/canvas",
            json=second_state,
            headers={"X-CSRF-Token": token},
        ).status_code == 200
        assert client.get(f"/api/projects/{second_id}/canvas").json()["state"] == second_state
        assert client.get(f"/api/projects/{first_id}/canvas").json()["state"] == first_state

        draft = {"prompt": "保留画布时保存草稿"}
        assert client.put(
            f"/api/projects/{first_id}/canvas",
            json={"draft": draft},
            headers={"X-CSRF-Token": token},
        ).status_code == 200
        assert client.get(f"/api/projects/{first_id}/canvas").json()["state"] == {
            **first_state,
            "draft": draft,
        }
        assert client.get(f"/api/projects/{second_id}/canvas").json()["state"] == second_state


def test_workspace_project_switcher_lists_only_owned_active_projects():
    with TestClient(app) as client:
        token = login(client)
        first_id = create_project(client, token, "切换项目甲")
        second_id = create_project(client, token, "切换项目乙")
        response = client.get(f"/projects/{first_id}")
        assert response.status_code == 200
        assert f'href="/projects/{first_id}"' in response.text
        assert f'href="/projects/{second_id}"' in response.text
        assert 'data-new-project' in response.text
        assert 'action="/projects"' in response.text
        assert f'action="/projects/{first_id}/rename"' not in response.text

        with SessionLocal() as session:
            admin = session.query(User).filter_by(username="admin").one()
            foreign = User(username="foreign-switcher", password_hash=admin.password_hash)
            session.add(foreign)
            session.flush()
            foreign_project = Project(user_id=foreign.id, name="他人不可见项目")
            session.add(foreign_project)
            session.commit()
        response = client.get(f"/projects/{first_id}")
        assert "他人不可见项目" not in response.text


def test_mutations_require_csrf(monkeypatch):
    profile = ModelProfile(
        id="api:test-image", label="Test Image", provider="api",
        upstream_model="test-image", enabled=True,
    )
    monkeypatch.setattr("image_hub.web.get_profile", lambda _: profile)
    with TestClient(app) as client:
        token = login(client)
        project_id = create_project(client, token, "CSRF 项目")
        response = client.post(
            f"/api/projects/{project_id}/generations",
            data={"prompt": "不能跨站提交", "profile_id": profile.id, "ratio": "1:1",
                  "resolution": "2K", "quality": "standard",
                  "idempotency_key": "abcdef1234567890"},
        )
        assert response.status_code == 403


def test_generation_submission_is_idempotent_per_project(monkeypatch):
    profile = ModelProfile(
        id="api:test-image", label="Test Image", provider="api",
        upstream_model="test-image", enabled=True,
    )
    monkeypatch.setattr("image_hub.web.get_profile", lambda _: profile)
    with TestClient(app) as client:
        token = login(client)
        first_project = create_project(client, token, "幂等项目甲")
        second_project = create_project(client, token, "幂等项目乙")
        payload = {"prompt": "只应创建一条", "profile_id": profile.id, "ratio": "1:1",
                   "resolution": "2K", "quality": "standard",
                   "idempotency_key": "same-key-12345678"}
        first_url = f"/api/projects/{first_project}/generations"
        first = client.post(first_url, data=payload, headers={"X-CSRF-Token": token})
        duplicate = client.post(first_url, data=payload, headers={"X-CSRF-Token": token})
        second = client.post(
            f"/api/projects/{second_project}/generations",
            data=payload,
            headers={"X-CSRF-Token": token},
        )
        assert first.status_code == duplicate.status_code == second.status_code == 202
        assert first.json()["id"] == duplicate.json()["id"]
        assert second.json()["id"] != first.json()["id"]


def test_active_task_limit_applies_across_projects(monkeypatch):
    profile = ModelProfile(
        id="api:test-image", label="Test Image", provider="api",
        upstream_model="test-image", enabled=True,
        ratios=("1:1",), resolutions=("2K",), qualities=("standard",), max_references=0,
    )
    monkeypatch.setattr("image_hub.web.get_profile", lambda _: profile)
    monkeypatch.setattr(settings, "max_active_tasks_per_user", 1)
    create_member("limited-member", "额度成员")
    with TestClient(app) as client:
        token = login(client, "limited-member")
        first_project = create_project(client, token, "额度项目甲")
        second_project = create_project(client, token, "额度项目乙")
        payload = {
            "prompt": "账号级额度测试",
            "profile_id": profile.id,
            "ratio": "1:1",
            "resolution": "2K",
            "quality": "standard",
            "idempotency_key": "limit-key-12345678",
        }
        assert client.post(
            f"/api/projects/{first_project}/generations",
            data=payload,
            headers={"X-CSRF-Token": token},
        ).status_code == 202
        payload["idempotency_key"] = "limit-key-87654321"
        assert client.post(
            f"/api/projects/{second_project}/generations",
            data=payload,
            headers={"X-CSRF-Token": token},
        ).status_code == 429


def test_concurrent_idempotent_submissions_return_the_same_generation(monkeypatch):
    profile = ModelProfile(
        id="api:test-image", label="Test Image", provider="api",
        upstream_model="test-image", enabled=True,
        ratios=("1:1",), resolutions=("2K",), qualities=("standard",), max_references=0,
    )
    monkeypatch.setattr(settings, "max_active_tasks_per_user", 50)
    first_entered, release_first, second_attempted = install_submission_race_gate(
        monkeypatch, profile
    )
    with TestClient(app) as first_client, TestClient(app) as second_client:
        first_token = login(first_client)
        second_token = login(second_client)
        project_id = create_project(first_client, first_token, "并发幂等项目")
        payload = {
            "prompt": "并发幂等测试",
            "profile_id": profile.id,
            "ratio": "1:1",
            "resolution": "2K",
            "quality": "standard",
            "idempotency_key": "concurrent-same-key",
        }
        url = f"/api/projects/{project_id}/generations"
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                first_client.post, url, data=payload, headers={"X-CSRF-Token": first_token}
            )
            assert first_entered.wait(5)
            second_future = executor.submit(
                second_client.post, url, data=payload, headers={"X-CSRF-Token": second_token}
            )
            assert second_attempted.wait(5)
            release_first.set()
            first_response = first_future.result(timeout=5)
            second_response = second_future.result(timeout=5)

        assert first_response.status_code == second_response.status_code == 202
        assert first_response.json()["id"] == second_response.json()["id"]
        with SessionLocal() as session:
            assert session.query(Generation).filter_by(project_id=project_id).count() == 1


def test_concurrent_cross_project_submissions_respect_user_limit(monkeypatch):
    profile = ModelProfile(
        id="api:test-image", label="Test Image", provider="api",
        upstream_model="test-image", enabled=True,
        ratios=("1:1",), resolutions=("2K",), qualities=("standard",), max_references=0,
    )
    monkeypatch.setattr(settings, "max_active_tasks_per_user", 1)
    first_entered, release_first, second_attempted = install_submission_race_gate(
        monkeypatch, profile
    )
    create_member("concurrent-member", "并发额度成员")
    with TestClient(app) as first_client, TestClient(app) as second_client:
        first_token = login(first_client, "concurrent-member")
        second_token = login(second_client, "concurrent-member")
        first_project = create_project(first_client, first_token, "并发额度项目甲")
        second_project = create_project(first_client, first_token, "并发额度项目乙")

        def submit(client, token, project_id, key):
            return client.post(
                f"/api/projects/{project_id}/generations",
                data={
                    "prompt": "并发额度测试",
                    "profile_id": profile.id,
                    "ratio": "1:1",
                    "resolution": "2K",
                    "quality": "standard",
                    "idempotency_key": key,
                },
                headers={"X-CSRF-Token": token},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                submit, first_client, first_token, first_project, "concurrent-limit-key-a"
            )
            assert first_entered.wait(5)
            second_future = executor.submit(
                submit, second_client, second_token, second_project, "concurrent-limit-key-b"
            )
            assert second_attempted.wait(5)
            release_first.set()
            responses = [first_future.result(timeout=5), second_future.result(timeout=5)]

        assert sorted(response.status_code for response in responses) == [202, 429]
        with SessionLocal() as session:
            active_count = session.query(Generation).filter(
                Generation.user_id == session.query(User.id).filter_by(
                    username="concurrent-member"
                ).scalar_subquery(),
                Generation.status.in_(("queued", "running")),
            ).count()
            assert active_count == 1


def test_external_failure_requires_recovery(monkeypatch):
    profile = ModelProfile(
        id="api:test-image", label="Test Image", provider="api",
        upstream_model="test-image", enabled=True,
    )

    def fail_after_submission(generation, _profile):
        generation.external_task_id = "paid-upstream-task"
        raise RuntimeError("download interrupted")

    monkeypatch.setattr("image_hub.providers._execute_api", fail_after_submission)
    with SessionLocal() as session:
        admin = session.query(User).filter_by(username="admin").one()
        project = Project(user_id=admin.id, name="恢复项目")
        session.add(project)
        session.flush()
        generation = Generation(
            user_id=admin.id, project_id=project.id,
            idempotency_key="recovery-test-key", original_prompt="恢复测试",
            provider="api", model_id="test-image", model_label="Test Image",
            provider_snapshot_json=json.dumps(profile.snapshot_dict()),
            parameters_json=json.dumps(
                {"ratio": "1:1", "resolution": "2K", "quality": "standard"}
            ),
            status="running",
        )
        session.add(generation)
        session.commit()
        generation_id = generation.id
    execute_generation(generation_id)
    with SessionLocal() as session:
        generation = session.get(Generation, generation_id)
        assert generation.status == "recovery_required"
        assert generation.external_task_id == "paid-upstream-task"


def test_non_admin_has_no_management_ui_or_access():
    with SessionLocal() as session:
        admin = session.query(User).filter_by(username="admin").one()
        session.add(User(username="member", display_name="普通成员", password_hash=admin.password_hash))
        session.commit()
    with TestClient(app) as client:
        token = login(client, "member")
        projects = client.get("/projects")
        assert "系统管理" not in projects.text
        project_id = create_project(client, token, "普通成员项目")
        assert ">管理<" not in client.get(f"/projects/{project_id}").text
        assert client.get("/admin").status_code == 403


def test_security_headers_are_present():
    with TestClient(app) as client:
        response = client.get("/login")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in response.headers["content-security-policy"]


def test_v1_workspace_contract_has_canvas_ports_without_legacy_surfaces():
    with TestClient(app) as client:
        token = login(client)
        project_id = create_project(client, token, "V1 画布契约")
        html = client.get(f"/projects/{project_id}").text
        script = client.get("/static/app.js").text
        stylesheet = client.get("/static/app.css").text
        canvas_core = client.get("/static/canvas-core.js").text

        assert 'id="canvas-viewport"' in html
        assert 'id="canvas-upload"' in html
        assert 'id="add-request"' in html
        assert f'href="/projects/{project_id}/history"' in html
        for forbidden in ("Agent", "模板市场", "generation-dock", "recent-panel", "lightbox"):
            assert forbidden not in html
        # 3baff7b unified the separate request/result nodes into one generation
        # node, so the v1 ports are now expressed through these symbols.
        assert "core.GENERATION" in script
        assert "core.batchResults" in script
        assert "orderedInputIds" in script
        assert "temporary-link" in script
        assert "pointercancel" in script
        assert "node.orderedInputIds.push(ref)" in script
        assert "data.append('references'" in script
        assert "for (const ref of node?.orderedInputIds" in canvas_core
        assert "ImageHubResultActions" in script
        assert "generationId" in script and "artifactUrl" in script
        assert ".sentiment-row" in stylesheet
        assert ".sentiment-group button.selected" in stylesheet


def test_public_profiles_expose_capabilities_but_not_api_secrets():
    with TestClient(app) as client:
        token = login(client)
        project_id = create_project(client, token, "公开能力契约")
        html = client.get(f"/projects/{project_id}").text
        assert "IMAGE_HUB_MODELS" in html
        assert '"ratios"' in html and '"resolutions"' in html
        assert '"max_references"' in html
        assert settings.openai_image_base_url not in html
        assert "openai_image_api_key" not in html
        assert "key env" not in html


def test_history_is_independent_and_project_scoped(monkeypatch):
    profile = ModelProfile(
        id="api:history-test", label="History Test", provider="api",
        upstream_model="history-test", enabled=True,
    )
    monkeypatch.setattr("image_hub.web.get_profile", lambda _: profile)
    with TestClient(app) as client:
        token = login(client)
        first_id = create_project(client, token, "历史项目 A")
        second_id = create_project(client, token, "历史项目 B")
        created = client.post(
            f"/api/projects/{first_id}/generations",
            data={
                "prompt": "仅属于项目 A", "profile_id": profile.id,
                "ratio": "1:1", "resolution": "2K", "quality": "standard",
                "idempotency_key": "history-project-key",
            },
            headers={"X-CSRF-Token": token},
        )
        assert created.status_code == 202
        page = client.get(f"/projects/{first_id}/history")
        assert page.status_code == 200
        assert "生成历史" in page.text
        for control in ("history-project", "history-status", "history-model", "history-sentiment"):
            assert f'id="{control}"' in page.text
        filtered = client.get(
            f"/api/projects/{first_id}/generations", params={"status_filter": "queued"}
        )
        assert filtered.json()["items"][0]["prompt"] == "仅属于项目 A"
        assert client.get(f"/api/projects/{second_id}/generations").json()["items"] == []


def test_admin_can_store_api_config_without_echoing_secret(monkeypatch):
    monkeypatch.setattr(
        provider_module, "_resolve_and_validate_endpoint", lambda _url: ("203.0.113.10",)
    )
    config_path = settings.storage_root / "provider-config.json"
    config_path.unlink(missing_ok=True)
    with TestClient(app) as client:
        token = login(client)
        response = client.post(
            "/admin/providers/api",
            data={
                "csrf": token,
                "base_url": "https://images.example.test/v1",
                "api_key": "server-only-test-key",
                "models": "image-v1|Studio Image|1:1;3:4|1K;2K|4",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        # 8ab9491 rewrote the admin overview to show per-provider model counts,
        # so the stored model label now surfaces on the provider config page.
        admin_html = client.get("/admin").text
        provider_html = client.get("/admin/providers").text
        assert "Studio Image" in provider_html
        assert "server-only-test-key" not in admin_html
        assert "server-only-test-key" not in provider_html
        project_id = create_project(client, token, "API 配置公开边界")
        workspace_html = client.get(f"/projects/{project_id}").text
        assert "Studio Image" in workspace_html
        assert "server-only-test-key" not in workspace_html
        assert "images.example.test" not in workspace_html
        assert "image-v1" not in workspace_html
        assert "api:image-v1" not in workspace_html
    config_path.unlink(missing_ok=True)


def test_queued_api_generation_keeps_submission_route_after_admin_edit(monkeypatch):
    config_path = settings.storage_root / "provider-config.json"
    original_config = config_path.read_bytes() if config_path.exists() else None
    monkeypatch.setattr(
        provider_module, "_resolve_and_validate_endpoint", lambda _url: ("198.51.100.20",)
    )
    captured = {}

    def capture_frozen_route(_generation, profile):
        config = provider_module._load_api_execution_config(profile.execution_config_id)
        captured.update(base_url=config["base_url"], api_key=config["api_key"])
        captured["upstream_model"] = profile.upstream_model

    monkeypatch.setattr(provider_module, "_execute_api", capture_frozen_route)
    try:
        provider_module.save_api_config(
            "https://old-route.example.test/v1", "old-secret", "route-model|Route model"
        )
        profile = next(
            item for item in provider_module.model_profiles() if item.upstream_model == "route-model"
        )
        with TestClient(app) as client:
            token = login(client)
            project_id = create_project(client, token, "冻结 API 路由")
            response = client.post(
                f"/api/projects/{project_id}/generations",
                data={
                    "prompt": "必须走旧路由",
                    "profile_id": profile.id,
                    "ratio": "1:1",
                    "resolution": "2K",
                    "quality": "standard",
                    "idempotency_key": "frozen-route-1234",
                },
                headers={"X-CSRF-Token": token},
            )
            assert response.status_code == 202
            generation_id = response.json()["id"]

        provider_module.save_api_config(
            "https://new-route.example.test/v1", "new-secret", "route-model|Route model"
        )
        execute_generation(generation_id)

        assert captured == {
            "base_url": "https://old-route.example.test/v1",
            "api_key": "old-secret",
            "upstream_model": "route-model",
        }
        with SessionLocal() as session:
            assert session.get(Generation, generation_id).status == "succeeded"
    finally:
        if original_config is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_bytes(original_config)


def test_legacy_api_profile_ids_resolve_or_remain_explicitly_unavailable(monkeypatch):
    config_path = settings.storage_root / "provider-config.json"
    original_config = config_path.read_bytes() if config_path.exists() else None
    monkeypatch.setattr(
        provider_module, "_resolve_and_validate_endpoint", lambda _url: ("198.51.100.21",)
    )
    try:
        provider_module.save_api_config(
            "https://legacy.example.test/v1", "legacy-secret", "legacy-model|Legacy Model"
        )
        current = provider_module.get_profile("api:legacy-model")
        assert current is not None
        assert current.id.startswith("api:")
        assert current.id != "api:legacy-model"

        with TestClient(app) as client:
            token = login(client)
            project_id = create_project(client, token, "旧模型兼容")
            with SessionLocal() as session:
                user = session.query(User).filter_by(username="admin").one()
                generation = Generation(
                    user_id=user.id,
                    project_id=project_id,
                    idempotency_key="legacy-row-key",
                    original_prompt="旧结果提示词",
                    provider="api",
                    model_id="legacy-model",
                    model_label="Legacy Model",
                    provider_snapshot_json=json.dumps(
                        ModelProfile(
                            id="api:legacy-model",
                            label="Legacy Model",
                            provider="api",
                            upstream_model="legacy-model",
                            enabled=True,
                        ).public_dict()
                    ),
                    parameters_json=json.dumps(
                        {"ratio": "3:4", "resolution": "2K", "quality": "standard"}
                    ),
                    status="succeeded",
                )
                session.add(generation)
                session.commit()
                generation_id = generation.id

            saved = client.put(
                f"/api/projects/{project_id}/canvas",
                json={
                    "draft": {"profile": "api:legacy-model", "prompt": "旧草稿"},
                    "nodes": [
                        {
                            "id": "legacy-request",
                            "type": "generation_request",
                            "profileId": "api:legacy-model",
                        },
                        {
                            "id": "legacy-result",
                            "type": "generation_result",
                            "generationId": generation_id,
                            "profileId": "api:legacy-model",
                        },
                    ]
                },
                headers={"X-CSRF-Token": token},
            )
            assert saved.status_code == 200
            loaded_state = client.get(f"/api/projects/{project_id}/canvas").json()["state"]
            loaded_nodes = loaded_state["nodes"]
            assert loaded_state["draft"]["profile"] == current.id
            assert [node["profileId"] for node in loaded_nodes] == [current.id, current.id]
            assert all("profileUnavailable" not in node for node in loaded_nodes)

            historical = client.get(
                f"/api/projects/{project_id}/generations/{generation_id}"
            ).json()
            assert historical["profile_id"] == current.id
            assert historical["profile_available"] is True

            provider_module.save_api_config(
                "https://legacy.example.test/v1", "legacy-secret", "replacement|Replacement"
            )
            unavailable = client.get(
                f"/api/projects/{project_id}/generations/{generation_id}"
            ).json()
            assert unavailable["profile_id"] == current.id
            assert unavailable["profile_available"] is False
            assert "legacy-model" not in json.dumps(unavailable)
            blocked = client.post(
                f"/api/projects/{project_id}/generations",
                data={
                    "prompt": "不得切到其他供应商",
                    "profile_id": "api:legacy-model",
                    "ratio": "1:1",
                    "resolution": "2K",
                    "quality": "standard",
                    "idempotency_key": "legacy-blocked-1234",
                },
                headers={"X-CSRF-Token": token},
            )
            assert blocked.status_code == 503
    finally:
        if original_config is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_bytes(original_config)


def test_ambiguous_legacy_and_opaque_api_id_fails_closed(monkeypatch):
    opaque_match = ModelProfile(
        id="api:collision", label="Opaque match", provider="api",
        upstream_model="different-model", enabled=True,
    )
    legacy_match = ModelProfile(
        id="api:other-opaque", label="Legacy match", provider="api",
        upstream_model="collision", enabled=True,
    )
    monkeypatch.setattr(
        provider_module, "model_profiles", lambda: (opaque_match, legacy_match)
    )

    assert provider_module.get_profile("api:collision") is None
    assert provider_module.get_profile("api:other-opaque") == legacy_match


def test_history_uses_distinct_locate_and_continue_actions():
    script = (settings.static_dir / "history.js").read_text()
    assert "?action=locate&amp;generation=" in script
    assert "?action=continue&amp;generation=" in script
    assert "?reuse=" not in script


def test_member_cannot_open_another_users_history():
    with SessionLocal() as session:
        admin = session.query(User).filter_by(username="admin").one()
        session.add(
            User(
                username="history-member",
                display_name="历史普通成员",
                password_hash=admin.password_hash,
            )
        )
        session.commit()
    with TestClient(app) as admin_client:
        admin_token = login(admin_client)
        admin_project = create_project(admin_client, admin_token, "管理员历史")
    with TestClient(app) as member_client:
        login(member_client, "history-member")
        assert member_client.get(f"/projects/{admin_project}/history").status_code == 404