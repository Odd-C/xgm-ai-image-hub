import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from image_hub.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    department: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(300))
    #: user / admin
    role: Mapped[str] = mapped_column(String(20), default="user")
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    #: 每次改密/停用账号自增，用来让旧会话立刻失效。
    auth_version: Mapped[int] = mapped_column(Integer, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    must_change_password: Mapped[int] = mapped_column(Integer, default=0)
    #: 软删：非空表示账号已被删除，仅从登录入口与列表消失，数据保留。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    projects: Mapped[list["Project"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="Project.updated_at.desc()"
    )
    generations: Mapped[list["Generation"]] = relationship(back_populates="user")


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    canvas_state_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 归档与软删分开：归档可翻出，删除从列表移除；两者都不动存储里的图片。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    user: Mapped[User] = relationship(back_populates="projects")
    generations: Mapped[list["Generation"]] = relationship(
        back_populates="project", cascade="all, delete-orphan",
        order_by="Generation.created_at.desc()",
    )


class Generation(Base):
    __tablename__ = "generations"
    __table_args__ = (UniqueConstraint("project_id", "idempotency_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), index=True)
    original_prompt: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    model_id: Mapped[str] = mapped_column(String(120), index=True)
    model_label: Mapped[str] = mapped_column(String(160))
    provider_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    reference_manifest_json: Mapped[str] = mapped_column(Text, default="[]")
    #: 提示词优化结果；空串 = 未优化，执行器回落到 original_prompt。
    optimized_prompt: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: none / requested / done / failed / skipped
    prompt_optimize_state: Mapped[str] = mapped_column(
        String(20), default="none", server_default="none"
    )
    prompt_optimize_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: "" / optimize / agent
    prompt_optimize_mode: Mapped[str] = mapped_column(String(20), default="", server_default="")
    #: Agent 模式的诊断结论，与发给上游的 optimized_prompt 分开存。
    prompt_optimize_note: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: "" 普通生图 / "agent" 结构化工作流
    mode: Mapped[str] = mapped_column(String(20), default="", server_default="")
    agent_spec_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    agent_plan_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    #: 实际消耗：token、张数、上游调用次数与按单价算出的成本。
    usage_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    #: 后置机检结论（比例 / 纯白底 / 文字水印）。
    validation_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    parent_generation_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    sentiment: Mapped[str] = mapped_column(String(20), default="", index=True)
    external_project_id: Mapped[str] = mapped_column(String(160), default="")
    external_task_id: Mapped[str] = mapped_column(String(160), default="")
    #: 平台账号池的路由留痕：本任务实际走的 provider_accounts 行；空串 = 未走账号池。
    provider_account_id: Mapped[str] = mapped_column(
        String(32), default="", server_default="", index=True
    )
    artifact_storage_key: Mapped[str] = mapped_column(String(500), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 待恢复任务的自动复查记账。
    reconcile_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str] = mapped_column(String(160), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="generations")
    project: Mapped[Project] = relationship(back_populates="generations")
    references: Mapped[list["ReferenceImage"]] = relationship(
        back_populates="generation", cascade="all, delete-orphan", order_by="ReferenceImage.position"
    )


class ReferenceImage(Base):
    __tablename__ = "reference_images"
    __table_args__ = (UniqueConstraint("generation_id", "position"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(ForeignKey("generations.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    original_name: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(500), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(80))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)

    generation: Mapped[Generation] = relationship(back_populates="references")


class AdminAuditEvent(Base):
    __tablename__ = "admin_audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(80), index=True)
    target_type: Mapped[str] = mapped_column(String(40), index=True)
    target_id: Mapped[str] = mapped_column(String(160), index=True)
    summary: Mapped[str] = mapped_column(String(500), default="")
    #: 操作来源：IP 用 45 字符装下 IPv6，UA 截到 200。
    actor_ip: Mapped[str] = mapped_column(String(45), default="", server_default="")
    actor_agent: Mapped[str] = mapped_column(String(200), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    actor: Mapped[User | None] = relationship(foreign_keys=[actor_user_id])


#: 参与「账号池」路由的平台渠道。api 渠道按 base_url 配置单一路由，没有账号概念。
PROVIDER_POOL_PROVIDERS = ("libtv", "lovart")


class ProviderAccount(Base):
    """路由是**固定顺序**的：按 ``position`` 升序，跳过 ``enabled=0`` 或 ``exhausted_at``"""

    __tablename__ = "provider_accounts"
    __table_args__ = (UniqueConstraint("provider", "position"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    #: 管理员可读的标签（「主号」「备用」）。空串也合法。
    label: Mapped[str] = mapped_column(String(120), default="", server_default="")
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    enabled: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    #: 非空 = 已被上游判为额度用尽，路由跳过它。没有自动恢复，只能由管理员手工清除。
    exhausted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exhausted_reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: 最近一次错误原文，用于校准额度错误识别词表。
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_message: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProviderRoutingCursor(Base):
    """平台与渠道当前的选号游标；单独一张表，推进退化为一条 UPDATE。"""

    __tablename__ = "provider_routing_cursors"

    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    current_account_id: Mapped[str] = mapped_column(String(32), default="", server_default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ApiRouteState(Base):
    """(模型, 路由) 的失效标记与最近一次错误。"""

    __tablename__ = "api_route_states"

    model_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    route: Mapped[str] = mapped_column(String(120), primary_key=True)
    exhausted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exhausted_reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_message: Mapped[str] = mapped_column(Text, default="", server_default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


#: 素材可见性取值：shared = 管理员上传、全员可见；private = 普通账号上传、仅本人与管理员可见。
ASSET_VISIBILITY_SHARED = "shared"
ASSET_VISIBILITY_PRIVATE = "private"


class Asset(Base):
    """账号级素材库的一条素材；管理员上传进共享库，普通账号上传默认私有。"""

    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    #: 实际上传者。私有素材据此判定。
    uploaded_by: Mapped[str] = mapped_column(String(32), index=True)
    visibility: Mapped[str] = mapped_column(
        String(16), default=ASSET_VISIBILITY_PRIVATE, index=True
    )
    original_name: Mapped[str] = mapped_column(String(255), default="")
    storage_key: Mapped[str] = mapped_column(String(500), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime_type: Mapped[str] = mapped_column(String(80), default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
