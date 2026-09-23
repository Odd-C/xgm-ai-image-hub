"""审计记录（``/admin/audit``）的回归测试。

钉住本轮对审计子系统的五处改动：

1. **来源留痕** —— 记下操作者的 IP 与 User-Agent（审计链只有「谁 + 做了什么」
   不够用，出事时要能回答「从哪来」）。
2. **action 筛选是精确匹配** —— 下拉框的候选取自 ``distinct(action)``，而此前用
   的是 ``LIKE %..%``：既对不上语义，输入里的 ``%`` / ``_`` 还会退化成通配符。
3. **计数不再白 join** —— 只有筛操作者时才 join ``users``。
4. **字段长度在写入口收敛** —— SQLite 不校验 VARCHAR，超长值能写进去，
   等迁到 PostgreSQL 才炸。
5. **CSV 导出** —— 带 BOM（Excel 认 UTF-8）、仅总管理员、有行数上限、
   中和公式注入，且导出动作本身留痕但不出现在自己导出的文件里。
"""

import csv
import io
import re
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from image_hub import db
from image_hub.app import app
from image_hub.auth import hash_password
from image_hub.db import SessionLocal
from image_hub.management import _audit, _audit_source, _csv_cell
from image_hub.models import AdminAuditEvent, User

ADMIN = "admin"
PASSWORD = "test-password"

#: 老库的 ``admin_audit_events``：没有来源两列。
LEGACY_AUDIT_DDL = """
CREATE TABLE admin_audit_events (
    id VARCHAR(32) NOT NULL PRIMARY KEY,
    actor_user_id VARCHAR(32),
    action VARCHAR(80) NOT NULL,
    target_type VARCHAR(40) NOT NULL,
    target_id VARCHAR(160) NOT NULL,
    summary VARCHAR(500) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL
)
"""


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


def make_user(username: str, role: str = "user") -> str:
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


def audit_events(action: str = "") -> list[dict]:
    """把审计事件读成普通 dict —— 出了 session 再去碰 ``event.actor`` 会触发
    延迟加载并报 DetachedInstanceError。"""
    with SessionLocal() as session:
        statement = select(AdminAuditEvent).order_by(AdminAuditEvent.id)
        if action:
            statement = statement.where(AdminAuditEvent.action == action)
        return [
            {
                "action": event.action,
                "target_type": event.target_type,
                "target_id": event.target_id,
                "summary": event.summary,
                "actor_user_id": event.actor_user_id,
                "actor_ip": event.actor_ip,
                "actor_agent": event.actor_agent,
            }
            for event in session.scalars(statement).all()
        ]


def audit_total(html: str) -> int:
    match = re.search(r"共 (\d+) 条记录", html)
    assert match, "审计页应当渲染「共 N 条记录」"
    return int(match.group(1))


# ---------------------------------------------------------------------------


def test_audit_migration_adds_source_columns(tmp_path, monkeypatch):
    """老库的审计表没有来源两列，迁移要补上；老行保持「来源未知」。

    ⚠️ 这是**真实会跑到**的路径：``admin_audit_events`` 从第一版就存在，
    而这两列是后加的 —— 升级时一定会走 ALTER。
    """
    database_path = tmp_path / "legacy-audit.db"
    migration_engine = create_engine(f"sqlite:///{database_path}")
    with migration_engine.begin() as connection:
        connection.execute(text(LEGACY_AUDIT_DDL))
        connection.execute(
            text(
                "INSERT INTO admin_audit_events "
                "(id,actor_user_id,action,target_type,target_id,summary,created_at) "
                "VALUES ('old-event',NULL,'user.created','user','u1','旧记录',CURRENT_TIMESTAMP)"
            )
        )
    monkeypatch.setattr(db, "engine", migration_engine)
    monkeypatch.setattr(db.settings, "database_url", f"sqlite:///{database_path}")

    db._migrate_audit_source_fields()
    # 幂等：第二次不该再 ALTER（SQLite 会报 duplicate column name）
    db._migrate_audit_source_fields()

    columns = {
        column["name"] for column in inspect(migration_engine).get_columns("admin_audit_events")
    }
    assert {"actor_ip", "actor_agent"} <= columns
    with migration_engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT actor_ip, actor_agent, summary FROM admin_audit_events "
                "WHERE id='old-event'"
            )
        ).one()
        # 空串就是「来源未知」——不回填，也不假装知道
        assert row == ("", "", "旧记录")


