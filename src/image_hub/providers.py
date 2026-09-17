import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import object_session

from image_hub.config import settings
from image_hub.models import Generation, utcnow
from image_hub.storage import InvalidImage, resolve_storage_key, validate_artifact


@dataclass(frozen=True)
class ModelProfile:
    id: str
    label: str
    provider: str
    upstream_model: str
    enabled: bool
    supports_references: bool = True
    ratios: tuple[str, ...] = ("1:1", "4:3", "3:4", "16:9", "9:16")
    qualities: tuple[str, ...] = ("standard",)
    resolutions: tuple[str, ...] = ("2K",)
    max_references: int = 14
    execution_config_id: str = ""

    def public_dict(self) -> dict:
        """Return browser-safe selection metadata without upstream routing details."""
        payload = asdict(self)
        payload.pop("upstream_model", None)
        payload.pop("execution_config_id", None)
        # 某些模型（1:3 / 3:1）不是每个比例都有全部档位，前端据此收窄分辨率
        # 选择器，避免用户选到一个上游根本不存在的组合。
        payload["tiers"] = {
            ratio: list(_lovart_tiers_for(self.upstream_model, ratio))
            for ratio in self.ratios
        } if self.provider == "lovart" else {}
        return payload

    def snapshot_dict(self) -> dict:
        """Keep immutable server-side execution routing with the generation evidence."""
        return asdict(self)


# LibTV 现在由官方 agent-im skill 驱动：用户侧只负责传话，模型与提示词由上游 Agent
# 自动编排。因此这里不再逐个枚举上游模型，而是暴露「平台默认编排」这一个入口，
# 避免把用户侧掌握的模型 ID 当成白名单硬塞进 prompt。
LIBTV_MODELS = (
    ("agent", "LibTV 智能编排"),
)

# Lovart 暴露的是具体出图模型，所以这里逐个枚举。上游工具名与展示名来自官方 skill
# 的 SKILL.md「Available models for --prefer-models」表格，改这里前务必先核对那张表。
#
# 刻意只收「Auto」档 + 不带档位后缀的模型：Flare/Sunburst 各自还有
# _low/_medium/_high/_xhigh/_max 共 6 档，逐个上架会让下拉框塞进 30+ 项，
# 而质量档位本质是成本与保真度的取舍——真正的差异化是「用哪个模型」。
# 默认走 Auto 由上游按提示词自己选档，需要精细控制时再由管理员按需扩充。
LOVART_MODELS = (
    ("gpt-image-2-5-flare", "GPT Image 2.5 Flare Auto", "generate_image_gpt_image_2_5_flare"),
    (
        "gpt-image-2-5-sunburst", "GPT Image 2.5 Sunburst Auto",
        "generate_image_gpt_image_2_5_sunburst",
    ),
    # VIP 不在本仓库随附的那份 SKILL.md 表里（它按 2026-09-15 的快照生成），
    # 但账号侧确实有这个模型，且官方把它和 Flare/Sunburst 放在同一张
    # 1K/2K/4K 比例表下，所以工具名按同一命名惯例推导。若上游日后改名，
    # 只需改这一行的第三个元素。
    ("gpt-image-2-vip", "GPT Image 2 VIP", "generate_image_gpt_image_2_vip"),
    ("gpt-image-2", "GPT Image 2 Auto", "generate_image_gpt_image_2"),
    ("gpt-image-1-5", "GPT Image 1.5", "generate_image_gpt_image_1_5"),
    ("nano-banana-pro", "Nano Banana Pro", "generate_image_nano_banana_pro"),
    ("nano-banana-2", "Nano Banana 2", "generate_image_nano_banana_2"),
    ("nano-banana-2-lite", "Nano Banana 2 Lite", "generate_image_nano_banana_2_lite"),
    ("nano-banana", "Nano Banana", "generate_image_nano_banana"),
    ("seedream-5-pro", "Seedream 5.0 Pro", "generate_image_seedream_v5_pro"),
    ("seedream-5-lite", "Seedream 5.0 Lite", "generate_image_seedream_v5"),
    ("seedream-4-5", "Seedream 4.5", "generate_image_seedream_v4_5"),
    ("seedream-4", "Seedream 4", "generate_image_seedream_v4"),
    ("flux-2-max", "Flux.2 Max", "generate_image_flux_2_max"),
    ("flux-2-pro", "Flux.2 Pro", "generate_image_flux_2_pro"),
    ("luma-uni-1", "Luma uni-1", "generate_image_luma_uni_1"),
    ("luma-uni-1-max", "Luma uni-1-max", "generate_image_luma_uni_1_max"),
    ("midjourney", "Midjourney", "generate_image_midjourney"),
    ("ideogram-4", "Ideogram 4", "generate_image_ideogram_v4"),
    ("ideogram-p-image", "Ideogram P-Image", "generate_image_p_image_ideogram"),
    ("qwen-image3", "Qwen Image3", "generate_image_qwen_image3"),
    ("qwen-image3-pro", "Qwen Image3 Pro", "generate_image_qwen_image3_pro"),
)

# 从 LOVART_MODELS 派生，避免「模型清单改了、尺寸分派忘了改」这种漂移。
_LOVART_UPSTREAM_TOOLS = frozenset(tool for _, _, tool in LOVART_MODELS)
_LOVART_GPT_IMAGE_2_5_TOOLS = frozenset(
    tool for tool in _LOVART_UPSTREAM_TOOLS if tool.startswith("generate_image_gpt_image_2_5")
)
# 走 1K/2K/4K 三档比例表的模型。Flare/Sunburst 靠前缀匹配，VIP 需要显式列出，
# 因为它的工具名不以 _2_5 开头，却又共用同一张比例表。
_LOVART_THREE_TIER_TOOLS = _LOVART_GPT_IMAGE_2_5_TOOLS | {
    "generate_image_gpt_image_2_vip",
}
_LOVART_NANO_BANANA_TOOLS = frozenset(
    tool for tool in _LOVART_UPSTREAM_TOOLS if tool.startswith("generate_image_nano_banana")
)
_SINGLE_TIER_TOOL = "generate_image_gpt_image_2"

# Lovart 的尺寸能力面。⚠️ `chat` 子命令**没有任何尺寸参数**（只有 --prompt /
# --project-id / --prefer-models / --attachments / --mode / --output-dir 等），
# 所以比例和分辨率只能以文字形式随提示词下发。下面的表就是「用户选了 1K + 16:9，
# 实际要向上游声明哪组像素」的唯一真相来源。
#
# GPT-image 系的上游硬约束（超出即报错，不是自动裁剪）：
#   最大边长 ≤ 3840px / 两条边都是 16 的倍数 / 长边:短边 ≤ 3:1 /
#   总像素 655,360 ~ 8,294,400
# 下面每一组都已按此校验过，改数值前请重新验算（见 tests 里的约束测试）。
#
# 键是比例，值是「1K / 2K / 4K」三档像素。1:3 与 3:1 上游只给两档，
# 用 None 占位表示该档位不可用 —— 不要为了整齐而补一个上游不认的值。
LOVART_SIZES_GPT_IMAGE_2_5: dict[str, tuple[str | None, str | None, str | None]] = {
    "1:1": ("1024x1024", "2048x2048", "2880x2880"),
    "16:9": ("1280x720", "2048x1152", "3840x2160"),
    "9:16": ("720x1280", "1152x2048", "2160x3840"),
    "4:3": ("1152x864", "2304x1728", "3264x2448"),
    "3:4": ("864x1152", "1728x2304", "2448x3264"),
    "3:2": ("1536x1024", "2048x1360", "3504x2336"),
    "2:3": ("1024x1536", "1360x2048", "2336x3504"),
    "5:4": ("1120x896", "2240x1792", "3200x2560"),
    "4:5": ("896x1120", "1792x2240", "2560x3200"),
    "21:9": ("1456x624", "2912x1248", "3840x1648"),
    "9:21": ("624x1456", "1248x2912", "1648x3840"),
    "1:3": ("688x2048", "1280x3840", None),
    "3:1": ("2048x688", "3840x1280", None),
    "2:1": ("1536x768", "3072x1536", "3840x1920"),
    "1:2": ("768x1536", "1536x3072", "1920x3840"),
}

