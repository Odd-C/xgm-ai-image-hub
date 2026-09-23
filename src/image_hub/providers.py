import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session, object_session

from image_hub import agent, api_routing, libtv_mcp, prompt_optimize, provider_pool
from image_hub.config import settings
from image_hub.models import Generation, utcnow
from image_hub.storage import InvalidImage, resolve_storage_key, validate_artifact

logger = logging.getLogger("image_hub.providers")

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
    #: API 通道里这个模型当前路由到的 API 名（空 = 默认路由）。它进 ``snapshot_dict``
    api_route: str = ""
    #: 给用户看的**一句话介绍**（管理页逐模型填，见模型串第 10 字段）。
    note: str = ""
    #: 这笔任务**可以**发往上游的模型名候选，按优先级排列（见模型串第 11 字段）。
    upstreams: tuple[str, ...] = ()
    #: **这一笔实际发出去**的上游模型名（候选里第一个路由还走得通的那个），执行前由
    upstream_exec: str = ""

    def public_dict(self) -> dict:
        """Return browser-safe selection metadata without upstream routing details."""
        payload = asdict(self)
        payload.pop("upstream_model", None)
        # 上游模型名候选同样不外传：它和 ``upstream_model`` 是一回事（都是上游的模型名）
        payload.pop("upstreams", None)
        payload.pop("upstream_exec", None)
        payload.pop("execution_config_id", None)
        payload.pop("api_route", None)
        # 某些模型（1:3 / 3:1）不是每个比例都有全部档位
        if self.provider == "lovart":
            payload["tiers"] = {
                ratio: list(_lovart_tiers_for(self.upstream_model, ratio))
                for ratio in self.ratios
            }
        elif (builtin := builtin_api_model(self.upstream_model)) is not None and builtin.pixels:
            # API 通道里需要换算像素的模型，「比例 × 档位」并不总是矩形
            payload["tiers"] = {
                ratio: [
                    tier for tier in API_RESOLUTION_ORDER
                    if tier in (builtin.pixels.get(ratio) or {})
                ]
                for ratio in self.ratios
            }
        else:
            payload["tiers"] = {}
        return payload

    def snapshot_dict(self) -> dict:
        """Keep immutable server-side execution routing with the generation evidence."""
        return asdict(self)


# LibTV 的模型目录是**活的**：`search_models`（列目录）+ `get_model`（逐模型能力）都能查
_LIBTV_AGENT_KEY = "agent"
_LIBTV_AGENT_LABEL = "LibTV 智能编排"
LIBTV_FALLBACK_MODELS = (
    ("z-image", "Z Image Turbo"),
    ("seedream-4", "Seedream 4.0"),
    ("seedream-4.5", "Seedream 4.5"),
    ("seedream-5", "Seedream 5.0 Lite"),
    ("doubao-seedream-5-0-pro", "Seedream 5.0 Pro"),
    ("jimeng-4.6", "Seedream 4.6"),
    ("lib-image-2", "Lib Image"),
    ("lib-image-2.5-f", "Lib Image 2.5 Fast"),
    ("lib-image-2.5-s", "Lib Image 2.5 Pro"),
    ("nebula-core", "General image"),
    ("nebula-ultra", "General image Pro"),
    ("nebula-2-flash", "General image V2"),
    ("mj-v8.2", "Midjourney V8.2"),
    ("mj-v7", "Midjourney V7"),
    ("mj-niji7", "Midjourney Niji 7"),
    ("qwen-image-3", "Qwen image 3.0"),
    ("qwen", "QwenImage"),
    ("flux-2-pro", "基础算法F.2 Pro"),
    ("flux-2-flex", "基础算法F.2 Flex"),
    ("flux-1", "基础算法F.1"),
    ("orbit-2-image", "Shock Image 2"),
)
#: 上游查不到能力面时的兜底（平台通用档位）。
_LIBTV_DEFAULT_RATIOS = ("1:1", "4:3", "3:4", "16:9", "9:16")
_LIBTV_DEFAULT_RESOLUTIONS = ("1K", "2K", "4K")