def test_an_admin_action_records_who_did_it_and_from_where():
    """一次真实的管理动作要同时留下操作者与来源（IP / User-Agent）。"""
    with TestClient(app) as client:
        token = login(client, ADMIN)
        response = client.post(
            "/admin/users",
            data={
                "csrf": token,
                "username": f"audit-src-{uuid4().hex[:8]}",
                "password": "member-password",
                "display_name": "审计来源",
                "role": "user",
            },
            headers={"user-agent": "audit-probe/1.0"},
            follow_redirects=False)
        assert response.status_code == 303, response.text

    created = audit_events("user.created")
    assert created, "创建账号必须留下审计事件"
    latest = created[-1]
    assert latest["actor_ip"] == "testclient"  # TestClient 的默认来源
    assert latest["actor_agent"] == "audit-probe/1.0"


def test_an_audit_event_without_a_request_records_no_source():
    """没有请求上下文时（例如将来的后台任务）来源留空，而不是抛异常。"""
    assert _audit_source(None) == ("", "")
    marker = uuid4().hex[:8]
    with SessionLocal() as session:
        _audit(session, None, f"probe.noreq.{marker}", "probe", marker, "无来源")
        session.commit()
    event = next(e for e in audit_events(f"probe.noreq.{marker}"))
    assert event["actor_ip"] == ""
    assert event["actor_agent"] == ""


# ---------------------------------------------------------------------------


def test_audit_action_filter_is_an_exact_match_not_a_like():
    """action 筛选必须精确匹配。

    此前是 ``LIKE %输入%``：① 下拉框的候选来自 ``distinct(action)``，模糊匹配对不上
    语义；② 输入里的 ``%`` 会变成通配符 —— 搜一个 ``%`` 等于「不过滤」。
    """
    marker = uuid4().hex[:8]
    short = f"probe.{marker}.alpha"
    with SessionLocal() as session:
        session.add_all(
            [
                AdminAuditEvent(
                    action=short, target_type="probe", target_id="1", summary="目标记录"
                ),
                AdminAuditEvent(
                    action=f"{short}.extra",
                    target_type="probe",
                    target_id="2",
                    summary="不该被短词匹配到"),
            ]
        )
        session.commit()

    with TestClient(app) as client:
        login(client, ADMIN)
        exact = client.get(f"/admin/audit?action={short}")
        wildcard = client.get("/admin/audit?action=%25")  # % → 字面百分号

    assert exact.status_code == 200
    # 精确匹配只捞到那一条；旧实现（contains）会连 .extra 一起捞出来。
    assert audit_total(exact.text) == 1
    assert "不该被短词匹配到" not in exact.text
    assert audit_total(wildcard.text) == 0


def test_audit_actor_filter_treats_wildcards_literally():
    """actor 是模糊搜索，但 ``%`` / ``_`` 必须是字面量。"""
    make_user(f"wild-{uuid4().hex[:8]}")
    with TestClient(app) as client:
        login(client, ADMIN)
        # 没有任何账号的用户名含字面量 '%'；不转义时它会退化成「匹配一切」。
        assert audit_total(client.get("/admin/audit?actor=%25").text) == 0
        # 反向对照：admin 确实留下过带操作者的审计事件，否则上一条是真空通过。
        assert audit_total(client.get("/admin/audit?actor=admin").text) >= 1


# ---------------------------------------------------------------------------


