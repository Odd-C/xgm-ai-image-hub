from sqlalchemy import create_engine, inspect, text

from image_hub import db
from image_hub.db import Base
from image_hub.models import Generation

LEGACY_GENERATIONS_DDL = """
CREATE TABLE generations (
    id VARCHAR(32) NOT NULL PRIMARY KEY,
    user_id VARCHAR(32) NOT NULL,
    idempotency_key VARCHAR(64) NOT NULL,
    original_prompt TEXT NOT NULL,
    provider VARCHAR(40) NOT NULL,
    model_id VARCHAR(120) NOT NULL,
    model_label VARCHAR(160) NOT NULL,
    provider_snapshot_json TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    reference_manifest_json TEXT NOT NULL,
    parent_generation_id VARCHAR(32) NOT NULL,
    status VARCHAR(40) NOT NULL,
    sentiment VARCHAR(20) NOT NULL,
    external_project_id VARCHAR(160) NOT NULL,
    external_task_id VARCHAR(160) NOT NULL,
    artifact_storage_key VARCHAR(500) NOT NULL,
    error_message TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    lease_owner VARCHAR(160) NOT NULL,
    lease_expires_at DATETIME,
    created_at DATETIME NOT NULL,
    started_at DATETIME,
    finished_at DATETIME,
    UNIQUE (user_id, idempotency_key),
    FOREIGN KEY(user_id) REFERENCES users (id)
)
"""


def test_legacy_sqlite_migration_preserves_history_and_updates_constraints(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "legacy.db"
    migration_engine = create_engine(f"sqlite:///{database_path}")
    with migration_engine.begin() as connection:
        # 老库的 users 只有 id 是不够的：积分迁移会读 role / is_active / created_at
        connection.execute(
            text(
                "CREATE TABLE users ("
                "id VARCHAR(32) NOT NULL PRIMARY KEY, username VARCHAR(80) NOT NULL,"
                "role VARCHAR(20) NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,"
                "created_at DATETIME)"
            )
        )
        connection.execute(text(LEGACY_GENERATIONS_DDL))
        connection.execute(
            text(
                "INSERT INTO users (id,username,role,is_active,created_at) "
                "VALUES ('legacy-user','legacy-admin','admin',1,"
                "'2026-01-01 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO generations "
                "(id,user_id,idempotency_key,original_prompt,provider,model_id,model_label,"
                "provider_snapshot_json,parameters_json,reference_manifest_json,"
                "parent_generation_id,status,sentiment,external_project_id,external_task_id,"
                "artifact_storage_key,error_message,attempt_count,lease_owner,created_at) "
                "VALUES ('legacy-generation','legacy-user','same-key','旧提示词','api','model',"
                "'Model','{}','{}','[]','','succeeded','','','','','',0,'',CURRENT_TIMESTAMP)"
            )
        )

    assert Generation.__table__.metadata is Base.metadata
    Base.metadata.create_all(bind=migration_engine)
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reference_images "
                "(id,generation_id,position,original_name,storage_key,sha256,mime_type,width,height) "
                "VALUES ('legacy-reference','legacy-generation',1,'参考图.png','legacy/reference.png',"
                "'sha256','image/png',100,100)"
            )
        )
    monkeypatch.setattr(db, "engine", migration_engine)
    monkeypatch.setattr(db.settings, "database_url", f"sqlite:///{database_path}")

    # 直接跑 init_database() 用的那一份迁移序列，而不是在这里复制顺序 ——
    db._run_migrations()
    # 幂等：整套迁移重复跑不应失败，也不该产生重复迁移结果。
    db._run_migrations()

    unique_constraints = {
        tuple(constraint["column_names"])
        for constraint in inspect(migration_engine).get_unique_constraints("generations")
    }
    assert ("project_id", "idempotency_key") in unique_constraints
    assert ("user_id", "idempotency_key") not in unique_constraints

    # agent 节点的四列：additive + server_default，老行无需回填（mode 为空即普通生图）。
    generation_columns = {
        column["name"] for column in inspect(migration_engine).get_columns("generations")
    }
    assert {"mode", "agent_spec_json", "usage_json", "validation_json"} <= generation_columns

    with migration_engine.begin() as connection:
        migrated_projects = connection.execute(
            text("SELECT id, name FROM projects WHERE user_id='legacy-user'")
        ).all()
        assert len(migrated_projects) == 1
        assert migrated_projects[0].name == "迁移的历史项目"
        assert connection.execute(
            text("SELECT project_id FROM generations WHERE id='legacy-generation'")
        ).scalar_one() == migrated_projects[0].id
        assert connection.execute(
            text(
                "SELECT generation_id FROM reference_images WHERE id='legacy-reference'"
            )
        ).scalar_one() == "legacy-generation"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

        connection.execute(
            text(
                "INSERT INTO projects "
                "(id,user_id,name,description,canvas_state_json,created_at,updated_at) "
                "VALUES ('second-project','legacy-user','第二个项目','','{}',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO generations "
                "(id,user_id,project_id,idempotency_key,original_prompt,provider,model_id,"
                "model_label,provider_snapshot_json,parameters_json,reference_manifest_json,"
                "parent_generation_id,status,sentiment,external_project_id,external_task_id,"
                "artifact_storage_key,error_message,attempt_count,lease_owner,created_at) "
                "VALUES ('second-generation','legacy-user','second-project','same-key','新提示词',"
                "'api','model','Model','{}','{}','[]','','queued','','','','','',0,'',"
                "CURRENT_TIMESTAMP)"
            )
        )


