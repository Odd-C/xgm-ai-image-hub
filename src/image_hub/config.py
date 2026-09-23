from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    """Anchor relative paths to the repo root, not the process working directory."""
    return Path(__file__).resolve().parents[2]


SQLITE_ABSOLUTE_PREFIX = "sqlite:///"
SQLITE_RELATIVE_PREFIX = "sqlite:///./"

#: 占位 Session 密钥。
PLACEHOLDER_SESSION_SECRETS = frozenset(
    {
        "development-only-change-me",
        "replace-with-a-long-random-secret",
    }
)
#: 占位初始管理员口令。``.env.example`` 里的样例值同样在列：照着样例 ``cp``
PLACEHOLDER_ADMIN_PASSWORDS = frozenset({"change-me-now", "change-me"})
#: Session 密钥的最小长度。
MIN_SESSION_SECRET_LENGTH = 32
#: 初始管理员口令的最小长度。与建号接口的 ``min_length=8`` 保持一致。
MIN_BOOTSTRAP_PASSWORD_LENGTH = 8


_GENERATE_SECRET_HINT = (
    'python -c "import secrets; print(secrets.token_urlsafe(48))"'
)


def assert_session_secret_is_strong(secret: str) -> None:
    """Session 签名的根密钥校验。"""
    clean = (secret or "").strip()
    if not clean or clean in PLACEHOLDER_SESSION_SECRETS:
        raise RuntimeError(
            "IMAGE_HUB_SESSION_SECRET 还是占位值。它是 Session 签名的根密钥，"
            "公开已知的取值意味着任何人都能伪造登录态。请在 .env 中改成随机值，例如："
            f"{_GENERATE_SECRET_HINT}"
        )
    if len(clean) < MIN_SESSION_SECRET_LENGTH:
        raise RuntimeError(
            f"IMAGE_HUB_SESSION_SECRET 至少需要 {MIN_SESSION_SECRET_LENGTH} 个字符，"
            f"当前只有 {len(clean)} 个。可以用：{_GENERATE_SECRET_HINT}"
        )