def test_every_audit_field_is_truncated_to_its_column_limit():
    """超长值必须在写入口被截断。

    SQLite 不校验 VARCHAR 长度，超长值能一路写进去，等迁到 PostgreSQL 才炸 ——
    那时脏数据已经躺在库里了。
    """
    with SessionLocal() as session:
        _audit(session, None, "p" * 200, "t" * 100, "i" * 300, "s" * 900, request=None)
        session.commit()

    with SessionLocal() as session:
        event = session.scalars(
            select(AdminAuditEvent).where(AdminAuditEvent.action == "p" * 80)
        ).one()
        assert len(event.action) == 80
        assert len(event.target_type) == 40
        assert len(event.target_id) == 160
        assert len(event.summary) == 500


# ---------------------------------------------------------------------------


def test_audit_log_is_admin_only():
    """审计记录只对管理员开放；普通账号一律 403。"""
    admin_name = f"audit-admin-{uuid4().hex[:8]}"
    make_user(admin_name, role="admin")
    member_name = f"audit-member-{uuid4().hex[:8]}"
    make_user(member_name, role="user")

    with TestClient(app) as client:
        login(client, admin_name)
        assert client.get("/admin/audit").status_code == 200
        assert client.get("/admin/audit.csv").status_code == 200

    with TestClient(app) as client:
        login(client, member_name)
        assert client.get("/admin/audit").status_code == 403
        assert client.get("/admin/audit.csv").status_code == 403


def test_audit_export_is_utf8_csv_and_neutralises_formulas():
    """导出的 CSV 要能被 Excel 正确打开，且不让摘要变成公式。"""
    marker = uuid4().hex[:8]
    with SessionLocal() as session:
        session.add(
            AdminAuditEvent(
                action="probe.export",
                target_type="probe",
                target_id=marker,
                summary="=cmd|' /C calc'!A0")
        )
        session.commit()

    with TestClient(app) as client:
        login(client, ADMIN)
        response = client.get("/admin/audit.csv?action=probe.export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    # Excel 只有看到 BOM 才认 UTF-8，否则中文列名与摘要全是乱码。
    assert response.text.startswith("\ufeff")

    rows = list(csv.reader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert rows[0] == [
        "时间",
        "操作者",
        "Action",
        "目标类型",
        "目标",
        "来源 IP",
        "User-Agent",
        "安全摘要",
    ]
    data = [row for row in rows[1:] if row[4] == marker]
    assert len(data) == 1
    # 以 = 开头的值被补了一个单引号：Excel 当字面量，值本身不变。
    assert data[0][7] == "'=cmd|' /C calc'!A0"
    assert data[0][2] == "probe.export"


def test_the_export_is_audited_but_absent_from_its_own_file():
    """导出审计日志本身要留痕，但那条事件不该出现在它自己导出的文件里。"""
    with TestClient(app) as client:
        login(client, ADMIN)
        before = len(audit_events("audit.exported"))
        response = client.get("/admin/audit.csv")
        assert response.status_code == 200
        rows = list(csv.reader(io.StringIO(response.text.lstrip("\ufeff"))))

    exported_rows = [row for row in rows[1:] if row[2] == "audit.exported"]
    assert len(exported_rows) == before
    assert len(audit_events("audit.exported")) == before + 1


def test_csv_cell_only_rewrites_formula_leads():
    """中和只针对公式开头；普通文本（含中间带 = 的）必须原样输出。"""
    assert _csv_cell("=1+1") == "'=1+1"
    assert _csv_cell("+86") == "'+86"
    assert _csv_cell("-5") == "'-5"
    assert _csv_cell("@here") == "'@here"
    assert _csv_cell("user.role_changed") == "user.role_changed"
    assert _csv_cell("额度 = 100") == "额度 = 100"
    assert _csv_cell(None) == ""


# ---------------------------------------------------------------------------


def test_audit_page_shows_the_source_column_and_an_export_link():
    with TestClient(app) as client:
        login(client, ADMIN)
        page = client.get("/admin/audit")

    assert page.status_code == 200
    assert "audit-table" in page.text
    assert "<th>来源</th>" in page.text
    assert "/admin/audit.csv" in page.text