def test_legacy_tenant_and_credit_columns_are_dropped(tmp_path, monkeypatch):
    """老库升级：把已经废弃的租户 / 积分列删掉（本版本已无这些概念）。"""
    database_path = tmp_path / "legacy-drop.db"
    migration_engine = create_engine(f"sqlite:///{database_path}")
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE users ("
                "id VARCHAR(32) NOT NULL PRIMARY KEY, username VARCHAR(80) NOT NULL,"
                "role VARCHAR(20) NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,"
                "owner_admin_id VARCHAR(32) NOT NULL DEFAULT '',"
                "credits INTEGER NOT NULL DEFAULT 0,"
                "created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users (id,username,role,is_active,owner_admin_id,credits,created_at) "
                "VALUES ('old-admin','admin','admin',1,'x',120,'2026-01-01 00:00:00')"
            )
        )
    monkeypatch.setattr(db, "engine", migration_engine)
    monkeypatch.setattr(db.settings, "database_url", f"sqlite:///{database_path}")

    db._migrate_drop_legacy_columns()
    # 幂等：再跑一次不应失败（列已经不在了）。
    db._migrate_drop_legacy_columns()

    user_columns = {column["name"] for column in inspect(migration_engine).get_columns("users")}
    assert not {"owner_admin_id", "credits"} & user_columns
    # 其余列与数据原样保留。
    with migration_engine.begin() as connection:
        assert connection.execute(
            text("SELECT role FROM users WHERE id='old-admin'")
        ).scalar_one() == "admin"


def test_provider_account_migration_adds_route_column_before_table_rebuild(
    tmp_path, monkeypatch
):
    """老库升级：补上 ``generations.provider_account_id``，并建出账号池两张表。

    ⚠️ 这条测试真正在验的是**迁移顺序**，不只是「列加上了」。``_migrate_project_scope()``
    在唯一约束不匹配时会按当前 ORM 定义整表重建 ``generations``，而它
    ``INSERT ... SELECT`` 的列清单直接来自 ``Generation.__table__.columns``（含新列）。
    老库的 ``UNIQUE (user_id, idempotency_key)`` 一定会触发这次重建 —— 所以新列迁移
    只要排到它**后面**，这条测试就会以 ``no such column: provider_account_id`` 失败，
    而不是静默通过。

    两张新表则由 ``init_database()`` 的 ``Base.metadata.create_all`` 直接建出，
    与 ``assets`` / ``announcements`` 同一套约定，所以没有对应的迁移函数。
    """
    database_path = tmp_path / "legacy-provider-accounts.db"
    migration_engine = create_engine(f"sqlite:///{database_path}")
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE users ("
                "id VARCHAR(32) NOT NULL PRIMARY KEY, username VARCHAR(80) NOT NULL,"
                "role VARCHAR(20) NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,"
                "created_at DATETIME)"
            )
        )
        connection.execute(text(LEGACY_GENERATIONS_DDL))
        connection.execute(
            text(
                "INSERT INTO users (id,username,role,is_active,created_at) "
                "VALUES ('u1','admin','admin',1,'2026-01-01 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO generations "
                "(id,user_id,idempotency_key,original_prompt,provider,model_id,model_label,"
                "provider_snapshot_json,parameters_json,reference_manifest_json,"
                "parent_generation_id,status,sentiment,external_project_id,external_task_id,"
                "artifact_storage_key,error_message,attempt_count,lease_owner,created_at) "
                "VALUES ('g1','u1','k1','旧提示词','libtv','m','M','{}','{}','[]','',"
                "'succeeded','','p','t','','',0,'',CURRENT_TIMESTAMP)"
            )
        )

    # 同 init_database()：先 create_all 建出两张新表，再跑迁移序列。
    Base.metadata.create_all(bind=migration_engine)
    monkeypatch.setattr(db, "engine", migration_engine)
    monkeypatch.setattr(db.settings, "database_url", f"sqlite:///{database_path}")

    db._run_migrations()
    # 幂等：整套重复跑不应失败（ALTER 有列判断，索引是 IF NOT EXISTS）。
    db._run_migrations()

    assert {"provider_accounts", "provider_routing_cursors"} <= set(
        inspect(migration_engine).get_table_names()
    )
    assert ("provider", "position") in {
        tuple(constraint["column_names"])
        for constraint in inspect(migration_engine).get_unique_constraints("provider_accounts")
    }
    assert "provider_account_id" in {
        column["name"] for column in inspect(migration_engine).get_columns("generations")
    }

    with migration_engine.begin() as connection:
        # 老行原样保留，且无需回填：空串 = 这笔历史任务没走过账号池。
        assert connection.execute(
            text("SELECT provider_account_id FROM generations WHERE id='g1'")
        ).scalar_one() == ""
        assert connection.execute(
            text("SELECT original_prompt FROM generations WHERE id='g1'")
        ).scalar_one() == "旧提示词"
        # 路由留痕列**刻意不加外键**：删账号既不该失败，也不该抹掉历史留痕。
        referenced = {
            row[2] for row in connection.execute(
                text("SELECT * FROM pragma_foreign_key_list('generations')")
            )
        }
        assert "provider_accounts" not in referenced
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
