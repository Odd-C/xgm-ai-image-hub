"""平台账号池的管理页：增删改移、失效清除。

账号池是全局的，所以每个用例开始前先清空池，避免测试会话共用 test.db 时互相污染。
"""

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from image_hub import libtv_mcp
from image_hub.app import app
from image_hub.auth import hash_password
from image_hub.db import SessionLocal
from image_hub.models import AdminAuditEvent, ProviderAccount, ProviderRoutingCursor, User

PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def _empty_pool():
    with SessionLocal() as session:
        for row in session.scalars(select(ProviderAccount)):
            session.delete(row)
        for row in session.scalars(select(ProviderRoutingCursor)):
            session.delete(row)
        session.commit()
    yield


def _login(client: TestClient, username: str) -> str:
    page = client.get("/login")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={"username": username, "password": PASSWORD, "csrf": token},
        follow_redirects=False)
    assert response.status_code == 303, response.text
    page = client.get("/admin/providers")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def _fresh_admin() -> str:
    username = f"pool-admin-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as session:
        session.add(
            User(
                username=username,
                display_name=username,
                password_hash=hash_password(PASSWORD),
                role="admin",
                must_change_password=0)
        )
        session.commit()
    return username


def _pool(provider: str = "libtv") -> list[ProviderAccount]:
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(ProviderAccount)
                .where(ProviderAccount.provider == provider)
                .order_by(ProviderAccount.position)
            )
        )


def _add(client: TestClient, token: str, label: str, provider: str = "libtv") -> None:
    response = client.post(
        "/admin/providers/accounts/add",
        data={"provider": provider, "label": label, "csrf": token},
        follow_redirects=False)
    assert response.status_code == 303, response.text


def _post(client: TestClient, path: str, token: str, **data) -> None:
    response = client.post(path, data={"csrf": token, **data}, follow_redirects=False)
    assert response.status_code == 303, response.text


def test_admin_adds_an_account_and_the_page_lists_it():
    with TestClient(app) as client:
        token = _login(client, _fresh_admin())
        _add(client, token, "主号")
        page = client.get("/admin/providers").text
        assert "主号" in page
        assert "平台账号池" in page
    rows = _pool()
    assert [row.label for row in rows] == ["主号"]
    assert rows[0].position == 0
    # 池上的每一次变更都要留审计，且指向具体那个号。
    with SessionLocal() as session:
        actions = {
            event.action
            for event in session.scalars(
                select(AdminAuditEvent).where(AdminAuditEvent.target_id == rows[0].id)
            )
        }
    assert "provider.account_added" in actions


def test_admin_reorders_renames_and_toggles_accounts():
    with TestClient(app) as client:
        token = _login(client, _fresh_admin())
        _add(client, token, "一号")
        _add(client, token, "二号")
        rows = {row.label: row for row in _pool()}
        first, second = rows["一号"], rows["二号"]

        # 上移「二号」：它必须真的排到前面去（固定顺序 = 路由顺序）。
        _post(client, "/admin/providers/accounts/move", token, account_id=second.id, direction="up")
        moved = {row.label: row.position for row in _pool()}
        assert moved["二号"] < moved["一号"]

        _post(
            client,
            "/admin/providers/accounts/update",
            token,
            account_id=first.id,
            action="rename",
            label="一号（改名后）")
        assert {row.label for row in _pool()} == {"一号（改名后）", "二号"}

        _post(
            client, "/admin/providers/accounts/update", token, account_id=first.id, action="disable"
        )
        assert next(row for row in _pool() if row.id == first.id).enabled == 0
        _post(
            client, "/admin/providers/accounts/update", token, account_id=first.id, action="enable"
        )
        assert next(row for row in _pool() if row.id == first.id).enabled == 1

        # 已经在顺序尽头上移：拒绝但不改数据（不许把 position 弄乱）。
        before = {row.label: row.position for row in _pool()}
        _post(client, "/admin/providers/accounts/move", token, account_id=second.id, direction="up")
        assert {row.label: row.position for row in _pool()} == before


def test_admin_can_clear_an_exhausted_mark_and_the_page_says_so():
    with TestClient(app) as client:
        token = _login(client, _fresh_admin())
        _add(client, token, "耗尽号")
        account = _pool()[0]
        # 模拟上游判过额度不足（真实场景由执行侧写入）。
        with SessionLocal() as session:
            stored = session.get(ProviderAccount, account.id)
            stored.exhausted_at = stored.created_at
            stored.exhausted_reason = "LibTV 工具执行失败：credits not enough"
            session.commit()

        page = client.get("/admin/providers").text
        assert "额度耗尽" in page
        # 上游原文要展示：管理员据此判断「这到底是不是额度问题」。
        assert "credits not enough" in page

        _post(client, "/admin/providers/accounts/clear_exhausted", token, account_id=account.id)
        # 再清一次没有可清的东西：如实报错（经 flash），而不是假报成功。
        _post(client, "/admin/providers/accounts/clear_exhausted", token, account_id=account.id)
    assert _pool()[0].exhausted_at is None


def test_removing_an_account_also_deletes_its_server_side_credentials(monkeypatch):
    """账号行删了、令牌还躺在私有目录里 = 一份没人用也没人看得见的有效凭据。"""
    # 撤销授权会打上游：这里断掉网络调用，只验证本地凭据确实被清掉。
    monkeypatch.setattr(libtv_mcp, "_http_post_form", lambda *args, **kwargs: (200, {}, "{}"))
    with TestClient(app) as client:
        token = _login(client, _fresh_admin())
        _add(client, token, "待移除")
        account = _pool()[0]
        libtv_mcp._store_tokens(
            {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
            account.id)
        assert libtv_mcp.authorized(account.id) is True

        # 没有确认标记 → 拒绝。
        _post(client, "/admin/providers/accounts/delete", token, account_id=account.id)
        assert len(_pool()) == 1

        _post(
            client,
            "/admin/providers/accounts/delete",
            token,
            account_id=account.id,
            confirm_clear="delete-provider-account")
    assert _pool() == []
    assert libtv_mcp.authorized(account.id) is False
