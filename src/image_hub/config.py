from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    """Anchor relative paths to the repo root, not the process working directory.

    ``.env`` and the launcher scripts treat paths as relative to the project, and
    ``settings`` is imported before anything chdir()s. Resolving here keeps every
    downstream path (storage, sqlite file) stable regardless of where the process
    was started from or which directory a child process later runs in.
    """
    return Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="IMAGE_HUB_", extra="ignore")

    env: str = "development"
    database_url: str = "sqlite:///./data/image-hub.db"
    storage_root: Path = Path("storage")
    max_upload_bytes: int = 30 * 1024 * 1024
    max_request_upload_bytes: int = 80 * 1024 * 1024
    max_artifact_bytes: int = 40 * 1024 * 1024
    max_active_tasks_per_user: int = 5
    session_secret: str = "development-only-change-me"
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "change-me-now"
    # LibTV：官方 agent-im skill 的 scripts 目录，以及 Bearer Access Key。
    libtv_skill_scripts: Path = (
        Path.home() / ".hermes" / "skills" / "creative" / "libtv-skill" / "scripts"
    )
    libtv_access_key: str = ""
    libtv_im_base: str = "https://im.liblib.tv"
    lovart_access_key: str = ""
    lovart_secret_key: str = ""
    lovart_base_url: str = "https://lgw.lovart.ai"
    lovart_skill_script: Path = Path(
        Path.home() / ".hermes" / "skills" / "creative" / "lovart-api" / "scripts" / "agent_skill.py"
    )
    # Lovart 的推理模式：fast（轻量单次）或 thinking（深度多步）。
    lovart_thread_mode: str = "fast"
    # 单次生成允许等待上游多久（秒）；Lovart 的视频/深度推理任务耗时较长。
    lovart_chat_timeout_seconds: int = 1800
    native_provider_probe_seconds: int = 45
    openai_image_api_key: str = ""
    openai_image_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    openai_image_models: str = "doubao-seedream-5-0-pro-260628|Seedream 5.0 Pro"
    api_private_network_allowlist: str = ""
    worker_enabled: bool = True
    worker_poll_seconds: float = 1.0
    worker_stale_minutes: int = 60
    provider_command_timeout_seconds: int = 900

    @field_validator("storage_root")
    @classmethod
    def _anchor_storage_root(cls, value: Path) -> Path:
        """Make storage_root absolute so provider subprocesses cannot reinterpret it.

        Skill scripts run with ``cwd`` set to their own directory, so handing them a
        relative ``--output-dir`` silently downloads artifacts *there* instead of
        into the project — and any later ``Path(...).is_file()`` check fails too.
        """
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

    def ensure_directories(self) -> None:
        if self.env == "production" and (
            self.session_secret == "development-only-change-me"
            or self.bootstrap_admin_password == "change-me-now"
        ):
            raise RuntimeError("生产环境必须修改 Session Secret 和初始管理员密码")
        self.storage_root.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite:///./"):
            Path(self.database_url.removeprefix("sqlite:///./")).parent.mkdir(
                parents=True, exist_ok=True
            )


settings = Settings()