def assert_bootstrap_password_is_usable(password: str) -> None:
    """只在「真的要用它建初始管理员」时调用。"""
    clean = (password or "").strip()
    if not clean or clean in PLACEHOLDER_ADMIN_PASSWORDS:
        raise RuntimeError(
            "IMAGE_HUB_BOOTSTRAP_ADMIN_PASSWORD 还是占位值，拒绝用它创建初始管理员。"
            "请在 .env 中改成自己的强口令后重新启动。"
        )
    if len(clean) < MIN_BOOTSTRAP_PASSWORD_LENGTH:
        raise RuntimeError(
            f"IMAGE_HUB_BOOTSTRAP_ADMIN_PASSWORD 至少需要 "
            f"{MIN_BOOTSTRAP_PASSWORD_LENGTH} 个字符，当前只有 {len(clean)} 个。"
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="IMAGE_HUB_", extra="ignore")

    env: str = "development"
    database_url: str = "sqlite:///./data/image-hub.db"
    storage_root: Path = Path("storage")
    max_upload_bytes: int = 30 * 1024 * 1024
    max_request_upload_bytes: int = 80 * 1024 * 1024
    max_artifact_bytes: int = 40 * 1024 * 1024
    max_active_tasks_per_user: int = 5
    #: SQLite 写锁的等待上限（秒）。
    sqlite_busy_timeout_seconds: float = 30.0
    session_secret: str = "development-only-change-me"
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "change-me-now"
    # LibTV：官方 skill 的 scripts 目录。
    libtv_skill_scripts: Path = Path("")
    libtv_access_key: str = ""
    libtv_im_base: str = "https://im.liblib.tv"
    # LibTV Remote MCP：OAuth 2.1 + PKCE。实测要点见 libtv_mcp.py 模块注释。
    libtv_mcp_base: str = "https://mcp.liblib.tv"
    # redirect_uri 必须是回环地址（上游白名单只收 localhost/127.0.0.1）。
    libtv_oauth_redirect_uri: str = "http://localhost:8000/admin/providers/libtv/callback"
    lovart_access_key: str = ""
    lovart_secret_key: str = ""
    lovart_base_url: str = "https://lgw.lovart.ai"
    lovart_skill_script: Path = Path("")
    # Lovart 的推理模式：fast（轻量单次）或 thinking（深度多步）。
    lovart_thread_mode: str = "fast"
    # 单次生成允许等待上游多久（秒）；Lovart 的视频/深度推理任务耗时较长。
    lovart_chat_timeout_seconds: int = 1800
    native_provider_probe_seconds: int = 45
    openai_image_api_key: str = ""
    openai_image_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    openai_image_models: str = "doubao-seedream-5-0-pro-260628|Seedream 5.0 Pro"
    api_private_network_allowlist: str = ""
    # 素材库的存量上限。
    asset_max_per_tenant: int = 2000
    # 素材保留期（天）。
    asset_retention_days: int = 14
    # 清理节流：worker 每轮都全表扫太浪费，这里限制两次清理的最小间隔（分钟）。
    asset_purge_interval_minutes: int = 60
    # 提示词优化：调用**服务器上部署的本地 skill 脚本**，由脚本自己去调 LLM API。
    prompt_optimize_enabled: bool = False
    prompt_optimize_script: str = ""
    #: 脚本参数模板。占位符：{prompt_file} / {prompt} / {model}。
    prompt_optimize_args: str = "--prompt-file {prompt_file}"
    prompt_optimize_api_base_url: str = ""
    prompt_optimize_api_key: str = ""
    prompt_optimize_model: str = ""
    prompt_optimize_timeout_seconds: int = 120
    # Agent 模式（带参考图的多模态诊断 + 改写）。
    prompt_optimize_agent_enabled: bool = False
    prompt_optimize_agent_model: str = ""
    worker_enabled: bool = True
    worker_poll_seconds: float = 1.0
    worker_stale_minutes: int = 60
    # 同时执行的生成任务数。
    worker_concurrency: int = 4
    # 待恢复任务的自动复查。
    worker_reconcile_seconds: float = 15.0
    worker_reconcile_max_attempts: int = 240
    provider_command_timeout_seconds: int = 900
    # API 通道的回复方式。``async`` = 提交后立刻拿 task id 再轮询 ``/api/result``
    api_reply_mode: str = "async"
    api_poll_seconds: float = 3.0
    # 轮询上限。上游过载时任务会先 running 再失败，给足时间才不会把「还在排队」
    api_poll_timeout_seconds: int = 600
    # 上游过载（``The model load is too high``）属瞬时错误，退避后重提
    api_submit_retries: int = 3
    api_retry_backoff_seconds: float = 5.0

    @field_validator("worker_concurrency")
    @classmethod
    def _clamp_worker_concurrency(cls, value: int) -> int:
        """0 或负数会让 worker 一个槽位都不起，队列从此不动 —— 静默失效比报错更糟。"""
        return max(int(value), 1)

    @field_validator("api_reply_mode")
    @classmethod
    def _normalize_reply_mode(cls, value: str) -> str:
        mode = (value or "").strip().lower()
        if mode not in {"async", "sync"}:
            raise ValueError("IMAGE_HUB_API_REPLY_MODE 只能是 async 或 sync")
        return mode

    @field_validator("database_url")
    @classmethod
    def _anchor_sqlite_database(cls, value: str) -> str:
        """Anchor a relative SQLite file to the repo root."""
        if not value.startswith(SQLITE_RELATIVE_PREFIX):
            return value
        relative = value[len(SQLITE_RELATIVE_PREFIX):]
        absolute = (_project_root() / relative).resolve()
        return f"{SQLITE_ABSOLUTE_PREFIX}{absolute.as_posix()}"

    @field_validator("storage_root")
    @classmethod
    def _anchor_storage_root(cls, value: Path) -> Path:
        """Make storage_root absolute so provider subprocesses cannot reinterpret it."""
        return value if value.is_absolute() else (_project_root() / value).resolve()

    @property
    def package_dir(self) -> Path:
        return Path(__file__).resolve().parent

    @property
    def template_dir(self) -> Path:
        return self.package_dir / "templates"

    @property
    def static_dir(self) -> Path:
        return self.package_dir / "static"

    def sqlite_file_path(self) -> Path | None:
        """本地 SQLite 数据文件；非文件型后端（如 PostgreSQL）返回 None。"""
        if not self.database_url.startswith(SQLITE_ABSOLUTE_PREFIX):
            return None
        raw = self.database_url[len(SQLITE_ABSOLUTE_PREFIX):]
        if not raw or raw == ":memory:":
            return None
        return Path(raw)

    def ensure_directories(self) -> None:
        # 无条件校验 Session 密钥
        assert_session_secret_is_strong(self.session_secret)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        database_file = self.sqlite_file_path()
        if database_file is not None:
            database_file.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
