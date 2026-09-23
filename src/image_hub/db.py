import uuid
from collections.abc import Generator

from sqlalchemy import Engine, MetaData, create_engine, event, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable

from image_hub.config import assert_bootstrap_password_is_usable, settings


class Base(DeclarativeBase):
    pass


engine: Engine = create_engine(
    settings.database_url,
    connect_args=(
        {"check_same_thread": False, "timeout": settings.sqlite_busy_timeout_seconds}
        if settings.database_url.startswith("sqlite")
        else {}
    ),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def _run_migrations() -> None:
    """按固定顺序执行所有就地迁移。"""
    _migrate_user_account_fields()
    _migrate_reconcile_fields()
    _migrate_soft_delete_fields()
    _migrate_prompt_optimize_fields()
    _migrate_agent_fields()
    _migrate_audit_source_fields()
    _migrate_provider_account_fields()
    _migrate_project_scope()
    _migrate_drop_legacy_columns()


def init_database() -> None:
    from image_hub.auth import hash_password
    from image_hub.models import User

    Base.metadata.create_all(bind=engine)
    _run_migrations()
    with SessionLocal() as session:
        admin = session.scalar(select(User).where(User.username == settings.bootstrap_admin_username))
        if admin is None:
            # 只有真的要用它建号时才校验口令，避免老部署被失效的配置项拦住启动。
            assert_bootstrap_password_is_usable(settings.bootstrap_admin_password)
            session.add(
                User(
                    username=settings.bootstrap_admin_username,
                    display_name="管理员",
                    password_hash=hash_password(settings.bootstrap_admin_password),
                    role="admin",
                    must_change_password=0,
                )
            )
            session.commit()


def _migrate_user_account_fields() -> None:
    """Add account lifecycle columns to legacy SQLite databases in place."""
    if not settings.database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("users")}
    statements = {
        "auth_version": "ALTER TABLE users ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 1",
        "last_login_at": "ALTER TABLE users ADD COLUMN last_login_at DATETIME",
        "updated_at": "ALTER TABLE users ADD COLUMN updated_at DATETIME",
        "must_change_password": (
            "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
        ),
    }
    with engine.begin() as connection:
        for name, statement in statements.items():
            if name not in columns:
                connection.execute(text(statement))
        connection.execute(
            text("UPDATE users SET updated_at=COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)")
        )


def _migrate_reconcile_fields() -> None:
    """必须排在 ``_migrate_project_scope()`` 之前（原因见 ``_run_migrations``）。"""
    if not settings.database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "generations" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("generations")}
    statements = {
        "reconcile_count": (
            "ALTER TABLE generations ADD COLUMN reconcile_count INTEGER NOT NULL DEFAULT 0"
        ),
        "next_reconcile_at": (
            "ALTER TABLE generations ADD COLUMN next_reconcile_at DATETIME"
        ),
    }
    with engine.begin() as connection:
        for name, statement in statements.items():
            if name not in columns:
                connection.execute(text(statement))


def _migrate_soft_delete_fields() -> None:
    """给老库补上软删时间戳列。两列可空，不需要 DEFAULT。"""
    if not settings.database_url.startswith("sqlite"):
        return
    table_names = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        for table in ("users", "projects"):
            if table not in table_names:
                continue
            columns = {column["name"] for column in inspect(engine).get_columns(table)}
            if "deleted_at" not in columns:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN deleted_at DATETIME"))
            connection.execute(
                text(f"CREATE INDEX IF NOT EXISTS ix_{table}_deleted_at ON {table} (deleted_at)")
            )


def _migrate_prompt_optimize_fields() -> None:
    """给老库补上提示词优化的五列。都必须排在 ``_migrate_project_scope()`` 之前。"""
    if not settings.database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "generations" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("generations")}
    statements = {
        "optimized_prompt": (
            "ALTER TABLE generations ADD COLUMN optimized_prompt TEXT NOT NULL DEFAULT ''"
        ),
        "prompt_optimize_state": (
            "ALTER TABLE generations ADD COLUMN prompt_optimize_state "
            "VARCHAR(20) NOT NULL DEFAULT 'none'"
        ),
        "prompt_optimize_error": (
            "ALTER TABLE generations ADD COLUMN prompt_optimize_error TEXT NOT NULL DEFAULT ''"
        ),
        "prompt_optimize_mode": (
            "ALTER TABLE generations ADD COLUMN prompt_optimize_mode "
            "VARCHAR(20) NOT NULL DEFAULT ''"
        ),
        "prompt_optimize_note": (
            "ALTER TABLE generations ADD COLUMN prompt_optimize_note TEXT NOT NULL DEFAULT ''"
        ),
    }
    with engine.begin() as connection:
        for name, statement in statements.items():
            if name not in columns:
                connection.execute(text(statement))


def _migrate_agent_fields() -> None:
    """给老库补上 agent 节点的五列。都必须排在 ``_migrate_project_scope()`` 之前。"""
    if not settings.database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "generations" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("generations")}
    statements = {
        "mode": ("ALTER TABLE generations ADD COLUMN mode VARCHAR(20) NOT NULL DEFAULT ''"),
        "agent_spec_json": (
            "ALTER TABLE generations ADD COLUMN agent_spec_json TEXT NOT NULL DEFAULT '{}'"
        ),
        "agent_plan_json": (
            "ALTER TABLE generations ADD COLUMN agent_plan_json TEXT NOT NULL DEFAULT '{}'"
        ),
        "usage_json": (
            "ALTER TABLE generations ADD COLUMN usage_json TEXT NOT NULL DEFAULT '{}'"
        ),
        "validation_json": (
            "ALTER TABLE generations ADD COLUMN validation_json TEXT NOT NULL DEFAULT '{}'"
        ),
    }
    with engine.begin() as connection:
        for name, statement in statements.items():
            if name not in columns:
                connection.execute(text(statement))