# gpt-image-2 只有单档：它不认 1K/2K/4K，每个比例固定一组像素。
LOVART_SIZES_GPT_IMAGE_2: dict[str, str] = {
    "1:1": "1024x1024",
    "16:9": "1672x941",
    "9:16": "941x1672",
    "4:3": "1443x1090",
    "3:4": "1090x1443",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
    "5:4": "1408x1120",
    "4:5": "1120x1408",
    "21:9": "1920x832",
    "9:21": "832x1920",
    "1:2": "896x1792",
    "2:1": "1792x896",
}

# 分辨率档位。Nano Banana 系没有逐比例的官方像素表，只需声明档位即可。
LOVART_RESOLUTIONS = ("1K", "2K", "4K")

# 单档模型（gpt-image-2）没有档位概念，但前端的组合选择器需要一个非空值才能
# 渲染，所以给一个中性的占位而不是硬塞「1K」这种会误导人的档位名。
LOVART_SINGLE_TIER = ("自动",)

# gpt-image-2.5 系支持的面板比通用组宽得多（15 种比例），所以单独声明。
# 顺序刻意与官方表一致，方便和 SKILL.md 对照。
LOVART_RATIOS_GPT_IMAGE_2_5 = (
    "auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5",
    "21:9", "9:21", "1:3", "3:1", "2:1", "1:2",
)

# gpt-image-2 是单档模型：比例更多，但不存在 1K/2K/4K 的选择。
LOVART_RATIOS_GPT_IMAGE_2 = (
    "auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5",
    "21:9", "9:21", "1:2", "2:1",
)

# 其余模型（Seedream / Flux / Luma / Midjourney / Ideogram / Qwen）官方没给逐比例
# 像素表，统一按这两组开放。
LOVART_RATIOS = ("1:1", "4:3", "3:4", "16:9", "9:16")