def _libtv_ordered(values: list[str], preferred: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """把上游给的默认档位排到最前 —— 前端取 `[0]` 当默认值。"""
    items = [str(value) for value in values if value]
    ordered = list(items or fallback)
    if preferred and preferred in ordered:
        ordered.remove(preferred)
    elif preferred:
        ordered.insert(0, preferred)
    else:
        return tuple(ordered)
    ordered.insert(0, preferred)
    return tuple(dict.fromkeys(ordered))


def _libtv_model_rows(account_id: str = "") -> tuple[tuple[str, str, list[dict]], bool]:
    """``account_id`` 只影响「首次没缓存时用哪个号的令牌去拉」；目录本身全局缓存一份。"""
    models, stale = libtv_mcp.model_catalog(account_id=account_id)
    if not models:
        return [(key, label, []) for key, label in LIBTV_FALLBACK_MODELS], True
    rows = [
        (
            str(entry.get("key") or ""),
            str(entry.get("name") or entry.get("key") or ""),
            [entry],
        )
        for entry in models
        if entry.get("key")
    ]
    # 目录过期不代表不可用：先用缓存渲染，下一次刷新由管理页的「刷新模型目录」触发。
    return rows, stale

# Lovart 暴露的是具体出图模型，所以这里逐个枚举。
LOVART_MODELS = (
    ("gpt-image-2-5-flare", "GPT Image 2.5 Flare Auto", "generate_image_gpt_image_2_5_flare"),
    (
        "gpt-image-2-5-sunburst", "GPT Image 2.5 Sunburst Auto",
        "generate_image_gpt_image_2_5_sunburst",
    ),
    # VIP 不在本仓库随附的那份 SKILL.md 表里（它按 2026-09-15 的快照生成）
    ("gpt-image-2-vip", "GPT Image 2 VIP", "generate_image_gpt_image_2_vip"),
    ("gpt-image-2", "GPT Image 2 Auto", "generate_image_gpt_image_2"),
    # 只暴露 nano-banana 2 系（含 lite）与 pro，以及 gpt-image-2 / 2.
    ("nano-banana-pro", "Nano Banana Pro", "generate_image_nano_banana_pro"),
    ("nano-banana-2", "Nano Banana 2", "generate_image_nano_banana_2"),
    ("nano-banana-2-lite", "Nano Banana 2 Lite", "generate_image_nano_banana_2_lite"),
)

# 从 LOVART_MODELS 派生，避免「模型清单改了、尺寸分派忘了改」这种漂移。
_LOVART_UPSTREAM_TOOLS = frozenset(tool for _, _, tool in LOVART_MODELS)
_LOVART_GPT_IMAGE_2_5_TOOLS = frozenset(
    tool for tool in _LOVART_UPSTREAM_TOOLS if tool.startswith("generate_image_gpt_image_2_5")
)
# 走 1K/2K/4K 三档比例表的模型。
_LOVART_THREE_TIER_TOOLS = _LOVART_GPT_IMAGE_2_5_TOOLS | {
    "generate_image_gpt_image_2_vip",
}
_LOVART_NANO_BANANA_TOOLS = frozenset(
    tool for tool in _LOVART_UPSTREAM_TOOLS if tool.startswith("generate_image_nano_banana")
)
_SINGLE_TIER_TOOL = "generate_image_gpt_image_2"

# Lovart 的尺寸能力面。
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

# 单档模型（gpt-image-2）没有档位概念
LOVART_SINGLE_TIER = ("自动",)

# gpt-image-2.5 系支持的面板比通用组宽得多（15 种比例），所以单独声明。
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
LOVART_RATIOS = ("1:1", "4:3", "3:4", "16:9", "9:16")


def _lovart_capabilities(upstream_model: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the ``(ratios, resolutions)`` a model actually accepts."""
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
    """Narrow the resolution chips to the tiers that ratio actually supports."""
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
    """Describe the requested canvas in the only channel the skill exposes: text."""
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


class ProviderAccountsExhaustedError(RuntimeError):
    """平台账号池里的号都用不上了，任务**从未被上游受理**。"""


class UpstreamPendingError(RuntimeError):
    """上游**还在生成**，这次没取到结果 —— 它不是失败。"""


#: ``aspectRatio`` 的比例全集；各模型从这里取子集（见 GRSAI_BUILTIN_MODELS）。
API_RATIOS = frozenset({
    "auto",
    "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "21:9", "9:21",
    "1:2", "2:1", "1:3", "3:1",
    "1:4", "4:1", "1:8", "8:1",
})
API_RESOLUTIONS = frozenset({"1K", "2K", "4K"})
#: 上游支持的质量取值。
API_QUALITIES = frozenset({"auto", "medium", "high", "xhigh", "max"})
#: 档位的展示/收窄顺序。
API_RESOLUTION_ORDER = ("1K", "2K", "4K")
#: OpenAI 兼容上游（火山方舟 / 自建中转）没有档位概念，只收像素 ``size``
API_SIZE_EDGES = {"1K": 1024, "2K": 2048, "4K": 4096}

# ---------------------------------------------------------------------------

#: 需要按「比例 + 档位」换算成像素值提交的模型所依赖的表。
GRSAI_PIXELS_FULL: dict[str, dict[str, str]] = {
    "1:1": {"1K": "1024x1024", "2K": "2048x2048", "4K": "2880x2880"},
    "16:9": {"1K": "1280x720", "2K": "2048x1152", "4K": "3840x2160"},
    "9:16": {"1K": "720x1280", "2K": "1152x2048", "4K": "2160x3840"},
    "4:3": {"1K": "1152x864", "2K": "2304x1728", "4K": "3264x2448"},
    "3:4": {"1K": "864x1152", "2K": "1728x2304", "4K": "2448x3264"},
    "3:2": {"1K": "1536x1024", "2K": "2048x1360", "4K": "3504x2336"},
    "2:3": {"1K": "1024x1536", "2K": "1360x2048", "4K": "2336x3504"},
    "5:4": {"1K": "1120x896", "2K": "2240x1792", "4K": "3200x2560"},
    "4:5": {"1K": "896x1120", "2K": "1792x2240", "4K": "2560x3200"},
    "21:9": {"1K": "1456x624", "2K": "2912x1248", "4K": "3840x1648"},
    "9:21": {"1K": "624x1456", "2K": "1248x2912", "4K": "1648x3840"},
    "2:1": {"1K": "1536x768", "2K": "3072x1536", "4K": "3840x1920"},
    "1:2": {"1K": "768x1536", "2K": "1536x3072", "4K": "1920x3840"},
    "1:3": {"2K": "688x2048", "4K": "1280x3840"},
    "3:1": {"2K": "2048x688", "4K": "3840x1280"},
}

#: ``gpt-image-2.5`` 只出 1K。
GRSAI_PIXELS_1K: dict[str, dict[str, str]] = {
    "1:1": {"1K": "1024x1024"},
    "16:9": {"1K": "1672x941"},
    "9:16": {"1K": "941x1672"},
    "4:3": {"1K": "1443x1090"},
    "3:4": {"1K": "1090x1443"},
    "3:2": {"1K": "1536x1024"},
    "2:3": {"1K": "1024x1536"},
    "5:4": {"1K": "1408x1120"},
    "4:5": {"1K": "1120x1408"},
    "21:9": {"1K": "1920x832"},
    "9:21": {"1K": "832x1920"},
    "1:2": {"1K": "896x1792"},
    "2:1": {"1K": "1792x896"},
}

#: nano-banana 系列的公共比例。
_NANO_RATIOS = ("auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "21:9")
#: ``nano-banana-2`` 系列额外支持的比例。
_NANO2_EXTRA_RATIOS = ("1:4", "4:1", "1:8", "8:1")


@dataclass
class BuiltinApiModel:
    """内置模型能力。``aspect_mode`` 决定 ``aspectRatio`` 该填什么："""

    model_id: str
    label: str
    aspect_mode: str
    ratios: tuple[str, ...]
    resolutions: tuple[str, ...]
    qualities: tuple[str, ...] = ()
    #: 参考图上限。只出 1K 的模型给 6 张，能出 2K/4K 的给 10 张。
    max_references: int = 10
    #: ``None`` 表示该模型直接用比例，不需要换算像素。
    pixels: dict[str, dict[str, str]] | None = None


#: 开箱即用的上游模型。管理后台显式配置模型时以配置为准。
GRSAI_BUILTIN_MODELS: tuple[BuiltinApiModel, ...] = (
    BuiltinApiModel(
        model_id="nano-banana-2-lite", label="Nano Banana 2 Lite", aspect_mode="ratio_tier",
        ratios=_NANO_RATIOS + _NANO2_EXTRA_RATIOS, resolutions=("1K",), max_references=6,
    ),
    BuiltinApiModel(
        model_id="nano-banana-2", label="Nano Banana 2", aspect_mode="ratio_tier",
        ratios=_NANO_RATIOS + _NANO2_EXTRA_RATIOS, resolutions=("1K", "2K", "4K"),
        max_references=10,
    ),
    BuiltinApiModel(
        model_id="nano-banana-pro", label="Nano Banana Pro", aspect_mode="ratio_tier",
        ratios=_NANO_RATIOS, resolutions=("1K", "2K", "4K"), max_references=10,
    ),
    BuiltinApiModel(
        model_id="gpt-image-2.5", label="GPT Image 2.5", aspect_mode="ratio_or_pixel",
        ratios=tuple(GRSAI_PIXELS_1K), resolutions=("1K",), qualities=("auto",),
        max_references=6, pixels=GRSAI_PIXELS_1K,
    ),
    BuiltinApiModel(
        model_id="gpt-image-2.5-flare", label="GPT Image 2.5 Flare", aspect_mode="pixel_only",
        ratios=tuple(GRSAI_PIXELS_FULL), resolutions=("1K", "2K", "4K"),
        qualities=("medium", "high"), max_references=10, pixels=GRSAI_PIXELS_FULL,
    ),
    BuiltinApiModel(
        model_id="gpt-image-2.5-sunburst", label="GPT Image 2.5 Sunburst",
        aspect_mode="pixel_only",
        ratios=tuple(GRSAI_PIXELS_FULL), resolutions=("1K", "2K", "4K"),
        qualities=("medium", "high", "xhigh", "max"), max_references=10,
        pixels=GRSAI_PIXELS_FULL,
    ),
)


def builtin_api_model(model_id: str) -> BuiltinApiModel | None:
    """按上游模型 ID 取内置能力，未知模型返回 ``None``。"""
    for item in GRSAI_BUILTIN_MODELS:
        if item.model_id == model_id:
            return item
    return None
_API_ENABLED_VALUES = frozenset({"1", "true", "on", "启用"})
_API_DISABLED_VALUES = frozenset({"0", "false", "off", "停用"})
_NATIVE_CREDENTIALS_DIR = "native-provider-credentials"
#: 未走账号池时的默认凭据槽。
_DEFAULT_ACCOUNT_SLOT = "default"


def _valid_account_id(account_id: str) -> bool:
    """平台账号 id 直接拼进私有文件路径，必须先校验（``uuid4().hex`` 形态）。"""
    return bool(re.fullmatch(r"[0-9a-f]{8,64}", str(account_id or "").strip()))


def _credentials_path(account_id: str = "") -> Path:
    """私有凭据文件位置：``<账号>.json``，空账号走 ``default`` 槽。"""
    base = settings.storage_root / "private" / _NATIVE_CREDENTIALS_DIR
    if not account_id:
        return base / f"{_DEFAULT_ACCOUNT_SLOT}.json"
    if not _valid_account_id(account_id):
        raise ProviderConfigError("平台账号标识无效")
    return base / f"{account_id}.json"


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
        "routes": "",
    }


def _api_routes(api_config: dict) -> tuple[dict, ...]:
    """配置里的路由清单（**只读**，读取路径上不做网络请求）。"""
    raw = api_config.get("routes")
    if not raw:
        return ()
    if isinstance(raw, str):
        try:
            return _parse_api_routes(raw)
        except ProviderConfigError:
            return ()
    if isinstance(raw, list):
        return tuple(
            item for item in raw if isinstance(item, dict) and str(item.get("name") or "")
        )
    return ()


def api_routes_text(api_config: dict | None = None) -> str:
    """路由清单 → 管理页文本框的原文。"""
    routes = _api_routes(api_config if api_config is not None else _api_config())
    lines = []
    for item in routes:
        async_value = ""
        if item.get("async_declared"):
            async_value = "true" if item.get("async") else "false"
        lines.append(
            "|".join(
                (
                    str(item.get("name") or ""),
                    str(item.get("base_url") or ""),
                    "",
                    str(item.get("family") or ""),
                    async_value,
                    "true" if item.get("enabled", True) else "false",
                )
            )
        )
    return "\n".join(lines)


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
        # POSIX needs an explicit fsync on the parent directory before the rename.
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


def _native_credentials(account_id: str = "") -> dict:
    path = _credentials_path(account_id)
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


def _save_native_credentials(payload: dict, account_id: str = "") -> None:
    _atomic_write_private_json(_credentials_path(account_id), payload)


def save_lovart_credentials(
    access_key: str,
    secret_key: str,
    skill_script: str = "",
    *,
    clear: bool = False,
    account_id: str = "",
) -> str:
    """``account_id`` 为空 = 默认槽；非空 = 账号池里的一格 —— 多账号就是每个号各存一对 AK/SK。"""
    payload = _native_credentials(account_id)
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
    _save_native_credentials(payload, account_id)
    return action


# ── LibTV / Lovart 官方 skill 脚本的唯一入口 ─────────────────────────────

_SKILL_SCRIPT_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+\.py$")


def _python_executable() -> str:
    """Use the interpreter that runs this service so the scripts need no PATH entry."""
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


#: skill 脚本的本地状态根目录（按平台 / 账号分层）。
_SKILL_STATE_DIR = "skill-state"


def _skill_state_home(provider: str, account_id: str = "") -> Path:
    """给 skill 脚本一份**按 (平台, 账号) 隔离**的 ``HOME``。"""
    base = settings.storage_root / "private" / _SKILL_STATE_DIR / provider
    if not account_id:
        return base / _DEFAULT_ACCOUNT_SLOT
    if not _valid_account_id(account_id):
        raise ProviderConfigError("平台账号标识无效")
    return base / account_id


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
    """Pick the **last** JSON object printed by a skill script."""
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
    found: dict | None = None
    for index, character in enumerate(stripped):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            found = payload
    if found is None:
        raise RuntimeError(error_message)
    return found



def _runtime_secret(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return _validated_secret(value, "平台凭据", allow_empty=True)
    except ProviderConfigError:
        return ""


def _resolved_lovart_skill_script(account_id: str = "") -> tuple[Path, str]:
    """路径只在服务端解析，**绝不回传前端**。"""
    managed = _native_credentials(account_id).get("lovart_skill_script", "")
    if isinstance(managed, str) and managed.strip():
        return Path(managed.strip()), "管理后台已配置"
    return settings.lovart_skill_script, (
        "环境已配置" if settings.lovart_skill_script.is_file() else "未配置"
    )


def _resolved_lovart_credentials(account_id: str = "") -> tuple[str, str, str]:
    """每个平台账号各存一对 AK/SK，读不到就是「未配置」。"""
    payload = _native_credentials(account_id)
    access = _runtime_secret(payload.get("lovart_access_key", ""))
    secret = _runtime_secret(payload.get("lovart_secret_key", ""))
    if access and secret:
        return access, secret, "管理后台已配置"
    return "", "", "未配置"


def public_native_credentials(account_id: str = "") -> dict:
    """管理页展示用的安全视图：只有状态与来源，不含路径与密钥。"""
    lovart_access, lovart_secret, lovart_source = _resolved_lovart_credentials(account_id)
    lovart_script, _ = _resolved_lovart_skill_script(account_id)
    return {
        "libtv": libtv_mcp.authorization_status(account_id),
        "lovart": {
            "connected": bool(lovart_access and lovart_secret and lovart_script.is_file()),
            "credentials_ready": bool(lovart_access and lovart_secret),
            "script_ready": lovart_script.is_file(),
            "source": lovart_source,
        },
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


def _assert_address_is_public(address: str, hostname: str, label: str) -> None:
    """单个解析结果必须落在公网，除非 allowlist 明确授权。"""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise ProviderConfigError(f"{label} 域名解析结果无效") from exc
    allowed_hosts, allowed_networks = _parse_allowlist()
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
        raise ProviderConfigError(f"{label} 不得指向本机、链路本地或保留地址")
    if ip.is_private:
        if not allowlisted_private:
            raise ProviderConfigError(f"{label} 指向内网；需由服务端 allowlist 明确授权")
    elif not ip.is_global:
        raise ProviderConfigError(f"{label} 不得指向非公网地址")


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
    for address in addresses:
        _assert_address_is_public(address, hostname, "API Base URL")
    return tuple(sorted(addresses))


#: 请求头名称允许的字符（RFC 7230 tchar 的子集）。
_HEADER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`~]+$")
#: 单个模型允许的附加请求头条数上限。请求头会随每次生成请求发出，没有理由放开。
_MAX_MODEL_HEADERS = 20
#: 单条请求头值的长度上限。
_MAX_HEADER_VALUE_LENGTH = 1000


def _parse_api_headers(raw: str) -> dict[str, str]:
    """解析某个模型**自己的**附加请求头。"""
    headers: dict[str, str] = {}
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        name, separator, value = item.partition(":")
        name = name.strip()
        if not separator or not name:
            raise ProviderConfigError("API 模型请求头格式应为 名称:值，多条用分号分隔")
        if not _HEADER_NAME_PATTERN.fullmatch(name):
            raise ProviderConfigError(f"API 模型请求头名称不合法：{name}")
        value = value.strip()
        if len(value) > _MAX_HEADER_VALUE_LENGTH:
            raise ProviderConfigError("API 模型请求头的值过长")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ProviderConfigError("API 模型请求头的值不得包含控制字符")
        headers[name] = value
    if len(headers) > _MAX_MODEL_HEADERS:
        raise ProviderConfigError(f"单个 API 模型最多配置 {_MAX_MODEL_HEADERS} 条请求头")
    return headers


#: 路由清单里允许的「请求形状」。
API_ROUTE_FAMILIES = ("grsai_native", "openai_compat")
#: 路由清单的条数上限。路由数是「个位数」，这个上限只为拦住失控的配置。
_MAX_API_ROUTES = 20
#: 路由名允许的字符。它同时是模型串第 9 字段里的一个元素，所以 ``|`` ``,`` ``;``
_API_ROUTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
#: 模型串第 10 字段（模型介绍）的长度上限。
_MAX_MODEL_NOTE_LENGTH = 160


def api_model_family(model_id: str) -> str:
    """模型 ID → **默认**请求形状。"""
    return "grsai_native" if builtin_api_model(model_id) is not None else "openai_compat"


def _parse_api_routes(raw: str) -> tuple[dict, ...]:
    """解析路由清单。每行一条，字段用 ``|`` 分隔："""
    if not raw.strip():
        return ()
    routes: list[dict] = []
    seen: set[str] = set()
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) > 6:
            raise ProviderConfigError("API 路由配置格式无效（应为 名称|地址|密钥|族|异步|启用）")
        if any("\r" in field or "\n" in field or "\0" in field for field in fields):
            raise ProviderConfigError("API 路由配置不得包含换行或 NUL 字符")
        name = fields[0]
        if not _API_ROUTE_NAME_PATTERN.fullmatch(name):
            raise ProviderConfigError(
                "API 路由名只能是 1-40 位的字母、数字、下划线、点和连字符"
            )
        if name in seen:
            raise ProviderConfigError("API 路由名不得重复")
        seen.add(name)
        base_url = fields[1] if len(fields) > 1 else ""
        if not base_url:
            raise ProviderConfigError(f"API 路由 {name} 缺少地址")
        resolved_ips = _resolve_and_validate_endpoint(base_url)
        api_key = fields[2] if len(fields) > 2 else ""
        if any(character in api_key for character in ("\r", "\n", "\0")):
            raise ProviderConfigError("API 路由密钥不得包含换行或 NUL 字符")
        if len(api_key) > 1000:
            raise ProviderConfigError("API 路由密钥长度超过限制")
        family = fields[3].lower() if len(fields) > 3 else ""
        if family and family not in API_ROUTE_FAMILIES:
            raise ProviderConfigError(
                "API 路由的族只能是 " + " / ".join(API_ROUTE_FAMILIES)
            )
        async_value = fields[4].lower() if len(fields) > 4 else ""
        if async_value and async_value not in _API_ENABLED_VALUES | _API_DISABLED_VALUES:
            raise ProviderConfigError("API 路由的异步取值必须是 true 或 false")
        if async_value in _API_ENABLED_VALUES and family == "openai_compat":
            # OpenAI 兼容端点没有可轮询的接口
            raise ProviderConfigError("OpenAI 兼容端点不支持异步任务")
        enabled_value = fields[5].lower() if len(fields) > 5 else "true"
        if enabled_value not in _API_ENABLED_VALUES | _API_DISABLED_VALUES:
            raise ProviderConfigError("API 路由的启用状态必须是 true 或 false")
        routes.append(
            {
                "name": name,
                "base_url": base_url.rstrip("/"),
                "api_key": api_key,
                "family": family,
                "async": async_value in _API_ENABLED_VALUES,
                "async_declared": bool(async_value),
                "enabled": enabled_value in _API_ENABLED_VALUES,
                "resolved_ips": list(resolved_ips),
            }
        )
    if len(routes) > _MAX_API_ROUTES:
        raise ProviderConfigError(f"API 路由最多配置 {_MAX_API_ROUTES} 条")
    return tuple(routes)


def _parse_api_models(models: str) -> tuple[dict, ...]:
    """解析模型配置串。"""
    if not models.strip():
        return ()
    parsed_models = []
    seen = set()
    raw_models = models.split(",")
    if any(not item.strip() for item in raw_models):
        raise ProviderConfigError("API 模型配置包含空行或多余逗号")
    for raw_item in raw_models:
        fields = [field.strip() for field in raw_item.split("|")]
        # 第 1 个字段是这个模型在 **平台侧的身份**（用户看到的模型就是它）
        if len(fields) > 11 or not fields[0] or any("\n" in field or "\r" in field for field in fields):
            raise ProviderConfigError("API 模型配置格式无效")
        model_id = fields[0]
        if any(character.isspace() for character in model_id):
            raise ProviderConfigError("API 模型 ID 不得包含空白字符")
        if model_id in seen:
            raise ProviderConfigError("API 模型 ID 不得重复")
        seen.add(model_id)
        # 第 11 个字段：这个模型在**各个上游**分别叫什么（分号分隔，按优先级）。
        upstreams = (
            tuple(dict.fromkeys(filter(None, fields[10].split(";"))))
            if len(fields) > 10 and fields[10].strip()
            else (model_id,)
        )
        if any(any(character.isspace() for character in item) for item in upstreams):
            raise ProviderConfigError("上游模型 ID 不得包含空白字符")
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
        # 允许为空：nano-banana 系列本身就没有质量维度
        if any(value not in API_QUALITIES for value in qualities):
            raise ProviderConfigError("API 模型包含执行器不支持的质量参数")
        headers = _parse_api_headers(fields[7]) if len(fields) > 7 else {}
        # 第 9 个字段：本模型要走的**那一条 API 路由名**（人工指定，见 ``api_routing``）。
        route_name = fields[8] if len(fields) > 8 else ""
        if ";" in route_name:
            # 这里曾接受 ``;`` 分隔的候选名单并依次尝试，现已收敛为单选。
            raise ProviderConfigError("每个模型只能指定一条 API 路由")
        if route_name and not _API_ROUTE_NAME_PATTERN.fullmatch(route_name):
            raise ProviderConfigError(
                "API 模型的路由名只能是 1-40 位的字母、数字、下划线、点和连字符"
            )
        # 第 10 个字段：给用户看的模型介绍（可留空）。
        note = fields[9] if len(fields) > 9 else ""
        if len(note) > _MAX_MODEL_NOTE_LENGTH:
            raise ProviderConfigError(
                f"API 模型介绍不能超过 {_MAX_MODEL_NOTE_LENGTH} 个字符"
            )
        if any(character in note for character in ",|;"):
            raise ProviderConfigError("API 模型介绍不能包含逗号、竖线或分号")
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
                "headers": headers,
                # 本模型要走的 API 路由（空 = 走默认路由）。由 ``api_routing`` 消费。
                "route": route_name,
                # 给用户看的模型介绍（空 = 选择弹层里只显示「可用」）。
                "note": note,
                # 发往上游的模型名候选（第 11 字段；空 = 只用模型标识本身）。
                "upstreams": upstreams,
            }
        )
    if not parsed_models:
        raise ProviderConfigError("至少配置一个 API 模型")
    return tuple(parsed_models)


def _model_request_headers(api_config: dict, model_id: str) -> dict[str, str]:
    """取某一个模型自己的附加请求头；没有配置就返回空字典。"""
    try:
        configured = _parse_api_models(str(api_config.get("models", "") or ""))
    except ProviderConfigError:
        # 冻结配置在入库时已经校验过，走到这里说明配置被外部改坏了。
        logger.warning("冻结配置里的模型串无法解析，已退回默认请求头")
        return {}
    for item in configured:
        if item["model_id"] == model_id:
            return dict(item.get("headers") or {})
    return {}


def _api_request_headers(api_config: dict, model_id: str) -> dict[str, str]:
    """默认的 Bearer 鉴权，叠加该模型自己的请求头。"""
    headers = {"Authorization": f"Bearer {api_config['api_key']}"}
    for name, value in _model_request_headers(api_config, model_id).items():
        if value:
            headers[name] = value
        else:
            headers.pop(name, None)
    return headers


def _validated_api_config(
    base_url: str,
    api_key: str,
    models: str,
    routes: str = "",
    *,
    existing: dict | None = None,
) -> dict:
    """校验并整理要落盘的 API 配置（含路由清单）。"""
    current = existing or {}
    normalized_url = base_url.strip().rstrip("/")
    resolved_ips = _resolve_and_validate_endpoint(normalized_url)
    parsed_models = _parse_api_models(models.strip())
    known_keys = {
        str(item.get("name") or ""): str(item.get("api_key") or "")
        for item in _api_routes(current)
    }
    stored_routes = []
    for item in _parse_api_routes(routes):
        entry = dict(item)
        entry["api_key"] = (
            entry["api_key"] or known_keys.get(entry["name"], "") or api_key.strip()
        )
        stored_routes.append(entry)
    route_names = {item["name"] for item in stored_routes}
    for model in parsed_models:
        route_name = str(model.get("route") or "")
        if route_name and route_name not in route_names:
            # 指定一条不存在的路由是**静默故障**：模型在列表里看着正常
            raise ProviderConfigError(
                f"API 模型 {model['model_id']} 指定了未定义的路由：{route_name}"
            )
    return {
        "base_url": normalized_url,
        "api_key": api_key.strip() or str(current.get("api_key", "")),
        "models": models.strip(),
        "resolved_ips": list(resolved_ips),
        "routes": stored_routes,
    }


def save_api_config(
    base_url: str, api_key: str, models: str, routes: str | None = None
) -> None:
    """Validate and atomically persist server-only credentials with mode 0600."""
    current = _api_config()
    payload = _validated_api_config(
        base_url,
        api_key,
        models,
        api_routes_text(current) if routes is None else routes,
        existing=current,
    )
    _atomic_write_private_json(settings.storage_root / "provider-config.json", payload)


def api_model_rows() -> tuple[str, ...]:
    """当前配置里的 API 模型行（原始文本，按清单顺序）。"""
    return tuple(
        row.strip()
        for row in str(_api_config().get("models") or "").split(",")
        if row.strip()
    )


def reorder_api_models(order: str) -> int:
    """按位置序列重排已保存的 API 模型串，返回行数。"""
    path = settings.storage_root / "provider-config.json"
    if not path.is_file():
        raise ProviderConfigError("API 配置尚未在管理页保存过，无法调整模型顺序")
    rows = api_model_rows()
    if not rows:
        raise ProviderConfigError("当前没有可排序的 API 模型行")
    indices: list[int] = []
    for chunk in order.split(","):
        chunk = chunk.strip()
        if not chunk.isdigit():
            raise ProviderConfigError("模型顺序格式无效")
        indices.append(int(chunk))
    if sorted(indices) != list(range(len(rows))):
        raise ProviderConfigError("模型顺序与当前配置不一致，请刷新页面后重试")
    payload = dict(_api_config())
    payload["models"] = ",".join(rows[index] for index in indices)
    _atomic_write_private_json(path, payload)
    return len(rows)


def public_api_config() -> dict:
    """Expose configuration state without returning any routing or credential material."""
    config = _api_config()
    return {
        "has_base_url": bool(config.get("base_url")),
        "has_api_key": bool(config.get("api_key")),
        "has_models": bool(config.get("models")),
        "has_routes": bool(_api_routes(config)),
    }


def _public_api_model_id(model_id: str) -> str:
    """模型真实 ID → 可以给浏览器看的公开 ID。上游模型名不应对外暴露。"""
    return hashlib.sha256(model_id.encode()).hexdigest()[:16]


def api_model_id_for_public(public_id: str) -> str:
    """公开安全 ID → 真实模型 ID（管理页表单回传用）；认不出来返回空串。"""
    try:
        configured = _parse_api_models(str(_api_config().get("models", "") or ""))
    except ProviderConfigError:
        return ""
    for item in configured:
        if f"api:{_public_api_model_id(item['model_id'])}" == public_id:
            return item["model_id"]
    return ""


def public_api_model_headers() -> dict[str, str]:
    """「公开安全 ID → 该模型自己的附加请求头串」，供管理后台回显。"""
    config = _api_config()
    try:
        configured = _parse_api_models(str(config.get("models", "") or ""))
    except ProviderConfigError:
        return {}
    return {
        f"api:{_public_api_model_id(item['model_id'])}": ";".join(
            f"{name}:{value}" for name, value in (item.get("headers") or {}).items()
        )
        for item in configured
        if item.get("headers")
    }


def public_api_routes() -> list[dict]:
    """路由清单的**脱敏**视图（管理页用）。密钥一律不外传，与 ``public_api_config``"""
    return [
        {
            "name": str(item.get("name") or ""),
            "base_url": str(item.get("base_url") or ""),
            "family": str(item.get("family") or ""),
            "async": bool(item.get("async")),
            "async_declared": bool(item.get("async_declared")),
            "enabled": bool(item.get("enabled", True)),
            "has_api_key": bool(item.get("api_key")),
        }
        for item in _api_routes(_api_config())
    ]


def public_api_model_routes() -> dict[str, str]:
    """「公开安全 ID → 该模型选定的 API 路由名」，供管理后台回显。"""
    config = _api_config()
    return {
        f"api:{_public_api_model_id(model_id)}": route
        for model_id, route in _api_models_with_route(config)
    }


def public_api_model_notes() -> dict[str, str]:
    """「公开安全 ID → 该模型填写的介绍」，供管理后台回显。"""
    config = _api_config()
    try:
        configured = _parse_api_models(str(config.get("models", "") or ""))
    except ProviderConfigError:
        return {}
    return {
        f"api:{_public_api_model_id(item['model_id'])}": str(item.get("note") or "")
        for item in configured
        if item.get("note")
    }


def public_api_route_states(session: Session) -> dict[str, list[dict]]:
    """「公开安全 ID → 该模型选定路由的失效/错误摘要」，供管理页渲染路由状态表。"""
    config = _api_config()
    upstreams_of = {
        item["model_id"]: item["upstreams"]
        for item in _parse_api_models(str(config.get("models") or ""))
    }
    states: dict[str, list[dict]] = {}
    for model_id, route in _api_models_with_route(config):
        # 一个模型现在可能对应好几个上游名字
        states[f"api:{_public_api_model_id(model_id)}"] = [
            state
            for name in api_upstream_names(upstreams_of.get(model_id), model_id)
            for state in api_routing.route_states(session, name, (route,))
        ]
    return states


def _execution_config_id(config: dict) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(settings.session_secret.encode(), encoded, hashlib.sha256).hexdigest()[:32]


def _default_route_payload(api_config: dict) -> dict:
    """默认路由的冻结配置：存储配置**去掉路由清单**。"""
    return {key: value for key, value in api_config.items() if key != "routes"}


def _api_route_config(current: dict, model_id: str, route_name: str) -> dict:
    """选中的一条路由 → 一份**独立的冻结配置**。"""
    if not route_name:
        if current.get("resolved_ips"):
            # 后台保存的配置：**原样**回吐。
            return _default_route_payload(current)
        # 环境变量配置没有 resolved_ips，这里补一次解析后落盘
        return _default_route_payload(
            _validated_api_config(
                str(current.get("base_url", "")),
                str(current.get("api_key", "")),
                str(current.get("models", "")),
            )
        )
    route = next(
        (item for item in _api_routes(current) if item["name"] == route_name), None
    )
    if route is None:
        raise ProviderConfigError(f"API 模型 {model_id} 选中的路由不存在：{route_name}")
    api_key = str(route.get("api_key") or current.get("api_key") or "")
    if not (route.get("base_url") and api_key):
        raise ProviderConfigError(f"API 路由 {route_name} 缺少地址或密钥")
    return {
        "base_url": str(route["base_url"]),
        "api_key": api_key,
        "models": str(current.get("models", "")),
        "resolved_ips": list(route.get("resolved_ips") or ()),
        # 路由身份与请求形状。执行器只认这三个字段，所以「同一个模型换到另一条 API」
        "route": route_name,
        "family": str(route.get("family") or ""),
        "async": bool(route.get("async")),
        "async_declared": bool(route.get("async_declared")),
    }


def _freeze_api_execution_config(model_id: str = "", route_name: str = "") -> str:
    current = _api_config()
    config = _api_route_config(current, model_id, route_name)
    identity = _execution_config_id(config)
    path = settings.storage_root / "provider-configs" / f"{identity}.json"
    if not path.exists():
        _atomic_write_private_json(path, config)
    return identity


#: 环境变量配置的冻结标识缓存，键是配置内容指纹。
_ENV_API_CONFIG_IDS: dict[str, str] = {}


def _api_execution_config_id(
    api_config: dict, model_id: str = "", route_name: str = ""
) -> str:
    """取得这份 API 配置的冻结标识。"""
    if route_name:
        try:
            return _execution_config_id(
                _api_route_config(api_config, model_id, route_name)
            )
        except ProviderConfigError as exc:
            logger.warning("API 路由 %s 不可用：%s", route_name, exc)
            return ""
    if api_config.get("resolved_ips"):
        # 只能拿「去掉路由清单」的那份去算：``_freeze_api_execution_config`` 冻结
        return _execution_config_id(_default_route_payload(api_config))
    if not (api_config.get("base_url") and api_config.get("api_key")):
        return ""
    # 指纹要带上存储目录：冻结文件是写在 storage_root 下的，目录一换
    fingerprint = json.dumps(
        {
            key: api_config.get(key, "") for key in ("base_url", "api_key", "models")
        }
        | {"storage_root": str(settings.storage_root)},
        sort_keys=True,
    )
    cached = _ENV_API_CONFIG_IDS.get(fingerprint)
    if cached is not None:
        return cached
    try:
        identity = _freeze_api_execution_config()
    except ProviderConfigError as exc:
        # 域名不可解析、或指到了内网 —— 交给调用方按"未配置"处理
        logger.warning("环境变量里的 API 配置未通过校验，API 通道将不可用：%s", exc)
        return ""
    _ENV_API_CONFIG_IDS[fingerprint] = identity
    return identity


def _current_native_execution_config(provider: str, account_id: str = "") -> dict:
    """按平台与账号槽取当次执行的冻结配置。"""
    if provider == "libtv":
        # LibTV 走 Remote MCP（OAuth）：冻结的是「哪个账号 + 哪个网关」
        if not libtv_mcp.authorized(account_id):
            raise ProviderConfigError("LibTV 尚未授权连接（管理页完成 OAuth 授权后可用）")
        return {
            "provider": "libtv",
            "transport": "mcp",
            "provider_account_id": account_id,
            "mcp_base": settings.libtv_mcp_base,
        }
    if provider == "lovart":
        access_key, secret_key, source = _resolved_lovart_credentials(account_id)
        if not access_key or not secret_key:
            raise ProviderConfigError("Lovart 凭据未成对配置")
        skill_script, _ = _resolved_lovart_skill_script(account_id)
        if not skill_script.is_file():
            raise ProviderConfigError("Lovart skill 脚本未配置或文件不存在")
        return {
            "provider": "lovart",
            "provider_account_id": account_id,
            "access_key": access_key,
            "secret_key": secret_key,
            "source": source,
            "base_url": settings.lovart_base_url,
            "skill_script": str(skill_script),
            "mode": settings.lovart_thread_mode,
            "chat_timeout": settings.lovart_chat_timeout_seconds,
        }
    raise ProviderConfigError("不支持冻结该平台配置")


def account_credentials_ready(provider: str, account_id: str) -> bool:
    """该平台账号此刻的凭据是否齐备（只读本地文件，不发网络请求）。"""
    if provider == "libtv":
        return libtv_mcp.authorized(account_id)
    if provider == "lovart":
        access_key, secret_key, _ = _resolved_lovart_credentials(account_id)
        return bool(access_key and secret_key)
    return False


def select_provider_account(session: Session, *, provider: str) -> str:
    """⚠️ ``provider`` 是关键字专用：它是 ``str``，写错不会报错，只会安静地返回「池里没号」"""
    if not provider_pool.is_pool_provider(provider):
        return ""
    if not provider_pool.pool_accounts(session, provider):
        return ""
    chosen = provider_pool.select_account(
        session, provider, ready=lambda account: account_credentials_ready(provider, account.id)
    )
    if chosen is None:
        chosen = provider_pool.select_account(session, provider)
    return chosen.id if chosen else ""


def routed_account_ids(session: Session) -> dict[str, str]:
    """每个平台的当前选号快照；漏算一个渠道会让它的模型静默消失。"""
    return {
        provider: select_provider_account(session, provider=provider)
        for provider in provider_pool.POOL_PROVIDERS
    }


def _enabled_api_route_names(api_config: dict) -> tuple[str, ...]:
    """配置里**启用**的路由名。"""
    return tuple(
        str(item.get("name") or "")
        for item in _api_routes(api_config)
        if item.get("enabled", True) and item.get("name")
    )


def routed_api_routes(session: Session) -> dict[str, str]:
    """一次算出**每个选定了路由的模型**此刻指向哪条 API。"""
    config = _api_config()
    enabled = _enabled_api_route_names(config)
    upstreams_of = {
        item["model_id"]: item["upstreams"]
        for item in _parse_api_models(str(config.get("models") or ""))
    }
    routed: dict[str, str] = {}
    for model_id, route in _api_models_with_route(config):
        # 候选里**只要有一个**名字这条路还走得通
        routed[model_id] = (
            route
            if any(
                api_routing.route_available(session, name, route, enabled=enabled)
                for name in api_upstream_names(upstreams_of.get(model_id), model_id)
            )
            else ""
        )
    return routed


def api_upstream_names(upstreams, fallback: str) -> tuple[str, ...]:
    """归一化「这个模型可以发往哪些上游名字」：候选为空就退回它自己的名字。"""
    names = tuple(name for name in (upstreams or ()) if name)
    return names or ((fallback,) if fallback else ())


def pick_api_upstream(session: Session, upstreams, route: str) -> str:
    """按优先级挑**这一笔实际要发**的上游模型名；一个都走不通就返回空串。"""
    names = api_upstream_names(upstreams, "")
    if not names:
        return ""
    if not route:
        return names[0]
    enabled = _enabled_api_route_names(_api_config())
    for name in names:
        if api_routing.route_available(session, name, route, enabled=enabled):
            return name
    return ""


def api_upstream_candidates(model_id: str) -> tuple[str, ...]:
    """这个模型可以发往哪些上游名字（管理页手工清失效标记时按它们逐个清）。"""
    try:
        models = _parse_api_models(str(_api_config().get("models") or ""))
    except ProviderConfigError:
        return (model_id,)
    for item in models:
        if item["model_id"] == model_id:
            return api_upstream_names(item["upstreams"], model_id)
    return (model_id,)


def snapshot_upstream_names(snapshot: object) -> tuple[str, ...]:
    """任务快照里这个模型当时**可能**用到的全部上游模型名。"""
    if not isinstance(snapshot, dict):
        return ()
    names = [
        str(snapshot.get("upstream_model") or ""),
        # 真正发出去的那一个：它一定也是上游名。
        str(snapshot.get("upstream_exec") or ""),
    ]
    candidates = snapshot.get("upstreams")
    if isinstance(candidates, (list, tuple)):
        names.extend(str(item) for item in candidates)
    return tuple(dict.fromkeys(name for name in names if name))


def _api_models_with_route(api_config: dict) -> tuple[tuple[str, str], ...]:
    """配置里选定了路由的模型 → ``(模型 id, 路由名)``。"""
    try:
        models = _parse_api_models(str(api_config.get("models", "") or ""))
    except ProviderConfigError:
        return ()
    return tuple(
        (item["model_id"], str(item.get("route") or ""))
        for item in models
        if item.get("route")
    )


def _freeze_native_execution_config(provider: str, account_id: str = "") -> str:
    config = _current_native_execution_config(provider, account_id)
    identity = _execution_config_id(config)
    path = settings.storage_root / "provider-configs" / f"{identity}.json"
    if path.exists():
        _load_native_execution_config(identity, provider)
    else:
        _atomic_write_private_json(path, config)
    return identity


def freeze_profile_execution(
    profile: ModelProfile, *, account_id: str = ""
) -> ModelProfile:
    """Attach an immutable server-only route identity before a task is queued."""
    if profile.provider not in {"api", "libtv", "lovart"}:
        return profile
    try:
        if profile.provider == "api":
            # 路由名来自档案（列表页按粘性游标选出的那条）。
            identity = _freeze_api_execution_config(
                profile.upstream_model, profile.api_route
            )
        else:
            identity = _freeze_native_execution_config(profile.provider, account_id)
    except ProviderConfigError:
        # No usable route is configured right now.
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
        # LibTV 冻结的是「账号 + 网关」
        ("mcp_base",)
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


def model_profiles(
    accounts: Mapping[str, str] | None = None,
    api_routes: Mapping[str, str] | None = None,
) -> tuple[ModelProfile, ...]:
    """按账号与选路生成模型清单。"""
    routed = accounts or {}
    libtv_ready = libtv_mcp.authorized(routed.get("libtv", ""))
    lovart_access_key, lovart_secret_key, _ = _resolved_lovart_credentials(
        routed.get("lovart", "")
    )
    lovart_script, _ = _resolved_lovart_skill_script(routed.get("lovart", ""))
    lovart_ready = bool(lovart_access_key and lovart_secret_key and lovart_script.is_file())
    profiles: list[ModelProfile] = []
    if libtv_ready:
        # 只暴露**上游活目录里的具体模型**（generation_submit 直连通道）。
        rows, _stale = _libtv_model_rows(routed.get("libtv", ""))
        for key, label, entries in rows:
            entry = entries[0] if entries else {}
            defaults = entry.get("defaults") or {}
            profiles.append(ModelProfile(
                id=f"libtv:{key}", label=label, provider="libtv", upstream_model=key,
                enabled=True,
                ratios=_libtv_ordered(
                    list(entry.get("ratios") or []), str(defaults.get("ratio") or ""),
                    _LIBTV_DEFAULT_RATIOS,
                ),
                resolutions=_libtv_ordered(
                    list(entry.get("resolutions") or []), str(defaults.get("resolution") or ""),
                    _LIBTV_DEFAULT_RESOLUTIONS,
                ),
                qualities=_libtv_ordered(
                    list(entry.get("qualities") or []), str(defaults.get("quality") or ""),
                    (),
                ),
            ))
    profiles.extend(
        ModelProfile(
            id=f"lovart:{key}", label=label, provider="lovart",
            upstream_model=upstream_model, enabled=lovart_ready,
            # 能力面按模型分派：gpt-image-2.
            ratios=_lovart_capabilities(upstream_model)[0],
            resolutions=_lovart_capabilities(upstream_model)[1],
        )
        for key, label, upstream_model in LOVART_MODELS
    )
    api_config = _api_config()
    try:
        configured_models = _parse_api_models(str(api_config.get("models", "")))
    except ProviderConfigError:
        configured_models = ()
    # 管理后台没有显式配置模型时，直接给出内置的六个：开箱可
    api_models = configured_models or tuple(
        {
            "model_id": item.model_id,
            "label": item.label,
            "ratios": item.ratios,
            "resolutions": item.resolutions,
            "qualities": item.qualities,
            "max_references": item.max_references,
            "enabled": True,
            # 内置模型没有介绍：选择弹层里只显示「可用」，不会凭空多出文案。
            "note": "",
            # 上游名候选：内置模型的平台侧身份与上游模型名本来就是同一个值
            "upstreams": (item.model_id,),
        }
        for item in GRSAI_BUILTIN_MODELS
    )
    routed_routes = api_routes or {}
    for item in api_models:
        public_id = _public_api_model_id(item["model_id"])
        model_id = item["model_id"]
        if model_id in routed_routes:
            # 选了路由就**只走**那条，绝不回退默认路由（见 routed_api_routes 的三态约定）。
            route_name = routed_routes[model_id]
            route_ready = bool(route_name)
        else:
            route_name = ""
            route_ready = bool(api_config.get("api_key") and api_config.get("base_url"))
        # 选了路由但它此刻不可用（被停用 / 已被标失效）时，**不**把这个模型摆出来。
        profiles.append(
            ModelProfile(
                id=f"api:{public_id}",
                label=item["label"],
                provider="api",
                upstream_model=model_id,
                enabled=bool(item["enabled"] and route_ready),
                ratios=item["ratios"],
                resolutions=item["resolutions"],
                qualities=item["qualities"],
                max_references=item["max_references"],
                upstreams=item["upstreams"],
                execution_config_id=_api_execution_config_id(
                    api_config, model_id, route_name
                ),
                api_route=route_name,
                note=str(item.get("note") or ""),
            )
        )
    return tuple(profiles)


def routed_model_profiles(session: Session) -> tuple[ModelProfile, ...]:
    """当前模型清单，带上账号池选号与 API 选路。"""
    return model_profiles(
        accounts=routed_account_ids(session),
        api_routes=routed_api_routes(session),
    )


def get_profile(
    profile_id: str,
    *,
    accounts: Mapping[str, str] | None = None,
    api_routes: Mapping[str, str] | None = None,
) -> ModelProfile | None:
    """Resolve opaque and legacy IDs, failing closed if their meanings collide."""
    profiles = model_profiles(accounts=accounts, api_routes=api_routes)
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


def _effective_prompt(generation: Generation) -> str:
    """真正发给上游的提示词。"""
    return generation.optimized_prompt or generation.original_prompt


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


def _native_execution_config(profile: ModelProfile) -> dict:
    if profile.execution_config_id:
        return _load_native_execution_config(profile.execution_config_id, profile.provider)
    # 无冻结配置的历史任务：按当前实时配置跑（一次性兼容路径）。
    return _current_native_execution_config(profile.provider)


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


# ── LibTV（Remote MCP 通道）───────────────────────────────────────────────

_LIBTV_PROJECT_NAME = "图像工作台"


def _provider_account_id(config: dict) -> str:
    """从冻结配置取平台账号 id；空串表示默认槽。"""
    return str(config.get("provider_account_id") or "")


def _libtv_ensure_project(account_id: str = "") -> str:
    """取（或建）该平台账号的专属项目；幂等键按账号派生，并发创建由上游去重。"""
    payload = _native_credentials(account_id)
    project = str(payload.get("libtv_project_id") or "")
    if project:
        return project
    key = f"image-hub-project-{account_id or _DEFAULT_ACCOUNT_SLOT}"
    result = libtv_mcp.call_tool("create_project", {
        "name": _LIBTV_PROJECT_NAME,
        "idempotencyKey": key,
    }, account_id=account_id)
    project = str(result.get("projectId") or (result.get("project") or {}).get("uuid") or "")
    if not project:
        raise RuntimeError("LibTV 项目创建失败")
    payload["libtv_project_id"] = project
    _save_native_credentials(payload, account_id)
    return project


def _libtv_retired_agent(profile: ModelProfile) -> bool:
    """这个快照是不是已下线的「智能编排」任务。"""
    return str(getattr(profile, "upstream_model", "")) in {_LIBTV_AGENT_KEY, _LIBTV_AGENT_LABEL}


def _libtv_image_request(generation: Generation, profile: ModelProfile) -> dict:
    """把我们的参数收成上游 `request`。"""
    params = _params(generation)
    payload: dict = {"prompt": _effective_prompt(generation)}
    ratio = str(params.get("ratio") or "").strip()
    if ratio and ratio in profile.ratios:
        payload["ratio"] = ratio
    resolution = str(params.get("resolution") or "").strip()
    if resolution and resolution in profile.resolutions:
        payload["resolution"] = resolution
    quality = str(params.get("quality") or "").strip()
    # 与 API 渠道同一条约定：模型没有画质维度（qualities 为空）时**不发这个字段**
    if quality and profile.qualities and quality in profile.qualities:
        payload["quality"] = quality
    return payload


def _libtv_artifact_media_urls(
    project_id: str, artifact: dict, account_id: str = ""
) -> list[str]:
    """从 task_status 的 artifact 取媒体地址。"""
    urls: list[str] = []
    for key in ("url", "previewUrl", "downloadUrl"):
        value = artifact.get(key)
        if isinstance(value, str) and value.startswith("http"):
            urls.append(value)
    node_key = str(artifact.get("node_key") or artifact.get("nodeKey") or "")
    if not urls and node_key:
        info = libtv_mcp.call_tool("inspect_media", {
            "node": node_key, "projectId": project_id,
        }, timeout=60, account_id=account_id)
        media = info.get("media") or {}
        urls.extend(str(url) for url in media.get("urls") or [] if str(url).startswith("http"))
    seen: set[str] = set()
    return [url for url in urls if not (url in seen or seen.add(url))]


def _execute_libtv(generation: Generation, profile: ModelProfile) -> None:
    """按选定模型提交生成（generation_submit → task_status）。"""
    config = _native_execution_config(profile)
    account_id = _provider_account_id(config)
    if _libtv_retired_agent(profile):
        raise RuntimeError(
            "LibTV 智能编排通道已下线，请改用具体模型重新提交任务。"
        )
    project_id = _libtv_ensure_project(account_id)
    generation.external_project_id = project_id
    output_dir = settings.storage_root / f"generations/{generation.id}/results"
    output_dir.mkdir(parents=True, exist_ok=True)

    request = _libtv_image_request(generation, profile)
    submitted = libtv_mcp.call_tool("generation_submit", {
        "project_id": project_id,
        "idempotency_key": f"image-hub-gen-{generation.id}",
        "kind": "image",
        "request": {
            "model": profile.upstream_model,
            "params": request,
            "task_title": f"image-hub {generation.id}"[:64],
        },
    }, timeout=90, account_id=account_id)
    task_id = str(submitted.get("task_id") or "")
    if not task_id:
        raise RuntimeError("LibTV 未返回 task_id")
    generation.external_task_id = task_id
    # 账号留痕：这笔任务**实际**走的是哪个号（运维与成本归因用，**不是**计费输入）。
    generation.provider_account_id = account_id
    _checkpoint(generation)
    _poll_libtv_task(
        generation, project_id, task_id, output_dir, str(profile.label), account_id
    )


def _poll_libtv_task(
    generation: Generation, project_id: str, task_id: str,
    output_dir: Path, label: str, account_id: str = "",
) -> None:
    """查询一次 durable task：出图就落盘，没出图就抛错交给自动复查。"""
    try:
        status = libtv_mcp.call_tool("task_status", {
            "project_id": project_id, "task_id": task_id,
        }, timeout=60, account_id=account_id)
    except RuntimeError as exc:
        message = str(exc)
        if "TASK_CONTEXT_UNAVAILABLE" in message:
            # 上下文过期：上游可能已经出图，但我们再也查不到任务了。
            raise RuntimeError(
                f"{label} 的任务上下文已过期，无法继续查询；图可能已在上游项目中生成，"
                "请在 LibTV 项目里确认后人工处理。"
            ) from exc
        raise
    outcome = str(status.get("outcome") or "")
    readiness = str(status.get("readiness") or "")
    if outcome in ("FAILED", "CANCELLED") or readiness in ("TERMINAL_FAILED", "TERMINAL_CANCELLED"):
        detail = status.get("error") or outcome or readiness
        raise RuntimeError(f"{label} 上游任务失败：{str(detail)[:300]}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for artifact in status.get("artifacts") or []:
        if not isinstance(artifact, dict) or artifact.get("kind") not in {"image", "video"}:
            continue
        urls = _libtv_artifact_media_urls(project_id, artifact, account_id)
        if not urls:
            continue
        files = _validated_images(_fetch_urls(urls, output_dir, "result"))
        if files:
            generation.artifact_storage_key = (
                max(files, key=lambda path: path.stat().st_mtime)
                .relative_to(settings.storage_root)
                .as_posix()
            )
            return
    raise UpstreamPendingError(f"{label} 上游仍在生成中，稍后自动取回。")


def _validated_images(paths: list[Path]) -> list[Path]:
    images = []
    for path in paths:
        try:
            validate_artifact(path)
        except InvalidImage:
            continue
        images.append(path)
    return images


class _OversizedDownload(Exception):
    """上游返回的体积超过单张图片的上限。"""


def _assert_download_url_allowed(url: str) -> None:
    """结果下载地址必须与 API 入口地址受同等约束。"""
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ProviderConfigError("结果下载地址必须是 HTTPS")
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        raise ProviderConfigError("结果下载地址缺少主机名")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ProviderConfigError("结果下载地址端口无效") from exc
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ProviderConfigError("结果下载地址域名无法解析") from exc
    if not addresses:
        raise ProviderConfigError("结果下载地址域名未解析到地址")
    for address in addresses:
        _assert_address_is_public(address, hostname, "结果下载地址")


def _guard_download_request(request: httpx.Request) -> None:
    """httpx 请求钩子：初始请求与每一跳重定向都要复查地址。"""
    _assert_download_url_allowed(str(request.url))


def _download_client() -> httpx.Client:
    """结果下载专用的 httpx 客户端。"""
    return httpx.Client(
        timeout=60,
        follow_redirects=True,
        event_hooks={"request": [_guard_download_request]},
    )


def _download_result_image(url: str, target: Path) -> None:
    """按**与结果下载同等**的约束取回一张图（API 渠道专用）。"""
    limit = settings.max_artifact_bytes
    target.parent.mkdir(parents=True, exist_ok=True)
    with _download_client() as client, client.stream("GET", url) as response:
        response.raise_for_status()
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise RuntimeError("图像 API 返回文件超过大小限制")
        written = 0
        with open(target, "wb") as handle:
            for chunk in response.iter_bytes():
                written += len(chunk)
                if written > limit:
                    raise RuntimeError("图像 API 返回文件超过大小限制")
                handle.write(chunk)


def _fetch_urls(urls: list[str], output_dir: Path, prefix: str) -> list[Path]:
    """Direct download fallback when the skill's batch downloader finds nothing."""
    output_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    limit = settings.max_artifact_bytes
    with _download_client() as client:
        for index, url in enumerate(urls, 1):
            suffix = Path(urlsplit(url).path).suffix or ".png"
            target = output_dir / f"{prefix}_{index:02d}{suffix}"
            try:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > limit:
                        raise _OversizedDownload
                    written = 0
                    with open(target, "wb") as handle:
                        for chunk in response.iter_bytes():
                            written += len(chunk)
                            if written > limit:
                                raise _OversizedDownload
                            handle.write(chunk)
            except (httpx.HTTPError, OSError, ProviderConfigError, _OversizedDownload) as exc:
                target.unlink(missing_ok=True)
                # 被拒绝的地址是安全信号，不能静默吞掉。
                logger.warning("跳过不可用的结果地址 %s：%s", url, exc)
                continue
            files.append(target)
    return files


def _run_lovart(*args: str, execution_config: dict | None = None) -> dict:
    config = execution_config
    if config is None:
        # 唯一没有显式传配置的调用方是历史遗留路径；执行配置必须可解析。
        raise RuntimeError("Lovart 调用缺少执行配置（冻结配置未提供）")
    access_key = str(config["access_key"])
    secret_key = str(config["secret_key"])
    script = Path(str(config["skill_script"]))
    # skill 的本地状态（~/.lovart/state.
    state_home = _skill_state_home(
        "lovart", str(config.get("provider_account_id") or "")
    )
    state_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    stdout = _run_skill_script(
        script, list(args),
        env_overrides={
            "LOVART_ACCESS_KEY": access_key,
            "LOVART_SECRET_KEY": secret_key,
            "LOVART_BASE_URL": str(config["base_url"]),
            # 把 expanduser("~") 指到本账号目录，见 _skill_state_home。
            "HOME": str(state_home),
            "USERPROFILE": str(state_home),
        },
        timeout=int(config.get("chat_timeout") or settings.provider_command_timeout_seconds),
        secret_values=(access_key, secret_key), script_dir=script,
    )
    return _parse_skill_json(stdout, "Lovart 返回结果无法解析")


def _lovart_downloaded_images(result: dict, output_dir: Path | None = None) -> list[Path]:
    """Collect on-disk image paths a Lovart skill command actually produced."""
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
    """Attach the chosen ratio/resolution to the prompt, leaving the text intact."""
    prompt = _effective_prompt(generation)
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
    # `chat --download` 的内联下载会失败
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


def probe_libtv_connection(account_id: str = "") -> tuple[bool, str]:
    """调一次 doctor（只读），证明该账号的 OAuth 授权与 MCP 通道都可用。"""
    if not libtv_mcp.authorized(account_id):
        return False, "LibTV 尚未授权连接"
    try:
        payload = libtv_mcp.call_tool("doctor", timeout=45, account_id=account_id)
    except RuntimeError as exc:
        return False, str(exc)
    if str(payload.get("status")) != "ready":
        return False, f"LibTV 连接异常（status={payload.get('status')}）"
    identity = payload.get("identity") or {}
    user_uuid = str(identity.get("userUuid") or "")[:8]
    return True, f"LibTV 连接正常（身份 {user_uuid}…，MCP 通道就绪）"


def probe_lovart_connection(account_id: str = "") -> tuple[bool, str]:
    """探活指定账号槽的 Lovart 配置（池里的号各测各的）。"""
    try:
        config = _current_native_execution_config("lovart", account_id)
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


class ApiTransientError(RuntimeError):
    """上游瞬时失败（过载 / 限流 / 5xx / 查询超时）。退避后重提有意义。"""


class ApiPollTimeoutError(RuntimeError):
    """轮询到 deadline 上游仍未给出终态。**故意不是** ``ApiTransientError``。"""

    def __init__(self, message: str, task_id: str = "") -> None:
        super().__init__(message)
        self.task_id = task_id


#: 判为「重试没用」的标记先匹配：额度、鉴权、参数、内容拦截。
_API_PERMANENT_MARKERS = (
    "credits not enough",
    "insufficient",
    "quota",
    "unauthorized",
    "forbidden",
    "invalid",
    "not found",
    "unsupported",
    "content",
    "safety",
    "blocked",
)
#: 判为「这个账号额度不够了」的标记 —— **只有额度类**才触发换号。
_EXHAUSTED_MARKERS = (
    "credits not enough",
    "insufficient",
    "quota",
)

#: 判「额度不足」时要**先排除**的相邻措辞。
_API_ROUTE_AUTH_MARKERS = (
    "unauthorized",
    "forbidden",
    "permission",
    "authentication",
    "api key",
    "api-key",
)


def provider_error_means_exhausted(message: str) -> bool:
    """上游错误文本是否表示「这个账号的额度不够了」。"""
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _EXHAUSTED_MARKERS)


def _api_error_faults_route(exc: Exception) -> bool:
    """这次失败该不该归到**这条路由**头上（= 是否标「额度耗尽」；**保守判据**）。"""
    if isinstance(exc, (ApiTransientError, httpx.HTTPError)):
        # 连不上/过载/限流：``ApiTransientError`` 是执行器对这类失败的归类，直接采信。
        return True
    message = str(exc)
    lowered = message.lower()
    if any(marker in lowered for marker in _API_ROUTE_AUTH_MARKERS):
        return False
    # 额度词表要看，瞬时词表也要看：重试耗尽后执行器抛出
    return provider_error_means_exhausted(message) or _api_error_is_transient(message)


#: 判为「瞬时」的标记。
_API_TRANSIENT_MARKERS = (
    "load is too high",
    "try again later",
    "temporarily",
    "timeout",
    "timed out",
    "rate limit",
    "too many requests",
    "busy",
)


def _api_error_text(payload: object) -> str:
    """从各种上游错误信封里抠出一句话。"""
    if not isinstance(payload, dict):
        return ""
    candidate: object = payload.get("error")
    if isinstance(candidate, dict):
        candidate = candidate.get("message") or ""
    if not candidate:
        for key in ("message", "msg", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                candidate = value
                break
    return str(candidate or "")


def _api_error_is_transient(message: str, status_code: int | None = None) -> bool:
    lowered = (message or "").lower()
    if any(marker in lowered for marker in _API_PERMANENT_MARKERS):
        return False
    if status_code is not None and (status_code == 429 or 500 <= status_code < 600):
        return True
    return any(marker in lowered for marker in _API_TRANSIENT_MARKERS)


def _api_post(client: httpx.Client, url: str, headers: dict, body: dict) -> tuple[int, dict]:
    """发一次请求并尽力解析 JSON。**不调 raise_for_status** —— 状态码要参与分类。"""
    response = client.post(url, headers=headers, json=body)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return response.status_code, payload


def _api_poll_task(
    client: httpx.Client, api_base: str, headers: dict, task_id: str, deadline: float
) -> dict:
    """轮询异步任务到终态，返回终态 payload。"""
    url = f"{api_base}/api/result"
    interval = max(float(settings.api_poll_seconds), 0.5)
    seen_status = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ApiPollTimeoutError(
                f"轮询任务 {task_id} 超时（上游仍未给出终态）", task_id=task_id
            )
        time.sleep(min(interval, remaining))
        try:
            status_code, payload = _api_post(client, url, headers, {"id": task_id})
        except httpx.HTTPError as exc:
            logger.warning("查询任务 %s 状态时网络异常，继续轮询：%s", task_id, exc)
            continue
        if status_code >= 400:
            message = _api_error_text(payload) or f"HTTP {status_code}"
            if _api_error_is_transient(message, status_code):
                logger.warning("查询任务 %s 遭瞬时错误（%s），继续轮询", task_id, message)
                continue
            raise RuntimeError(f"图像 API 查询失败：{message}")
        status = str(payload.get("status") or "")
        if status != seen_status:
            logger.info("任务 %s 状态：%s", task_id, status or "未知")
            seen_status = status
        if status == "succeeded":
            return payload
        if status in {"failed", "error", "canceled", "cancelled"}:
            message = _api_error_text(payload) or "上游未给出失败原因"
            if _api_error_is_transient(message):
                # 过载导致的失败：交给上层退避重提，而不是判死退款。
                raise ApiTransientError(f"任务 {task_id} 因上游过载失败：{message}")
            raise RuntimeError(f"图像 API 生成失败：{message}")


def _api_run_once(
    client: httpx.Client, *, submit_url: str, api_base: str, headers: dict, body: dict,
    async_mode: bool, deadline: float,
) -> dict:
    """提交一次（异步时再轮询到终态）。瞬时失败抛 ``ApiTransientError``。"""
    status_code, payload = _api_post(client, submit_url, headers, body)
    if status_code >= 400 or str(payload.get("status") or "") == "failed":
        message = _api_error_text(payload) or f"HTTP {status_code}"
        if _api_error_is_transient(message, status_code):
            raise ApiTransientError(message)
        raise RuntimeError(f"图像 API 提交失败：{message}")
    if not async_mode:
        return payload
    task_id = str(payload.get("id") or "")
    if not task_id:
        # 返回 200 却没有 id：上游契约变了。别静默当成"排队中"，否则会一直等到超时。
        raise RuntimeError("图像 API 异步提交未返回任务 ID")
    # 轮询有自己的一份预算，且不得超过整个任务的 deadline：``api_poll_timeout_seconds``
    poll_deadline = min(
        deadline, time.monotonic() + max(float(settings.api_poll_timeout_seconds), 1.0)
    )
    return _api_poll_task(client, api_base, headers, task_id, poll_deadline)


def _execute_api(generation: Generation, profile: ModelProfile) -> None:
    params = _params(generation)
    resolution = params.get("resolution")
    ratio = params.get("ratio")
    quality = params.get("quality")
    if resolution not in profile.resolutions or resolution not in API_RESOLUTIONS:
        raise RuntimeError("API 任务包含执行器不支持的分辨率")
    if ratio not in profile.ratios or ratio not in API_RATIOS:
        raise RuntimeError("API 任务包含执行器不支持的比例")
    # nano-banana 系列没有质量维度（profile.qualities 为空），此时忽略前端带来的值。
    if profile.qualities and (quality not in profile.qualities or quality not in API_QUALITIES):
        raise RuntimeError("API 任务包含执行器不支持的质量参数")
    if len(generation.references) > profile.max_references:
        raise RuntimeError("API 任务参考图数量超过冻结能力上限")
    # 请求形状由**冻结配置里的那条路由**决定
    api_config = _load_api_execution_config(profile.execution_config_id)
    family = str(api_config.get("family") or "") or api_model_family(
        profile.upstream_model
    )
    builtin = builtin_api_model(profile.upstream_model) if family == "grsai_native" else None
    # 异步只在 Grsai 原生端点上成立：OpenAI 兼容上游没有 /api/result，也无从轮询。
    if api_config.get("async_declared"):
        async_mode = bool(api_config.get("async")) and family == "grsai_native"
    else:
        async_mode = builtin is not None and settings.api_reply_mode == "async"
    body: dict = {
        # 发出去的是**这次实际选中的上游名字**（一个模型可能对应几家上游的叫法）
        "model": profile.upstream_exec or profile.upstream_model,
        "prompt": _effective_prompt(generation),
    }
    if builtin is not None:
        # Grsai 原生端点：比例与档位分开传
        if builtin.aspect_mode == "ratio_tier":
            body["aspectRatio"] = ratio
            body["imageSize"] = resolution
        else:
            pixel = (builtin.pixels or {}).get(ratio, {}).get(resolution)
            if not pixel:
                raise RuntimeError(f"上游不提供该比例与分辨率的组合：{ratio} · {resolution}")
            body["aspectRatio"] = pixel
        # 实测：async 提交约 0.9 秒返回 task id（且提交侧没有并发上限）
        body["replyType"] = "async" if async_mode else "json"
        endpoint = "/api/generate"
        reference_key = "images"
    elif family == "grsai_native":
        # 族声明为 Grsai 原生、但模型不在内置尺寸表里（管理员新加的上游模型）
        body["aspectRatio"] = ratio
        body["imageSize"] = resolution
        body["replyType"] = "async" if async_mode else "json"
        endpoint = "/api/generate"
        reference_key = "images"
    else:
        # 其他 API 上游（火山方舟、自建中转）走 OpenAI 兼容端点：只有像素 size
        pixel = ""
        official = builtin_api_model(profile.upstream_model)
        if official is not None and official.pixels:
            pixel = (official.pixels or {}).get(ratio, {}).get(resolution, "")
            if not pixel:
                raise RuntimeError(f"上游不提供该比例与分辨率的组合：{ratio} · {resolution}")
        if pixel:
            body["size"] = pixel
        else:
            edge = API_SIZE_EDGES.get(resolution, 1024)
            dimensions = {
                "1:1": (edge, edge),
                "4:3": (edge, edge * 3 // 4),
                "3:4": (edge * 3 // 4, edge),
                "16:9": (edge, edge * 9 // 16),
                "9:16": (edge * 9 // 16, edge),
            }
            width, height = dimensions.get(ratio, (edge, edge))
            body["size"] = f"{width}x{height}"
        endpoint = "/images/generations"
        reference_key = "image"
    # 没有质量维度的模型不传这个字段，否则上游会收到一个对它毫无意义的取值。
    if quality and profile.qualities:
        body["quality"] = quality
    references = _references(generation)
    if references:
        body[reference_key] = [_data_url(path) for path in references]
    api_base = str(api_config["base_url"]).rstrip("/")
    submit_url = f"{api_base}{endpoint}"
    headers = _api_request_headers(api_config, profile.upstream_model)
    # 总时长的兜底是 provider_command_timeout_seconds：重试与轮询都从这一个 deadline
    deadline = time.monotonic() + max(settings.provider_command_timeout_seconds, 1)
    attempts = max(int(settings.api_submit_retries), 0) + 1
    payload: dict = {}
    with httpx.Client(timeout=300) as client:
        for attempt in range(1, attempts + 1):
            try:
                payload = _api_run_once(
                    client, submit_url=submit_url, api_base=api_base, headers=headers,
                    body=body, async_mode=async_mode, deadline=deadline,
                )
                break
            except ApiPollTimeoutError as exc:
                # 任务已经在上游存在（可能已在计费），所以**不重提**。
                if exc.task_id:
                    generation.external_task_id = exc.task_id
                raise
            except ApiTransientError as exc:
                if attempt >= attempts:
                    raise RuntimeError(
                        f"图像 API 连续 {attempts} 次遭遇上游瞬时故障：{exc}"
                    ) from exc
                delay = max(float(settings.api_retry_backoff_seconds), 0.0) * attempt
                logger.warning(
                    "图像 API 第 %d/%d 次尝试遭遇瞬时故障（%s），%.1fs 后退避重提",
                    attempt, attempts, exc, delay,
                )
                time.sleep(delay)
    generation.external_task_id = str(payload.get("id") or "")
    # 原生端点返回 results[].url；data[] / b64_json 是 OpenAI 兼容端点的形状。
    results = payload.get("results") or payload.get("data") or []
    image = results[0] if isinstance(results, list) and results else {}
    output_dir = settings.storage_root / f"generations/{generation.id}/results"
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "result.png"
    if image.get("b64_json"):
        blob = base64.b64decode(image["b64_json"])
        if len(blob) > settings.max_artifact_bytes:
            raise RuntimeError("图像 API 返回文件超过大小限制")
        target.write_bytes(blob)
    elif image.get("url"):
        # 上游回传的地址与 API 入口受同等约束（地址校验 + 流式体积上限）—
        _download_result_image(str(image["url"]), target)
    else:
        # 异步路径的终态形状只能靠真实运行确认，所以把上游实际返回的字段记进日志
        logger.warning(
            "图像 API 未返回图片（任务 %s，状态 %s，返回字段 %s）",
            generation.external_task_id, payload.get("status"), sorted(payload),
        )
        raise RuntimeError("图像 API 未返回图片")
    validate_artifact(target)
    generation.artifact_storage_key = str(target.relative_to(settings.storage_root))


def _validate_frozen_capabilities(generation: Generation, profile: ModelProfile) -> None:
    params = _params(generation)
    if params.get("ratio") not in profile.ratios:
        raise RuntimeError("任务比例不在冻结能力范围内")
    if params.get("resolution") not in profile.resolutions:
        raise RuntimeError("任务分辨率不在冻结能力范围内")
    if profile.qualities and params.get("quality") not in profile.qualities:
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
    # 快照是当时的 dataclass 定义 asdict() 出来
    known_fields = {field.name for field in fields(ModelProfile)}
    payload = {key: value for key, value in payload.items() if key in known_fields}
    try:
        return ModelProfile(**payload)
    except TypeError:
        if payload.get("execution_config_id"):
            return None
        return get_profile(f"{generation.provider}:{generation.model_id}")


def _generation_reference_paths(generation: Generation) -> list[str]:
    """这次任务参考图在磁盘上的路径。"""
    paths: list[str] = []
    for reference in sorted(generation.references, key=lambda item: item.position):
        try:
            path = resolve_storage_key(reference.storage_key)
        except ValueError:  # 存储键非法：跳过而不是让整个任务挂掉
            continue
        if path.is_file():
            paths.append(str(path))
    return paths


def _apply_prompt_optimization(session, generation: Generation) -> None:
    """优化是生图管线里的前置步骤，结果**不回填输入框**、直接发给上游："""
    if generation.prompt_optimize_state != prompt_optimize.STATE_REQUESTED:
        return
    mode = prompt_optimize.normalize_mode(generation.prompt_optimize_mode)

    def _bail(state: str, message: str) -> None:
        generation.prompt_optimize_state = state
        generation.prompt_optimize_error = message[:2000]
        session.commit()

    if not prompt_optimize.is_configured(mode=mode):
        logger.warning("任务 %s 申请了 %s 模式优化，但服务端未启用", generation.id, mode)
        _bail(prompt_optimize.STATE_SKIPPED, f"未配置提示词优化 skill（{mode} 模式未启用）")
        return
    try:
        images = (
            _generation_reference_paths(generation)
            if mode == prompt_optimize.MODE_AGENT
            else []
        )
        optimized, note = prompt_optimize.optimize_prompt(
            generation.original_prompt, mode=mode, image_paths=images
        )
    except Exception as exc:  # noqa: BLE001 —— 任何失败都只降级，不判任务失败
        logger.warning("提示词优化失败（任务 %s / %s），改用原始提示词：%s",
                       generation.id, mode, exc)
        _bail(prompt_optimize.STATE_FAILED, str(exc))
        return
    generation.optimized_prompt = optimized
    generation.prompt_optimize_note = note
    generation.prompt_optimize_state = prompt_optimize.STATE_DONE
    generation.prompt_optimize_error = ""
    generation.prompt_optimize_mode = mode
    session.commit()
    logger.info("任务 %s 的提示词已优化（模式 %s，%d → %d 字，诊断 %d 字）",
                generation.id, mode, len(generation.original_prompt or ""),
                len(optimized), len(note))


# ---------------------------------------------------------------------------


def _merge_usage(existing_json: str, stage: str, payload: dict | None) -> str:
    """把一次外部调用的用量追加进 ``usage_json``（按阶段留痕）。"""
    try:
        current = json.loads(existing_json or "{}")
    except json.JSONDecodeError:
        current = {}
    if not isinstance(current, dict):
        current = {}
    stages = current.get("stages")
    if not isinstance(stages, list):
        stages = []
    stages.append({"stage": stage, "usage": payload if payload else None})
    current["stages"] = stages
    return json.dumps(current, ensure_ascii=False)


def _apply_agent_plan(session, generation: Generation) -> None:
    """把 agent 计划渲染成发给上游的提示词。"""
    spec = agent.normalize_spec(generation.agent_spec_json)
    # 把收敛后的 spec 写回：历史里看到的必须是**实际生效**的那套轴，而不是前端原样传来的。
    generation.agent_spec_json = json.dumps(spec, ensure_ascii=False)
    if not prompt_optimize.is_configured(mode=prompt_optimize.MODE_PLAN):
        raise RuntimeError("agent 节点需要服务端配置好提示词计划 skill（agent 模式未启用）")
    try:
        fields, note, usage = prompt_optimize.plan_prompt(
            generation.original_prompt,
            image_paths=_generation_reference_paths(generation),
            params=agent.spec_to_script_params(spec),
            platform_norms_text=agent.render_platform_norms(
                spec.get("platform", ""), spec.get("work_type", "")
            ),
        )
    except Exception as exc:  # 统一翻成任务失败，原因带上；这里必须重抛，所以 BLE001 不适用
        raise RuntimeError(f"agent 计划失败：{exc}") from exc
    rendered = agent.render_prompt(fields)
    if not rendered.strip():
        raise RuntimeError("agent 计划渲染出的提示词为空")
    generation.optimized_prompt = rendered
    generation.prompt_optimize_note = note
    generation.agent_plan_json = json.dumps(fields, ensure_ascii=False)
    generation.usage_json = _merge_usage(generation.usage_json, "plan", usage)
    session.commit()
    logger.info("agent 计划完成（任务 %s）：%d 个字段、提示词 %d 字、用量 %s",
                generation.id, len(fields), len(rendered), usage or "未取得")


def _record_generation_usage(session, generation: Generation) -> None:
    """记下**生图这一步**的消耗。按张计价，所以没有 token 可记，只有张数与参数。"""
    detail: dict = {"images": 1}
    try:
        parameters = json.loads(generation.parameters_json or "{}")
    except json.JSONDecodeError:
        parameters = {}
    for key in ("ratio", "resolution", "quality"):
        if parameters.get(key):
            detail[key] = parameters[key]
    generation.usage_json = _merge_usage(generation.usage_json, "generate", detail)
    session.commit()


def _validate_agent_artifact(session, generation: Generation) -> None:
    """一个已经出图的任务因为"背景不够白"被判定失败，用户白等一场，那是最糟的结果。"""
    from image_hub.agent_checks import check_artifact

    spec = agent.normalize_spec(generation.agent_spec_json)
    try:
        parameters = json.loads(generation.parameters_json or "{}")
    except json.JSONDecodeError:
        parameters = {}
    try:
        machine = agent.machine_check_spec(spec.get("platform", ""))
        result = check_artifact(
            resolve_storage_key(generation.artifact_storage_key),
            expected_ratio=str(parameters.get("ratio") or ""),
            want_pure_white=bool(machine.get("pure_white")),
            frame_fill_min=machine.get("frame_fill_min"),
        )
    except Exception as exc:  # noqa: BLE001 —— 校验本身出错不该影响交付
        result = {"status": "unknown", "checks": {}, "warnings": [f"校验未能执行：{exc}"]}
    generation.validation_json = json.dumps(result, ensure_ascii=False)
    session.commit()
    if result.get("warnings"):
        logger.info("agent 产物机检提醒（任务 %s）：%s", generation.id, result["warnings"])


def _frozen_account_id(profile: ModelProfile) -> str:
    """冻结配置里这笔任务**实际在用**的平台账号（空串 = 默认槽 / 读不到配置）。"""
    try:
        config = _native_execution_config(profile)
    except RuntimeError:
        return ""
    return _provider_account_id(config)


def _rotate_to_next_account(
    session: Session,
    generation: Generation,
    profile: ModelProfile,
    failed_account_id: str,
    tried: list[str],
) -> ModelProfile | None:
    """换到固定顺序里的下一个可用号，并**重新冻结**执行配置；到头返回 ``None``。"""
    if not failed_account_id:
        # 默认槽没有「下一个号」可换：维持既有失败收敛。
        return None
    tried.append(failed_account_id)
    next_account = provider_pool.advance_account(
        session,
        profile.provider,
        failed_account_id,
        exclude_ids=tried,
        ready=lambda account: account_credentials_ready(profile.provider, account.id),
    )
    if next_account is None:
        return None
    try:
        identity = _freeze_native_execution_config(profile.provider, next_account.id)
    except ProviderConfigError:
        # 目标号在这一刻凭据又不齐了（极少见）：放弃轮换，按原错误收敛。
        return None
    rotated = replace(profile, execution_config_id=identity)
    generation.provider_account_id = next_account.id
    generation.provider_snapshot_json = json.dumps(
        rotated.snapshot_dict(), ensure_ascii=False
    )
    # 立刻落库：换号是可观测的状态变化（管理页要显示当前号、审计要能追）
    _checkpoint(generation)
    return rotated


def _execute_api_route(
    session: Session,
    generation: Generation,
    profile: ModelProfile,
    executor: Callable[[Generation, ModelProfile], None],
) -> ModelProfile:
    """API 通道：执行一次；失败只**记账**，不换路（人工路由）。"""
    # 这个模型在几家上游都有名字时
    upstream = pick_api_upstream(
        session,
        api_upstream_names(profile.upstreams, profile.upstream_model),
        profile.api_route,
    )
    if not upstream:
        raise RuntimeError(
            f"模型「{profile.label or profile.upstream_model}」在所有上游上都已被判定为不可用，"
            "请到管理页清掉失效标记或改选一条路由"
        )
    profile = replace(profile, upstream_exec=upstream)
    try:
        executor(generation, profile)
    except UpstreamPendingError:
        # 「上游还在生成」不是错误：不记错误、不标记失效。
        raise
    except Exception as exc:  # 刻意捕获全部失败：记不记账由下面的判据决定
        message = str(exc)
        route = profile.api_route
        # 记账挂在**实际发出去**的那个名字上
        failed = profile.upstream_exec or profile.upstream_model
        if route:
            if _api_error_faults_route(exc):
                first = api_routing.mark_exhausted(session, failed, route, message)
                if first:
                    logger.warning(
                        "API 路由 %s 在模型 %s 上被判失效：%s",
                        route,
                        failed,
                        message,
                    )
            else:
                # 没命中判据也要留原文：多 API 的中转上游报错措辞彼此不同
                api_routing.record_error(session, failed, route, message)
            # ⚠️ 必须 flush：紧接着就抛异常
            session.flush()
        raise
    return profile


def _execute_with_account_rotation(
    session: Session,
    generation: Generation,
    profile: ModelProfile,
    executor: Callable[[Generation, ModelProfile], None],
) -> ModelProfile:
    """平台渠道的额度失效轮换：可换号时才换，换号即重提。"""
    if profile.provider == "api":
        # API 通道不走账号池：它只执行一次并记账，选路由管理员人工决定。
        return _execute_api_route(session, generation, profile, executor)
    if not provider_pool.is_pool_provider(profile.provider):
        executor(generation, profile)
        return profile

    tried: list[str] = []
    last_error: Exception | None = None
    # 上界 = 池里号数 + 1。
    attempts = len(provider_pool.pool_accounts(session, profile.provider)) + 1
    for _ in range(attempts):
        try:
            executor(generation, profile)
            return profile
        except UpstreamPendingError:
            # 「上游还在生成」不是错误：不记错误、不换号，行为与账号池上线前一致。
            raise
        except Exception as exc:
            last_error = exc
            message = str(exc)
            current_account_id = _frozen_account_id(profile)
            if not provider_error_means_exhausted(message):
                if current_account_id:
                    # 没命中词表也要留原文：识别词表**未经真实校准**（库里零样本）
                    provider_pool.record_error(session, current_account_id, message)
                    # ⚠️ 必须 flush：紧接着就抛异常
                    session.flush()
                raise
            if current_account_id:
                provider_pool.mark_exhausted(session, current_account_id, message)
                # autoflush 是关掉的（项目约定）
                session.flush()
            if generation.external_task_id:
                # 已被上游受理（可能已在计费）：只标记失效供**后续**任务路
                raise
            rotated = _rotate_to_next_account(
                session, generation, profile, current_account_id, tried
            )
            if rotated is None:
                raise ProviderAccountsExhaustedError(
                    "所有平台账号的额度都不可用，任务从未被上游受理"
                ) from exc
            profile = rotated
    raise ProviderAccountsExhaustedError(
        "平台账号轮换已达上界，任务从未被上游受理"
    ) from last_error


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
            if generation.mode == agent.MODE_AGENT:
                # ⭐ agent 节点**只在 API 渠道**跑：平台渠道（libtv/lovart）不参与按消耗
                if profile.provider != "api":
                    raise RuntimeError("agent 节点只支持 API 渠道的模型")
                _apply_agent_plan(session, generation)
            else:
                _apply_prompt_optimization(session, generation)
            executor = {"libtv": _execute_libtv, "lovart": _execute_lovart, "api": _execute_api}.get(
                profile.provider
            )
            if executor is None:
                raise RuntimeError("任务执行器未知")
            # 平台渠道：额度不足时按固定顺序换号重试
            profile = _execute_with_account_rotation(session, generation, profile, executor)
            generation.status = "succeeded"
            if generation.mode == agent.MODE_AGENT:
                _record_generation_usage(session, generation)
                _validate_agent_artifact(session, generation)
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 只有 failed 才收敛。
            if isinstance(exc, ProviderAccountsExhaustedError):
                # 任务**从未被上游受理**（抛它的前提就是 ``external_task_id`` 为空）
                generation.status = "failed"
            else:
                generation.status = (
                    "recovery_required"
                    if generation.external_project_id or generation.external_task_id
                    else "failed"
                )
            if isinstance(exc, UpstreamPendingError):
                # ⭐ **等待不是错误**：上游还在生成时
                generation.error_message = ""
                logger.info("上游仍在生成（任务 %s）：%s", generation.id, exc)
            else:
                # 真实原因要留在库里：展示层（_safe_error / _generation_dict）已经负责
                generation.error_message = str(exc)[-2000:]
            if profile and profile.provider == "api":
                logger.warning("API 生成失败（任务 %s）：%s", generation.id, exc)
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
            if not generation.external_project_id or not generation.external_task_id:
                raise RuntimeError("LibTV 任务缺少外部任务标识")
            config = _native_execution_config(profile)
            account_id = _provider_account_id(config)
            if _libtv_retired_agent(profile):
                # 已下线的「智能编排」任务：它的 external_task_id 是 runId
                raise RuntimeError(
                    "LibTV 智能编排通道已下线，无法继续查询该任务；请改用具体模型重新提交。"
                )
            try:
                _poll_libtv_task(
                    generation, generation.external_project_id,
                    generation.external_task_id, output_dir, str(profile.label), account_id,
                )
            except UpstreamPendingError as exc:
                # 仍在生成：保持待恢复、**不写 error_message**（避免界面显示成错误）。
                generation.error_message = ""
                session.commit()
                return False, str(exc)
            except RuntimeError as exc:
                generation.error_message = str(exc)[-2000:]
                session.commit()
                return False, generation.error_message
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