def _migrate_audit_source_fields() -> None:
    """给老库补上审计记录的来源两列（操作者 IP / User-Agent）。"""
    if not settings.database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "admin_audit_events" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("admin_audit_events")}
    statements = {
        "actor_ip": (
            "ALTER TABLE admin_audit_events ADD COLUMN actor_ip "
            "VARCHAR(45) NOT NULL DEFAULT ''"
        ),
        "actor_agent": (
            "ALTER TABLE admin_audit_events ADD COLUMN actor_agent "
            "VARCHAR(200) NOT NULL DEFAULT ''"
        ),
    }
    with engine.begin() as connection:
        for name, statement in statements.items():
            if name not in columns:
                connection.execute(text(statement))


def _migrate_provider_account_fields() -> None:
    """补账号池相关列与索引；必须排在 _migrate_project_scope() 之前。"""
    if not settings.database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "generations" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("generations")}
    with engine.begin() as connection:
        if "provider_account_id" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE generations ADD COLUMN provider_account_id "
                    "VARCHAR(32) NOT NULL DEFAULT ''"
                )
            )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_generations_provider_account_id "
                "ON generations (provider_account_id)"
            )
        )


def _migrate_project_scope() -> None:
    """Preserve prompt history when upgrading an older SQLite database."""
    if not settings.database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "generations" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("generations")}
    if "project_id" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE generations ADD COLUMN project_id VARCHAR(32) NOT NULL DEFAULT ''")
            )
            connection.execute(
                text("CREATE INDEX ix_generations_project_id ON generations (project_id)")
            )
    with engine.begin() as connection:
        user_ids = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT DISTINCT user_id FROM generations "
                    "WHERE project_id IS NULL OR project_id=''"
                )
            )
        ]
        for user_id in user_ids:
            project_id = connection.execute(
                text(
                    "SELECT id FROM projects "
                    "WHERE user_id=:user_id AND name='迁移的历史项目' LIMIT 1"
                ),
                {"user_id": user_id},
            ).scalar_one_or_none()
            if project_id is None:
                project_id = uuid.uuid4().hex
                connection.execute(
                    text(
                        "INSERT INTO projects "
                        "(id,user_id,name,description,canvas_state_json,created_at,updated_at) "
                        "VALUES (:id,:user_id,'迁移的历史项目','','{}',"
                        "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                    ),
                    {"id": project_id, "user_id": user_id},
                )
            connection.execute(
                text(
                    "UPDATE generations SET project_id=:project_id "
                    "WHERE user_id=:user_id AND (project_id IS NULL OR project_id='')"
                ),
                {"project_id": project_id, "user_id": user_id},
            )

    constraints = {
        tuple(constraint["column_names"])
        for constraint in inspect(engine).get_unique_constraints("generations")
    }
    if (
        ("project_id", "idempotency_key") not in constraints
        or ("user_id", "idempotency_key") in constraints
    ):
        _rebuild_generations_table()


def _rebuild_generations_table() -> None:
    """Replace legacy SQLite constraints without losing generations or references."""
    from image_hub.models import Generation, Project, User

    migration_metadata = MetaData()
    User.__table__.to_metadata(migration_metadata)
    Project.__table__.to_metadata(migration_metadata)
    migrated_table = Generation.__table__.to_metadata(
        migration_metadata, name="generations_project_scope"
    )
    column_names = [column.name for column in Generation.__table__.columns]
    quoted_columns = ", ".join(
        engine.dialect.identifier_preparer.quote(name) for name in column_names
    )

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            with connection.begin():
                connection.execute(text("DROP TABLE IF EXISTS generations_project_scope"))
                connection.execute(CreateTable(migrated_table))
                connection.execute(
                    text(
                        "INSERT INTO generations_project_scope "
                        f"({quoted_columns}) SELECT {quoted_columns} FROM generations"
                    )
                )
                connection.execute(text("DROP TABLE generations"))
                connection.execute(
                    text("ALTER TABLE generations_project_scope RENAME TO generations")
                )
                for index in Generation.__table__.indexes:
                    connection.execute(CreateIndex(index))
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
            connection.commit()
        if violations:
            raise RuntimeError(f"SQLite project migration left invalid foreign keys: {violations}")


def _migrate_drop_legacy_columns() -> None:
    """丢掉历史遗留的租户 / 积分列（本版本已无这些概念）。"""
    if not settings.database_url.startswith("sqlite"):
        return
    legacy = {
        "users": ("owner_admin_id", "credits"),
        "generations": ("credits_charged", "credits_state"),
        "assets": ("owner_admin_id",),
        "provider_accounts": ("tenant_admin_id",),
        "provider_routing_cursors": ("tenant_admin_id",),
    }
    existing_tables = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        for table, columns in legacy.items():
            if table not in existing_tables:
                continue
            present = {column["name"] for column in inspect(engine).get_columns(table)}
            for column in columns:
                if column in present:
                    connection.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