def _lovart_capabilities(upstream_model: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the ``(ratios, resolutions)`` a model actually accepts.

    The picker renders straight from these two tuples, so anything listed here
    must be genuinely usable — otherwise the user can select a combination the
    upstream will silently ignore. ``auto`` means "let the agent decide" and is
    therefore valid for every model.

    ``ratios`` is trimmed to the entries that carry a size for at least one
    tier, so a ratio whose every tier is ``None`` is never offered.
    """
    if upstream_model in _LOVART_THREE_TIER_TOOLS:
        ratios = tuple(
            ratio
            for ratio in LOVART_RATIOS_GPT_IMAGE_2_5
            if ratio == "auto" or any(LOVART_SIZES_GPT_IMAGE_2_5.get(ratio, ()))
        )
        return ratios, LOVART_RESOLUTIONS
    if upstream_model == _SINGLE_TIER_TOOL:
        # 单档模型：分辨率只有一个占位档，比例由像素表决定。
        return LOVART_RATIOS_GPT_IMAGE_2, LOVART_SINGLE_TIER
    return LOVART_RATIOS, LOVART_RESOLUTIONS


def _lovart_tiers_for(upstream_model: str, ratio: str) -> tuple[str, ...]:
    """Narrow the resolution chips to the tiers that ratio actually supports.

    ``1:3`` and ``3:1`` only exist at 1K/2K upstream, so offering a 4K button
    there would let the user pick a size that silently falls back to whatever
    the agent feels like returning.

    Single-tier models keep their one neutral placeholder for every ratio —
    there is no tier axis to narrow.
    """
    if upstream_model == _SINGLE_TIER_TOOL:
        return LOVART_SINGLE_TIER
    if upstream_model not in _LOVART_THREE_TIER_TOOLS or ratio == "auto":
        return LOVART_RESOLUTIONS
    table = LOVART_SIZES_GPT_IMAGE_2_5.get(ratio)
    if not table:
        return LOVART_RESOLUTIONS
    return tuple(
        tier for tier, pixels in zip(LOVART_RESOLUTIONS, table) if pixels
    ) or LOVART_RESOLUTIONS


def _lovart_size_hint(upstream_model: str, ratio: str, resolution: str) -> str:
    """Describe the requested canvas in the only channel the skill exposes: text.

    Returning an empty string means "this model exposes no table for that
    combination", and the caller leaves the prompt untouched rather than
    inventing a size the upstream never published.
    """
    if upstream_model == _SINGLE_TIER_TOOL:
        pixels = LOVART_SIZES_GPT_IMAGE_2.get(ratio)
        return f"{ratio}（{pixels}）" if pixels else ""
    if upstream_model in _LOVART_THREE_TIER_TOOLS:
        table = LOVART_SIZES_GPT_IMAGE_2_5.get(ratio)
        if not table:
            return ""
        pixels = table[LOVART_RESOLUTIONS.index(resolution)]
        if not pixels:
            return ""
        return f"{ratio}（{resolution}，{pixels}）"
    # Nano Banana 及其余模型只有档位，没有逐比例像素表。
    return f"{ratio}（{resolution}）"



class ProviderConfigError(ValueError):
    """An administrator supplied an unsafe or unsupported provider configuration."""


API_RATIOS = frozenset({"1:1", "4:3", "3:4", "16:9", "9:16"})
API_RESOLUTIONS = frozenset({"1K", "2K", "4K"})
API_QUALITIES = frozenset({"standard"})
_API_ENABLED_VALUES = frozenset({"1", "true", "on", "启用"})
_API_DISABLED_VALUES = frozenset({"0", "false", "off", "停用"})
_NATIVE_CREDENTIALS_FILE = "native-provider-credentials.json"
_CONFIG_ID_LENGTH = 32


def _api_config() -> dict:
    """Load optional admin-managed API routing from server-only storage."""
    path = settings.storage_root / "provider-config.json"
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "base_url": settings.openai_image_base_url,
        "api_key": settings.openai_image_api_key,
        "models": settings.openai_image_models,
    }


def _atomic_write_private_json(path: Path, payload: dict) -> None:
    """Atomically replace a private JSON file that is mode 0600 from creation."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # POSIX needs an explicit fsync on the parent directory to make the rename
        # itself durable. Windows cannot open a directory as a file handle, and its
        # os.replace already flushes the rename, so the call is skipped there.
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _native_credentials() -> dict:
    path = settings.storage_root / "private" / _NATIVE_CREDENTIALS_FILE
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _validated_secret(value: str, label: str, *, allow_empty: bool = False) -> str:
    if any(character in value for character in ("\r", "\n", "\0")):
        raise ProviderConfigError(f"{label} 不得包含换行或 NUL 字符")
    normalized = value.strip()
    if not normalized and allow_empty:
        return ""
    if not 1 <= len(normalized) <= 1000:
        raise ProviderConfigError(f"{label} 长度必须在 1 到 1000 个字符之间")
    return normalized


def _validated_path(value: str, label: str, *, expect_dir: bool) -> str:
    """Accept an absolute filesystem path for a skill script or scripts directory."""
    if any(character in value for character in ("\r", "\n", "\0")):
        raise ProviderConfigError(f"{label} 不得包含换行或 NUL 字符")
    normalized = value.strip()
    if not normalized:
        return ""
    if len(normalized) > 500:
        raise ProviderConfigError(f"{label} 路径过长")
    path = Path(normalized)
    if not path.is_absolute():
        raise ProviderConfigError(f"{label} 必须是绝对路径")
    if expect_dir:
        if not path.is_dir():
            raise ProviderConfigError(f"{label} 目录不存在")
    elif path.suffix.lower() != ".py":
        raise ProviderConfigError(f"{label} 必须指向一个 .py 脚本")
    elif not path.is_file():
        raise ProviderConfigError(f"{label} 脚本文件不存在")
    return str(path)


def _save_native_credentials(payload: dict) -> None:
    _atomic_write_private_json(
        settings.storage_root / "private" / _NATIVE_CREDENTIALS_FILE, payload
    )


def save_libtv_credentials(
    token: str, scripts_dir: str = "", *, clear: bool = False
) -> str:
    """Update the admin-managed LibTV contract: access key plus skill scripts dir."""
    payload = _native_credentials()
    for value in (token, scripts_dir):
        if any(character in value for character in ("\r", "\n", "\0")):
            raise ProviderConfigError("LibTV 配置不得包含换行或 NUL 字符")
    if clear:
        if token.strip() or scripts_dir.strip():
            raise ProviderConfigError("清除 LibTV 配置时不能同时提交新值")
        payload.pop("libtv_token", None)
        payload.pop("libtv_scripts_dir", None)
        action = "cleared"
    elif token.strip() or scripts_dir.strip():
        if token.strip():
            payload["libtv_token"] = _validated_secret(token, "LibTV Access Key")
        if scripts_dir.strip():
            payload["libtv_scripts_dir"] = _validated_path(
                scripts_dir, "LibTV skill 脚本目录", expect_dir=True
            )
        action = "saved"
    else:
        # 「留空保持不变」必须真的什么都不做。此前这里也会落盘，于是「密码框留空 +
        # 点保存」会把已有凭据覆盖成空 payload —— 一个纯误触就能清掉生产凭据。
        return "kept"
    _save_native_credentials(payload)
    return action


def save_lovart_credentials(
    access_key: str,
    secret_key: str,
    skill_script: str = "",
    *,
    clear: bool = False,
) -> str:
    """Update the admin-managed Lovart contract: credential pair plus skill script."""
    payload = _native_credentials()
    if any(
        character in value
        for value in (access_key, secret_key, skill_script)
        for character in ("\r", "\n", "\0")
    ):
        raise ProviderConfigError("Lovart 配置不得包含换行或 NUL 字符")
    if clear:
        if access_key.strip() or secret_key.strip() or skill_script.strip():
            raise ProviderConfigError("清除 Lovart 配置时不能同时提交新值")
        payload.pop("lovart_access_key", None)
        payload.pop("lovart_secret_key", None)
        payload.pop("lovart_skill_script", None)
        action = "cleared"
    elif access_key.strip() or secret_key.strip() or skill_script.strip():
        if bool(access_key.strip()) != bool(secret_key.strip()):
            raise ProviderConfigError("Lovart Access Key 与 Secret Key 必须成对填写")
        if access_key.strip():
            payload["lovart_access_key"] = _validated_secret(access_key, "Lovart Access Key")
            payload["lovart_secret_key"] = _validated_secret(secret_key, "Lovart Secret Key")
        if skill_script.strip():
            payload["lovart_skill_script"] = _validated_path(
                skill_script, "Lovart skill 脚本", expect_dir=False
            )
        action = "saved"
    else:
        # 同上：留空即「保持不变」，不能落盘，否则会清掉已保存的一对凭据。
        return "kept"
    _save_native_credentials(payload)
    return action


# ── LibTV / Lovart 官方 skill 脚本的唯一入口 ─────────────────────────────
# 这两个平台不再依赖外部 CLI，而是直接调用官方 skill 里的脚本：
#   LibTV  : <scripts>/create_session.py|query_session.py|download_results.py|upload_file.py
#   Lovart : <script>/agent_skill.py
# 这样部署时只需要把 skill 目录放到磁盘上，不需要额外安装任何可执行文件。

_SKILL_SCRIPT_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+\.py$")


def _libtv_script_path(command: str) -> Path:
    """Resolve one LibTV skill script by command name, refusing to escape the dir."""
    file_name = command if command.endswith(".py") else f"{command}.py"
    if not _SKILL_SCRIPT_SEGMENT.match(file_name):
        raise RuntimeError("LibTV skill 脚本名不受支持")
    return settings.libtv_skill_scripts / file_name


def _python_executable() -> str:
    """Use the interpreter that runs this service so the scripts need no PATH entry.

    Windows has no ``python3`` alias by default, so the skill docs' ``python3``
    invocation cannot be used verbatim there.
    """
    return sys.executable or "python3"


def _probe_project_script_interpreter(path: Path) -> None:
    """Allow a self-contained project Python (scripts/ directory next to it)."""
    scripts_dir = path.with_name("scripts")
    if not scripts_dir.is_dir():
        return
    for sub_dir in scripts_dir.iterdir():
        if sub_dir.is_dir() and sub_dir.name.startswith("python"):
            candidate = sub_dir / ("python.exe" if os.name == "nt" else "bin/python3")
            if candidate.is_file():
                site = str(path.with_name("Scripts"))
                if os.path.isdir(site) and site not in sys.path:
                    sys.path.insert(0, site)
                return


def _run_skill_script(
    script: Path,
    args: list[str],
    *,
    env_overrides: dict[str, str],
    timeout: int,
    secret_values: tuple[str, ...] = (),
    script_dir: Path | None = None,
) -> str:
    """Run an official skill script and return its stdout, failing closed on error."""
    if not script.is_file():
        raise RuntimeError(f"未找到 skill 脚本：{script}")
    if script_dir is not None:
        _probe_project_script_interpreter(script)
    env = _skill_child_env(env_overrides)
    try:
        completed = subprocess.run(
            [_python_executable(), str(script), *args],
            capture_output=True, text=True, check=False, env=env, timeout=timeout,
            cwd=str(script.parent),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("skill 脚本执行超时") from exc
    except OSError as exc:
        raise RuntimeError("skill 脚本无法启动") from exc
    if completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip() or "skill 脚本执行失败"
        raise RuntimeError(_sanitize_provider_output(message, *secret_values))
    return completed.stdout


def _skill_child_env(overrides: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(overrides)
    # 代理会破坏脚本对上游 API 的直连，子进程里统一清掉。
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    # 保证脚本能 import 自己的 _common
    return env


def _parse_skill_json(stdout: str, error_message: str) -> dict:
    """Pick the last JSON object printed by a skill script (they may log first)."""
    stripped = stdout.strip()
    if not stripped:
        raise RuntimeError(error_message)
    try:
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(stripped):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError(error_message)



def _runtime_secret(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return _validated_secret(value, "平台凭据", allow_empty=True)
    except ProviderConfigError:
        return ""


def _resolved_libtv_token() -> tuple[str, str]:
    managed = _runtime_secret(_native_credentials().get("libtv_token", ""))
    if managed:
        return managed, "管理后台已配置"
    # LibTV skill 脚本只认 LIBTV_ACCESS_KEY / LIBTV_TOKEN 环境变量，不读 CLI 凭据文件。
    environment = _runtime_secret(
        settings.libtv_access_key
        or os.environ.get("LIBTV_ACCESS_KEY", "")
        or os.environ.get("LIBTV_TOKEN", "")
    )
    if environment:
        return environment, "环境已配置"
    return "", "未配置"


def _resolved_libtv_scripts_dir() -> tuple[Path, str]:
    """Resolve the LibTV skill scripts directory: admin value, then environment."""
    managed = _native_credentials().get("libtv_scripts_dir", "")
    if isinstance(managed, str) and managed.strip():
        return Path(managed.strip()), "管理后台已配置"
    return settings.libtv_skill_scripts, (
        "环境已配置" if settings.libtv_skill_scripts.is_dir() else "未配置"
    )


def _resolved_lovart_skill_script() -> tuple[Path, str]:
    """Resolve the Lovart skill script path: admin value, then environment."""
    managed = _native_credentials().get("lovart_skill_script", "")
    if isinstance(managed, str) and managed.strip():
        return Path(managed.strip()), "管理后台已配置"
    return settings.lovart_skill_script, (
        "环境已配置" if settings.lovart_skill_script.is_file() else "未配置"
    )


def _resolved_lovart_credentials() -> tuple[str, str, str]:
    payload = _native_credentials()
    managed_access = _runtime_secret(payload.get("lovart_access_key", ""))
    managed_secret = _runtime_secret(payload.get("lovart_secret_key", ""))
    if managed_access and managed_secret:
        return managed_access, managed_secret, "管理后台已配置"
    environment_access = _runtime_secret(settings.lovart_access_key)
    environment_secret = _runtime_secret(settings.lovart_secret_key)
    if environment_access and environment_secret:
        return environment_access, environment_secret, "环境已配置"
    return "", "", "未配置"


def public_native_credentials() -> dict:
    """Return only booleans and safe source labels for the admin page."""
    libtv_token, libtv_source = _resolved_libtv_token()
    lovart_access, lovart_secret, lovart_source = _resolved_lovart_credentials()
    libtv_dir, libtv_dir_source = _resolved_libtv_scripts_dir()
    lovart_script, lovart_script_source = _resolved_lovart_skill_script()
    return {
        "has_libtv_token": bool(libtv_token),
        "has_lovart_access_key": bool(lovart_access),
        "has_lovart_secret_key": bool(lovart_secret),
        "libtv_source": libtv_source,
        "lovart_source": lovart_source,
        "libtv_scripts_label": f"{libtv_dir}（{libtv_dir_source}）",
        "lovart_scripts_label": f"{lovart_script}（{lovart_script_source}）",
        # 输入框只给占位提示，不回显已保存的绝对路径。
        "libtv_scripts_placeholder": (
            str(libtv_dir) if libtv_dir_source != "未配置" else "/path/to/libtv-skill/scripts"
        ),
        "lovart_scripts_placeholder": (
            str(lovart_script)
            if lovart_script_source != "未配置"
            else "/path/to/lovart-api/scripts/agent_skill.py"
        ),
    }


def _parse_allowlist() -> tuple[set[str], tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]]:
    hosts: set[str] = set()
    networks = []
    for raw in settings.api_private_network_allowlist.split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            hosts.add(entry.rstrip("."))
    return hosts, tuple(networks)


def _resolve_and_validate_endpoint(base_url: str) -> tuple[str, ...]:
    parsed = urlsplit(base_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ProviderConfigError("API Base URL 必须是有效的 HTTPS 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderConfigError("API Base URL 不得包含凭证、查询参数或片段")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ProviderConfigError("API Base URL 端口无效") from exc
    hostname = parsed.hostname.rstrip(".").lower()
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ProviderConfigError("API Base URL 域名无法解析") from exc
    if not addresses:
        raise ProviderConfigError("API Base URL 域名未解析到地址")
    allowed_hosts, allowed_networks = _parse_allowlist()
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ProviderConfigError("API Base URL 域名解析结果无效") from exc
        allowlisted_private = hostname in allowed_hosts or any(
            ip in network for network in allowed_networks
        )
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
        ):
            raise ProviderConfigError("API Base URL 不得指向本机、链路本地或保留地址")
        if ip.is_private:
            if not allowlisted_private:
                raise ProviderConfigError("API Base URL 指向内网；需由服务端 allowlist 明确授权")
        elif not ip.is_global:
            raise ProviderConfigError("API Base URL 不得指向非公网地址")
    return tuple(sorted(addresses))


def _parse_api_models(models: str) -> tuple[dict, ...]:
    parsed_models = []
    seen = set()
    raw_models = models.split(",")
    if any(not item.strip() for item in raw_models):
        raise ProviderConfigError("API 模型配置包含空行或多余逗号")
    for raw_item in raw_models:
        fields = [field.strip() for field in raw_item.split("|")]
        if len(fields) > 7 or not fields[0] or any("\n" in field or "\r" in field for field in fields):
            raise ProviderConfigError("API 模型配置格式无效")
        model_id = fields[0]
        if any(character.isspace() for character in model_id):
            raise ProviderConfigError("API 模型 ID 不得包含空白字符")
        if model_id in seen:
            raise ProviderConfigError("API 模型 ID 不得重复")
        seen.add(model_id)
        ratios = (
            tuple(filter(None, fields[2].split(";")))
            if len(fields) > 2
            else tuple(sorted(API_RATIOS))
        )
        resolutions = (
            tuple(filter(None, fields[3].split(";")))
            if len(fields) > 3
            else tuple(sorted(API_RESOLUTIONS))
        )
        if not ratios or any(value not in API_RATIOS for value in ratios):
            raise ProviderConfigError("API 模型包含执行器不支持的比例")
        if not resolutions or any(value not in API_RESOLUTIONS for value in resolutions):
            raise ProviderConfigError("API 模型包含执行器不支持的分辨率")
        try:
            max_references = int(fields[4]) if len(fields) > 4 else 4
        except ValueError as exc:
            raise ProviderConfigError("API 模型最大参考图数量必须是整数") from exc
        if not 0 <= max_references <= 14:
            raise ProviderConfigError("API 模型最大参考图数量必须在 0 到 14 之间")
        enabled_value = fields[5].lower() if len(fields) > 5 else "true"
        if enabled_value not in _API_ENABLED_VALUES | _API_DISABLED_VALUES:
            raise ProviderConfigError("API 模型启用状态必须是 true 或 false")
        qualities = (
            tuple(filter(None, fields[6].split(";")))
            if len(fields) > 6
            else tuple(sorted(API_QUALITIES))
        )
        if not qualities or any(value not in API_QUALITIES for value in qualities):
            raise ProviderConfigError("API 模型包含执行器不支持的质量参数")
        parsed_models.append(
            {
                "model_id": model_id,
                "label": (
                    fields[1]
                    if len(fields) > 1 and fields[1] and fields[1] != model_id
                    else f"API 模型 {len(parsed_models) + 1}"
                ),
                "ratios": ratios,
                "resolutions": resolutions,
                "qualities": qualities,
                "max_references": max_references,
                "enabled": enabled_value in _API_ENABLED_VALUES,
            }
        )
    if not parsed_models:
        raise ProviderConfigError("至少配置一个 API 模型")
    return tuple(parsed_models)


def _validated_api_config(base_url: str, api_key: str, models: str) -> dict:
    normalized_url = base_url.strip().rstrip("/")
    resolved_ips = _resolve_and_validate_endpoint(normalized_url)
    _parse_api_models(models.strip())
    return {
        "base_url": normalized_url,
        "api_key": api_key.strip(),
        "models": models.strip(),
        "resolved_ips": list(resolved_ips),
    }


def save_api_config(base_url: str, api_key: str, models: str) -> None:
    """Validate and atomically persist server-only credentials with mode 0600."""
    current = _api_config()
    payload = _validated_api_config(
        base_url,
        api_key.strip() or str(current.get("api_key", "")),
        models,
    )
    _atomic_write_private_json(settings.storage_root / "provider-config.json", payload)


def public_api_config() -> dict:
    """Expose configuration state without returning any routing or credential material."""
    config = _api_config()
    return {
        "has_base_url": bool(config.get("base_url")),
        "has_api_key": bool(config.get("api_key")),
        "has_models": bool(config.get("models")),
    }


def _execution_config_id(config: dict) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(settings.session_secret.encode(), encoded, hashlib.sha256).hexdigest()[:32]


def _freeze_api_execution_config() -> str:
    current = _api_config()
    config = _validated_api_config(
        str(current.get("base_url", "")),
        str(current.get("api_key", "")),
        str(current.get("models", "")),
    )
    identity = _execution_config_id(config)
    path = settings.storage_root / "provider-configs" / f"{identity}.json"
    if not path.exists():
        _atomic_write_private_json(path, config)
    return identity


def _current_native_execution_config(provider: str) -> dict:
    if provider == "libtv":
        token, source = _resolved_libtv_token()
        if not token:
            raise ProviderConfigError("LibTV 凭据未配置")
        scripts_dir, _ = _resolved_libtv_scripts_dir()
        if not (scripts_dir / "create_session.py").is_file():
            raise ProviderConfigError("LibTV skill 脚本目录未配置或缺少脚本")
        return {
            "provider": "libtv",
            "token": token,
            "source": source,
            "scripts_dir": str(scripts_dir),
            "im_base": settings.libtv_im_base,
            "chat_timeout": settings.lovart_chat_timeout_seconds,
        }
    if provider == "lovart":
        access_key, secret_key, source = _resolved_lovart_credentials()
        if not access_key or not secret_key:
            raise ProviderConfigError("Lovart 凭据未成对配置")
        skill_script, _ = _resolved_lovart_skill_script()
        if not skill_script.is_file():
            raise ProviderConfigError("Lovart skill 脚本未配置或文件不存在")
        return {
            "provider": "lovart",
            "access_key": access_key,
            "secret_key": secret_key,
            "source": source,
            "base_url": settings.lovart_base_url,
            "skill_script": str(skill_script),
            "mode": settings.lovart_thread_mode,
            "chat_timeout": settings.lovart_chat_timeout_seconds,
        }
    raise ProviderConfigError("不支持冻结该平台配置")


def _freeze_native_execution_config(provider: str) -> str:
    config = _current_native_execution_config(provider)
    identity = _execution_config_id(config)
    path = settings.storage_root / "provider-configs" / f"{identity}.json"
    if path.exists():
        _load_native_execution_config(identity, provider)
    else:
        _atomic_write_private_json(path, config)
    return identity


def freeze_profile_execution(profile: ModelProfile) -> ModelProfile:
    """Attach an immutable server-only route identity before a task is queued.

    A profile that already carries an identity is kept as-is, but its backing
    private config is still (re)materialised: the identity is derived from the
    live admin config, while the private file is only ever written on this path,
    so trusting the identity without restoring the file would strand the task
    with an unresolvable route.
    """
    if profile.provider not in {"api", "libtv", "lovart"}:
        return profile
    try:
        if profile.provider == "api":
            identity = _freeze_api_execution_config()
        else:
            identity = _freeze_native_execution_config(profile.provider)
    except ProviderConfigError:
        # No usable route is configured right now. Queueing must not fail here:
        # the task is durable and the executor reports the misconfiguration as a
        # normal task failure once it runs, so the operator sees it on the task
        # rather than as an opaque submit-time 503.
        return profile
    if profile.execution_config_id:
        if profile.execution_config_id != identity:
            raise ProviderConfigError("平台配置在提交期间发生变化，请重新确认模型后提交")
        return profile
    return replace(profile, execution_config_id=identity)


def _valid_config_identity(identity: str) -> bool:
    return len(identity) == _CONFIG_ID_LENGTH and all(
        character in "0123456789abcdef" for character in identity
    )


def _load_private_execution_config(identity: str, error_prefix: str) -> dict:
    if not _valid_config_identity(identity):
        raise RuntimeError(f"{error_prefix}任务缺少有效的冻结配置标识")
    path = settings.storage_root / "provider-configs" / f"{identity}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{error_prefix}任务的冻结配置不存在或已损坏") from exc
    if not isinstance(payload, dict) or _execution_config_id(payload) != identity:
        raise RuntimeError(f"{error_prefix}任务的冻结配置校验失败")
    return payload


def _load_native_execution_config(identity: str, provider: str) -> dict:
    payload = _load_private_execution_config(identity, provider.capitalize())
    if payload.get("provider") != provider:
        raise RuntimeError("任务冻结配置的平台不匹配")
    required = (
        ("token", "scripts_dir")
        if provider == "libtv"
        else ("access_key", "secret_key", "base_url", "skill_script")
    )
    if any(not isinstance(payload.get(field), str) or not payload[field] for field in required):
        raise RuntimeError("任务冻结配置内容无效")
    return payload


def _load_api_execution_config(identity: str) -> dict:
    payload = _load_private_execution_config(identity, "API ")
    current_ips = _resolve_and_validate_endpoint(str(payload.get("base_url", "")))
    if tuple(payload.get("resolved_ips", ())) != current_ips:
        raise RuntimeError("API 上游 DNS 已变化，已阻止可能的重绑定请求")
    return payload


def model_profiles() -> tuple[ModelProfile, ...]:
    libtv_token, _ = _resolved_libtv_token()
    libtv_scripts, _ = _resolved_libtv_scripts_dir()
    libtv_ready = bool(libtv_token and (libtv_scripts / "create_session.py").is_file())
    profiles = [
        ModelProfile(
            id=f"libtv:{key}", label=label, provider="libtv", upstream_model=label,
            enabled=libtv_ready,
            ratios=("1:1", "4:3", "3:4", "16:9", "9:16"),
            resolutions=("1K", "2K", "4K"),
        )
        for key, label in LIBTV_MODELS
    ]
    lovart_access_key, lovart_secret_key, _ = _resolved_lovart_credentials()
    lovart_script, _ = _resolved_lovart_skill_script()
    lovart_ready = bool(
        lovart_access_key and lovart_secret_key and lovart_script.is_file()
    )
    profiles.extend(
        ModelProfile(
            id=f"lovart:{key}", label=label, provider="lovart",
            upstream_model=upstream_model, enabled=lovart_ready,
            # 能力面按模型分派：gpt-image-2.5 系能出 15 种比例，
            # gpt-image-2 是单档（只有一种分辨率），其余按 1K/2K/4K 走。
            ratios=_lovart_capabilities(upstream_model)[0],
            resolutions=_lovart_capabilities(upstream_model)[1],
        )
        for key, label, upstream_model in LOVART_MODELS
    )
    api_config = _api_config()
    try:
        api_models = _parse_api_models(str(api_config.get("models", "")))
    except ProviderConfigError:
        api_models = ()
    for item in api_models:
        public_id = hashlib.sha256(item["model_id"].encode()).hexdigest()[:16]
        profiles.append(
            ModelProfile(
                id=f"api:{public_id}",
                label=item["label"],
                provider="api",
                upstream_model=item["model_id"],
                enabled=bool(
                    item["enabled"] and api_config.get("api_key") and api_config.get("base_url")
                ),
                ratios=item["ratios"],
                resolutions=item["resolutions"],
                qualities=item["qualities"],
                max_references=item["max_references"],
                execution_config_id=(
                    _execution_config_id(api_config) if api_config.get("resolved_ips") else ""
                ),
            )
        )
    return tuple(profiles)


def get_profile(profile_id: str) -> ModelProfile | None:
    """Resolve opaque and legacy IDs, failing closed if their meanings collide."""
    profiles = model_profiles()
    exact_matches = [profile for profile in profiles if profile.id == profile_id]
    if not profile_id.startswith("api:"):
        return exact_matches[0] if len(exact_matches) == 1 else None

    legacy_model = profile_id.split(":", 1)[1]
    legacy_matches = [
        profile
        for profile in profiles
        if profile.provider == "api" and profile.upstream_model == legacy_model
    ]
    matches = {profile.id: profile for profile in (*exact_matches, *legacy_matches)}
    return next(iter(matches.values())) if len(matches) == 1 else None


def _params(generation: Generation) -> dict:
    return json.loads(generation.parameters_json or "{}")


def _references(generation: Generation) -> list[Path]:
    paths = []
    for reference in generation.references:
        path = resolve_storage_key(reference.storage_key)
        if not path.is_file():
            raise RuntimeError(f"参考图不存在：{reference.original_name}")
        paths.append(path)
    return paths


def _checkpoint(generation: Generation) -> None:
    session = object_session(generation)
    if session:
        session.commit()


def _legacy_native_execution_config(provider: str) -> dict:
    """Resolve mutable deployment configuration only for pre-freeze legacy tasks."""
    return _current_native_execution_config(provider)


def _native_execution_config(profile: ModelProfile) -> dict:
    if profile.execution_config_id:
        return _load_native_execution_config(profile.execution_config_id, profile.provider)
    return _legacy_native_execution_config(profile.provider)


def _sanitize_provider_output(text: str, *secret_values: str) -> str:
    sanitized = text
    for value in secret_values:
        if value:
            sanitized = sanitized.replace(value, "[已隐藏]")
    sanitized = re.sub(r"https?://\S+", "[已隐藏地址]", sanitized)
    sanitized = re.sub(
        r"(?i)(bearer|api[_ -]?key|access[_ -]?key|secret[_ -]?key|token)"
        r"\s*[:=]?\s*\S+",
        r"\1 [已隐藏]",
        sanitized,
    )
    return sanitized[-2000:]


def _libtv_env(config: dict) -> dict[str, str]:
    token = str(config.get("token", ""))
    overrides = {"LIBTV_ACCESS_KEY": token, "LIBTV_TOKEN": token}
    im_base = str(config.get("im_base", "") or "")
    if im_base:
        overrides["OPENAPI_IM_BASE"] = im_base
    return _skill_child_env(overrides)


def _libtv_scripts(config: dict) -> Path:
    directory = Path(str(config.get("scripts_dir", "")))
    if not directory.is_dir():
        raise RuntimeError("LibTV skill 脚本目录不存在")
    return directory


def _run_libtv(*args: str, execution_config: dict | None = None) -> dict:
    """Run one LibTV skill script: the first arg names the script, the rest are its flags."""
    config = execution_config or _legacy_native_execution_config("libtv")
    if not args:
        raise RuntimeError("LibTV 调用缺少脚本名")
    directory = _libtv_scripts(config)
    script = directory / (
        args[0] if args[0].endswith(".py") else f"{args[0]}.py"
    )
    token = str(config.get("token", ""))
    stdout = _run_skill_script(
        script, list(args[1:]), env_overrides=_libtv_env(config),
        timeout=settings.provider_command_timeout_seconds,
        secret_values=(token,), script_dir=script,
    )
    return _parse_skill_json(stdout, "LibTV 返回结果无法解析")


def _libtv_download_results(
    config: dict, *, session_id: str, output_dir: Path, prefix: str = "result"
) -> list[Path]:
    """Batch-download every image/video the LibTV session produced."""
    directory = _libtv_scripts(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout = _run_skill_script(
        directory / "download_results.py",
        [session_id, "--output-dir", str(output_dir), "--prefix", prefix],
        env_overrides=_libtv_env(config),
        timeout=settings.provider_command_timeout_seconds,
        secret_values=(str(config.get("token", "")),), script_dir=directory,
    )
    payload = _parse_skill_json(stdout, "LibTV 下载失败")
    return [Path(item) for item in payload.get("downloaded", []) if item]


def _libtv_message_urls(messages: list) -> list[str]:
    """Extract result URLs the session already exposed (图片/视频)."""
    pattern = re.compile(
        r"https://libtv-res\.liblib\.art/[^\s\"'<>]+\.(?:png|jpg|jpeg|webp|mp4|mov|webm)"
    )
    urls: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        if message.get("role") == "tool":
            try:
                data = json.loads(content) if isinstance(content, str) else {}
            except json.JSONDecodeError:
                data = {}
            task_result = data.get("task_result", {}) if isinstance(data, dict) else {}
            for item in task_result.get("images", []) or []:
                preview = item.get("previewPath", "")
                if preview:
                    urls.append(preview)
            for item in task_result.get("videos", []) or []:
                preview = item.get("previewPath") or item.get("url") or ""
                if preview:
                    urls.append(preview)
        if message.get("role") == "assistant" and isinstance(content, str):
            urls.extend(pattern.findall(content))
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _execute_libtv(generation: Generation, profile: ModelProfile) -> None:
    """Send the original prompt to LibTV, then poll the session and collect the result."""
    config = _native_execution_config(profile)
    prompt = generation.original_prompt
    references = _references(generation)
    if references:
        # 参考图必须先落到可公开访问的地址，skill 才能把它作为附件传给上游。
        uploaded: list[str] = []
        for path in references:
            payload = _run_libtv("upload_file", str(path), execution_config=config)
            url = str(payload.get("url") or "")
            if not url:
                raise RuntimeError("LibTV 参考图上传失败")
            uploaded.append(url)
        prompt = f"{prompt}\n参考图：{' '.join(uploaded)}"
    created = _run_libtv("create_session", prompt, execution_config=config)
    session_id = str(created.get("sessionId") or "")
    if not session_id:
        raise RuntimeError("LibTV 未返回会话 ID")
    generation.external_project_id = str(created.get("projectUuid") or "")
    generation.external_task_id = session_id
    _checkpoint(generation)
    output_dir = settings.storage_root / f"generations/{generation.id}/results"
    output_dir.mkdir(parents=True, exist_ok=True)
    _poll_libtv_session(generation, config, session_id, output_dir, str(profile.label))


def _poll_libtv_session(
    generation: Generation, config: dict, session_id: str, output_dir: Path, label: str
) -> None:
    """Poll the session until an assistant result appears, one query per worker pass.

    A single pass issues one query and returns; the worker's outer recovery loop
    keeps the task alive while the upstream agent finishes. That avoids holding a
    worker slot for the entire multi-minute generation.
    """
    deadline = utcnow().timestamp() + settings.lovart_chat_timeout_seconds
    empty_passes = 0
    while utcnow().timestamp() < deadline:
        payload = _run_libtv(
            "query_session", session_id, "--after-seq", "0", execution_config=config
        )
        messages = payload.get("messages", [])
        urls = _libtv_message_urls(messages)
        if urls:
            files = _libtv_download_results(
                config, session_id=session_id, output_dir=output_dir, prefix="result"
            )
            if not files:
                files = _fetch_urls(urls, output_dir, "result")
            files = _validated_images(files)
            if files:
                generation.artifact_storage_key = str(
                    max(files, key=lambda path: path.stat().st_mtime).relative_to(
                        settings.storage_root
                    )
                )
                return
        empty_passes += 1
        raise RuntimeError(
            f"{label} 上游仍在生成中（已轮询 {empty_passes} 次），暂未产生结果。"
        )
    raise RuntimeError(f"{label} 等待上游结果超时")


def _validated_images(paths: list[Path]) -> list[Path]:
    images = []
    for path in paths:
        try:
            validate_artifact(path)
        except InvalidImage:
            continue
        images.append(path)
    return images


def _fetch_urls(urls: list[str], output_dir: Path, prefix: str) -> list[Path]:
    """Direct download fallback when the skill's batch downloader finds nothing."""
    output_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for index, url in enumerate(urls, 1):
        suffix = Path(urlsplit(url).path).suffix or ".png"
        target = output_dir / f"{prefix}_{index:02d}{suffix}"
        try:
            with httpx.stream("GET", url, timeout=60, follow_redirects=True) as response:
                response.raise_for_status()
                with open(target, "wb") as handle:
                    handle.writelines(response.iter_bytes())
        except (httpx.HTTPError, OSError):
            target.unlink(missing_ok=True)
            continue
        files.append(target)
    return files


def _run_lovart(*args: str, execution_config: dict | None = None) -> dict:
    config = execution_config or _legacy_native_execution_config("lovart")
    access_key = str(config["access_key"])
    secret_key = str(config["secret_key"])
    script = Path(str(config["skill_script"]))
    stdout = _run_skill_script(
        script, list(args),
        env_overrides={
            "LOVART_ACCESS_KEY": access_key,
            "LOVART_SECRET_KEY": secret_key,
            "LOVART_BASE_URL": str(config["base_url"]),
        },
        timeout=int(config.get("chat_timeout") or settings.provider_command_timeout_seconds),
        secret_values=(access_key, secret_key), script_dir=script,
    )
    return _parse_skill_json(stdout, "Lovart 返回结果无法解析")


def _lovart_downloaded_images(result: dict, output_dir: Path | None = None) -> list[Path]:
    """Collect on-disk image paths a Lovart skill command actually produced.

    The skill records every artifact it saw, including the ones whose download
    failed, as ``local_path: None``. Those entries must not be treated as a
    usable result, but they also must not be mistaken for "the upstream
    produced nothing" — the next caller may still fetch the artifact.

    The skill can also report a path that it never actually wrote (it names the
    file from a URL hash and skips the download when a *stale* file of that name
    already exists). So every candidate is confirmed to be a non-empty file
    before it is accepted.

    ``output_dir`` guards against the other half of that trap: the skill runs
    with ``cwd`` set to its own script directory, so a relative path it echoes
    back must be reinterpreted against the directory we told it to use.
    """
    images: list[Path] = []
    for item in result.get("downloaded") or []:
        if not isinstance(item, dict) or item.get("type") != "image":
            continue
        raw = item.get("local_path")
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw.strip())
        if output_dir is not None and path.is_absolute() is False:
            candidate = output_dir / path.name
            if candidate.is_file():
                path = candidate
        if path.is_file() and path.stat().st_size > 0:
            images.append(path)
    return images


def _store_lovart_result(
    source: Path, output_dir: Path, generation: Generation
) -> None:
    """Copy one downloaded artifact into the generation's own result directory."""
    target = output_dir / f"result{source.suffix or '.png'}"
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    validate_artifact(target)
    generation.artifact_storage_key = str(target.relative_to(settings.storage_root))


def _lovart_no_image_reason(result: dict) -> str:
    """Explain a Lovart run that produced no image, preferring the upstream text."""
    detail = str(
        result.get("agent_message") or result.get("warning") or ""
    ).strip()
    failures = result.get("failures") or []
    if not detail and failures:
        first = failures[0]
        detail = str(first.get("message") or first) if isinstance(first, dict) else str(first)
    status = str(result.get("final_status") or "").strip()
    if detail:
        return f"Lovart 未返回图片：{detail[:400]}"
    if status == "done":
        # 「跑完了但没有产物」通常是上游拒绝或模型没调用出图工具，而不是网络问题。
        return "Lovart 任务已完成但未产出图片（上游可能拒绝该提示词或未调用出图工具）"
    return f"Lovart 未返回图片（上游状态：{status or '未知'}）"


def _lovart_prompt(generation: Generation, profile: ModelProfile) -> str:
    """Attach the chosen ratio/resolution to the prompt, leaving the text intact.

    The Lovart ``chat`` subcommand has no ``--size``/``--ratio`` flag, so the
    only way the picker's value can reach the upstream agent is inside the
    prompt itself. Skipping this is exactly why every image used to come back at
    the wrong aspect ratio: the user's choice was stored on the Generation and
    then dropped on the floor.

    The original prompt is never rewritten — the size is appended as its own
    line so the agent reads it as a constraint rather than as subject matter.
    The snapshot is frozen with the task, so this reflects what the user actually
    submitted even if the model catalog changes later.
    """
    prompt = generation.original_prompt
    params = _params(generation)
    ratio = str(params.get("ratio") or "").strip()
    resolution = str(params.get("resolution") or "").strip()
    # `auto` 就是「不指定」，再附一句反而会干扰上游自己的判断。
    if not ratio or ratio == "auto":
        return prompt
    hint = _lovart_size_hint(profile.upstream_model, ratio, resolution)
    if not hint:
        return prompt
    return f"{prompt}\n\n画面比例：{hint}"


def _execute_lovart(generation: Generation, profile: ModelProfile) -> None:
    """Send the original prompt to the Lovart agent and collect the first image back."""
    config = _native_execution_config(profile)
    created = _run_lovart("create-project", execution_config=config)
    project_id = str(created.get("project_id") or "")
    if not project_id:
        raise RuntimeError("Lovart 未返回 Project ID")
    generation.external_project_id = project_id
    _checkpoint(generation)
    attachments = []
    for path in _references(generation):
        uploaded = _run_lovart("upload", "--file", str(path), execution_config=config)
        if not uploaded.get("url"):
            raise RuntimeError("Lovart 参考图上传失败")
        attachments.append(uploaded["url"])
    output_dir = settings.storage_root / f"generations/{generation.id}/results"
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "chat", "--project-id", project_id,
        "--prompt", _lovart_prompt(generation, profile),
        "--prefer-models", json.dumps({"IMAGE": [profile.upstream_model]}, separators=(",", ":")),
        "--mode", str(config.get("mode") or "fast"),
        "--json", "--download", "--output-dir", str(output_dir),
    ]
    if attachments:
        command.extend(["--attachments", *attachments])
    result = _run_lovart(*command, execution_config=config)
    thread_id = str(result.get("thread_id") or "")
    generation.external_task_id = thread_id
    _checkpoint(generation)
    if result.get("final_status") == "pending_confirmation":
        raise RuntimeError("Lovart 要求确认高成本操作，请由管理员检查上游任务")
    sources = _lovart_downloaded_images(result, output_dir)
    # `chat --download` 的内联下载会失败（skill 把这个 artifact 记为
    # local_path=None 且不报错）。此时线程其实已经产出图片，用 result 命令
    # 再取一次即可 —— 这一步是幂等的，不会重复生成，也不会再次计费。
    if not sources and thread_id:
        retry = _run_lovart(
            "result", "--thread-id", thread_id, "--json", "--download",
            "--output-dir", str(output_dir), execution_config=config,
        )
        sources = _lovart_downloaded_images(retry, output_dir)
        if not sources:
            result = retry  # 让下面的报错展示刷新后的原因
    if not sources:
        raise RuntimeError(_lovart_no_image_reason(result))
    _store_lovart_result(sources[0], output_dir, generation)


def probe_libtv_connection() -> tuple[bool, str]:
    """Create one empty session to prove the token and scripts actually work."""
    try:
        config = _current_native_execution_config("libtv")
    except ProviderConfigError as exc:
        return False, str(exc)
    try:
        payload = _run_libtv("create_session", execution_config=config)
    except RuntimeError as exc:
        return False, str(exc)
    session_id = str(payload.get("sessionId") or "")
    if not session_id:
        return False, "LibTV 未返回会话 ID"
    project = str(payload.get("projectUuid") or "")
    return True, f"LibTV 连接正常（会话 {session_id[:8]}…，项目 {project[:8] or '未知'}）"


def probe_lovart_connection() -> tuple[bool, str]:
    """Read the current generation mode: proves AK/SK signing and script wiring."""
    try:
        config = _current_native_execution_config("lovart")
    except ProviderConfigError as exc:
        return False, str(exc)
    try:
        _run_lovart("query-mode", execution_config=config)
    except RuntimeError as exc:
        return False, str(exc)
    return True, "Lovart 连接正常（AK/SK 签名与 skill 脚本均可用）"


def _data_url(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/webp" if path.suffix.lower() == ".webp" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _execute_api(generation: Generation, profile: ModelProfile) -> None:
    params = _params(generation)
    resolution = params.get("resolution")
    ratio = params.get("ratio")
    quality = params.get("quality")
    if resolution not in profile.resolutions or resolution not in API_RESOLUTIONS:
        raise RuntimeError("API 任务包含执行器不支持的分辨率")
    if ratio not in profile.ratios or ratio not in API_RATIOS:
        raise RuntimeError("API 任务包含执行器不支持的比例")
    if quality not in profile.qualities or quality not in API_QUALITIES:
        raise RuntimeError("API 任务包含执行器不支持的质量参数")
    if len(generation.references) > profile.max_references:
        raise RuntimeError("API 任务参考图数量超过冻结能力上限")
    edge = {"1K": 1024, "2K": 2048, "4K": 4096}[resolution]
    ratio_dimensions = {
        "1:1": (edge, edge), "4:3": (edge, edge * 3 // 4),
        "3:4": (edge * 3 // 4, edge), "16:9": (edge, edge * 9 // 16),
        "9:16": (edge * 9 // 16, edge),
    }
    width, height = ratio_dimensions[ratio]
    body = {
        "model": profile.upstream_model,
        "prompt": generation.original_prompt,
        "size": f"{width}x{height}",
        "n": 1,
        "response_format": "b64_json",
        "watermark": False,
    }
    if quality != "standard":
        body["quality"] = quality
    references = _references(generation)
    if references:
        body["image"] = [_data_url(path) for path in references]
    api_config = _load_api_execution_config(profile.execution_config_id)
    with httpx.Client(timeout=300) as client:
        response = client.post(
            f"{str(api_config['base_url']).rstrip('/')}/images/generations",
            headers={"Authorization": f"Bearer {api_config['api_key']}"}, json=body,
        )
        response.raise_for_status()
        payload = response.json()
    generation.external_task_id = str(payload.get("id") or "")
    image = (payload.get("data") or [{}])[0]
    output_dir = settings.storage_root / f"generations/{generation.id}/results"
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "result.png"
    if image.get("b64_json"):
        payload = base64.b64decode(image["b64_json"])
        if len(payload) > settings.max_artifact_bytes:
            raise RuntimeError("图像 API 返回文件超过大小限制")
        target.write_bytes(payload)
    elif image.get("url"):
        with httpx.Client(timeout=120) as client:
            download = client.get(image["url"])
            download.raise_for_status()
            if len(download.content) > settings.max_artifact_bytes:
                raise RuntimeError("图像 API 返回文件超过大小限制")
            target.write_bytes(download.content)
    else:
        raise RuntimeError("图像 API 未返回图片")
    validate_artifact(target)
    generation.artifact_storage_key = str(target.relative_to(settings.storage_root))


def _validate_frozen_capabilities(generation: Generation, profile: ModelProfile) -> None:
    params = _params(generation)
    if params.get("ratio") not in profile.ratios:
        raise RuntimeError("任务比例不在冻结能力范围内")
    if params.get("resolution") not in profile.resolutions:
        raise RuntimeError("任务分辨率不在冻结能力范围内")
    if params.get("quality") not in profile.qualities:
        raise RuntimeError("任务质量不在冻结能力范围内")
    if len(generation.references) > profile.max_references:
        raise RuntimeError("任务参考图数量超过冻结能力上限")


def _generation_frozen_profile(generation: Generation) -> ModelProfile | None:
    try:
        payload = json.loads(generation.provider_snapshot_json or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return ModelProfile(**payload)
    except TypeError:
        if payload.get("execution_config_id"):
            return None
        return get_profile(f"{generation.provider}:{generation.model_id}")


def execute_generation(generation_id: str) -> None:
    from image_hub.db import SessionLocal

    with SessionLocal() as session:
        generation = session.get(Generation, generation_id)
        if not generation:
            return
        profile = _generation_frozen_profile(generation)
        generation.status = "running"
        session.commit()
        try:
            if not profile or not profile.enabled:
                raise RuntimeError("生成平台或模型当前不可用")
            _validate_frozen_capabilities(generation, profile)
            executor = {"libtv": _execute_libtv, "lovart": _execute_lovart, "api": _execute_api}.get(
                profile.provider
            )
            if executor is None:
                raise RuntimeError("任务执行器未知")
            executor(generation, profile)
            generation.status = "succeeded"
        except Exception as exc:  # noqa: BLE001
            generation.status = (
                "recovery_required"
                if generation.external_project_id or generation.external_task_id
                else "failed"
            )
            generation.error_message = (
                "API 图像生成失败，请联系管理员检查服务端配置"
                if profile and profile.provider == "api"
                else str(exc)[-2000:]
            )
        finally:
            generation.finished_at = utcnow()
            generation.lease_owner = ""
            generation.lease_expires_at = None
            session.commit()


def recover_generation(generation_id: str) -> tuple[bool, str]:
    from image_hub.db import SessionLocal

    with SessionLocal() as session:
        generation = session.get(Generation, generation_id)
        if not generation or generation.status != "recovery_required":
            raise RuntimeError("任务不处于待恢复状态")
        output_dir = settings.storage_root / f"generations/{generation.id}/results"
        output_dir.mkdir(parents=True, exist_ok=True)
        profile = _generation_frozen_profile(generation)
        if not profile:
            raise RuntimeError("任务缺少有效的平台快照")
        if generation.provider == "libtv":
            if not generation.external_task_id:
                raise RuntimeError("LibTV 任务缺少外部会话 ID")
            config = _native_execution_config(profile)
            output_dir = settings.storage_root / f"generations/{generation.id}/results"
            output_dir.mkdir(parents=True, exist_ok=True)
            payload = _run_libtv(
                "query_session", generation.external_task_id, "--after-seq", "0",
                execution_config=config,
            )
            urls = _libtv_message_urls(payload.get("messages", []))
            files: list[Path] = []
            if urls:
                files = _libtv_download_results(
                    config, session_id=generation.external_task_id, output_dir=output_dir,
                    prefix="result",
                )
                if not files:
                    files = _fetch_urls(urls, output_dir, "result")
                files = _validated_images(files)
            if not files:
                generation.error_message = "LibTV 上游暂未返回可下载图片。"
                session.commit()
                return False, generation.error_message
            generation.artifact_storage_key = str(
                max(files, key=lambda path: path.stat().st_mtime).relative_to(
                    settings.storage_root
                )
            )
        elif generation.provider == "lovart":
            if not generation.external_task_id:
                raise RuntimeError("Lovart 任务缺少外部 Thread ID")
            config = _native_execution_config(profile)
            result = _run_lovart(
                "result", "--thread-id", generation.external_task_id, "--json", "--download",
                "--output-dir", str(output_dir), execution_config=config,
            )
            sources = _lovart_downloaded_images(result, output_dir)
            if not sources:
                generation.error_message = _lovart_no_image_reason(result)
                session.commit()
                return False, generation.error_message
            _store_lovart_result(sources[0], output_dir, generation)
        else:
            raise RuntimeError("该 API 为同步接口，没有可查询的外部恢复任务")
        generation.status = "succeeded"
        generation.error_message = ""
        generation.finished_at = utcnow()
        generation.lease_owner = ""
        generation.lease_expires_at = None
        session.commit()
        return True, "已从上游恢复生成结果"

