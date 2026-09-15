from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # 默认按当前用户的 home 目录解析，部署时用 IMAGE_HUB_LIBTV_CLI 覆盖。
    libtv_cli: Path = Path.home() / ".libtv" / "libtv"
    lovart_access_key: str = ""
    lovart_secret_key: str = ""
    lovart_base_url: str = "https://lgw.lovart.ai"
    lovart_skill_script: Path = Path(
        Path.home() / ".hermes" / "skills" / "creative" / "lovart-api" / "scripts" / "agent_skill.py"
    )
    openai_image_api_key: str = ""
    openai_image_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    openai_image_models: str = "doubao-seedream-5-0-pro-260628|Seedream 5.0 Pro"
    api_private_network_allowlist: str = ""
    worker_enabled: bool = True
    worker_poll_seconds: float = 1.0
    worker_stale_minutes: int = 60
    provider_command_timeout_seconds: int = 900

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
