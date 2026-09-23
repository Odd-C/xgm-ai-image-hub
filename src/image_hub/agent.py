"""Agent 节点：结构化轴驱动的生成工作流（域逻辑）。"""

from __future__ import annotations

import json
from pathlib import Path

from image_hub import prompt_optimize
from image_hub.prompt_optimize import (
    CATEGORIES,
    WORK_TYPES,
    category_label,
    work_type_label,
)

#: ``generations.mode`` 的取值。空串 = 普通生图。
MODE_AGENT = "agent"

#: 平台规范 YAML 所在目录（随包发布，运行时必读，所以放在包内而不是 skills/）。
PLATFORM_NORMS_DIR_NAME = "platform_norms"


# ---------------------------------------------------------------------------
PLATFORMS: tuple[tuple[str, str], ...] = (
    ("general", "通用（不套具体平台规范）"),
    ("amazon", "Amazon"),
    ("walmart", "Walmart"),
    ("ebay", "eBay"),
    ("etsy", "Etsy"),
    ("shopify", "Shopify（独立站）"),
    ("tiktok_shop", "TikTok Shop"),
    ("temu", "Temu"),
)

#: 市场是**独立于平台**的一根轴：同一个平台有多个站点（amazon.com/.de/.co.jp）
MARKETS: tuple[tuple[str, str], ...] = (
    ("general", "通用（不做文化适配）"),
    ("us", "美国"),
    ("eu", "欧洲"),
    ("uk", "英国"),
    ("jp", "日本"),
    ("ca", "加拿大"),
    ("au", "澳洲"),
    ("sea", "东南亚"),
    ("mena", "中东"),
)

#: 卖点：**枚举为主 + 一个自由补充框**。枚举保证可回归测试，自由文本留灵活性。
SELLING_POINTS: tuple[tuple[str, str], ...] = (
    ("general", "通用（不提卖点）"),
    ("durability", "结实 / 承重"),
    ("comfort", "舒适 / 人体工学"),
    ("quiet", "静音"),
    ("easy_assembly", "易安装 / 易收纳"),
    ("space_saving", "省空间"),
    ("material_quality", "材质 / 做工"),
    ("safety", "安全（儿童 / 老人）"),
    ("premium_look", "质感 / 高级感"),
)

#: **参考节点**（用户的原话就是这个 —— 它们是画布上一等输入，不是图片上的标签）。
REFERENCE_ROLES: tuple[tuple[str, str], ...] = (
    ("product", "产品外观"),
    ("scene", "场景参考"),
    ("angle", "角度参考"),
    ("layout", "排版参考"),
    ("model", "模特参考"),
)

#: 必选职责，**只有产品外观**。前端据此标必填，提交侧据此校验。
REQUIRED_REFERENCE_ROLES: tuple[str, ...] = ("product",)

#: 参考图**不标职责**时用的说法。用户口径（2026-09-21）：「留空标注一下，服务端会补上」
UNLABELED_ROLE_LABEL = "留空（由服务端判断）"

#: ⭐ 工作类型 → **这个功能调用哪些参考节点**。用户口径（2026-09-21）
WORK_TYPE_ROLES: dict[str, dict[str, tuple[str, ...]]] = {
    "replace": {
        "required": ("product",),
        "optional": ("scene", "angle", "layout", "model"),
    },
    "white_background": {
        "required": ("product",),
        # 白底主图不吃场景与排版（道具/版式会被带进画面，违反主图规则）
        "optional": ("angle", "model"),
    },
    "repair": {
        "required": ("product",),
        "optional": ("scene", "angle", "model"),
    },
}


def roles_for_work_type(work_type: str) -> dict[str, tuple[str, ...]]:
    """某个工作类型需要哪些参考节点。认不出就退回「只必选产品外观」。"""
    key = _normalize_work_type(work_type)
    return WORK_TYPE_ROLES.get(key, {"required": REQUIRED_REFERENCE_ROLES, "optional": ()})

DEFAULT_PLATFORM, DEFAULT_MARKET, DEFAULT_SELLING_POINT = "general", "general", "general"
_PLATFORM_LABELS = dict(PLATFORMS)
_MARKET_LABELS = dict(MARKETS)
_SELLING_LABELS = dict(SELLING_POINTS)
_REFERENCE_LABELS = dict(REFERENCE_ROLES)


def _pick(value: str, table: dict, default: str) -> str:
    candidate = (value or "").strip().lower()
    return candidate if candidate in table else default


def normalize_platform(value: str) -> str:
    return _pick(value, _PLATFORM_LABELS, DEFAULT_PLATFORM)


def normalize_market(value: str) -> str:
    return _pick(value, _MARKET_LABELS, DEFAULT_MARKET)


def normalize_selling_point(value: str) -> str:
    return _pick(value, _SELLING_LABELS, DEFAULT_SELLING_POINT)


def normalize_reference_role(value: str) -> str:
    """参考节点职责收敛。"""
    if not str(value or "").strip():
        return ""
    return _pick(value, _REFERENCE_LABELS, "product")


def platform_label(value: str) -> str:
    return _PLATFORM_LABELS[normalize_platform(value)]


def market_label(value: str) -> str:
    return _MARKET_LABELS[normalize_market(value)]


def selling_point_label(value: str) -> str:
    return _SELLING_LABELS[normalize_selling_point(value)]


# ---------------------------------------------------------------------------


def reference_role_label(value: str) -> str:
    """把职责翻成人话。**未标注**（空值）有它自己的说法，不能 KeyError。"""
    if not str(value or "").strip():
        return UNLABELED_ROLE_LABEL
    return _REFERENCE_LABELS[normalize_reference_role(value)]


def normalize_spec(payload: dict | str | None) -> dict:
    """把前端/别人塞进来的 spec 收敛成已知取值。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload) if payload.strip() else {}
        except json.JSONDecodeError:
            payload = {}
    data = payload if isinstance(payload, dict) else {}
    roles = data.get("reference_roles")
    notes = data.get("role_notes")
    normalized_notes: dict[str, str] = {}
    if isinstance(notes, dict):
        for raw_key, raw_text in notes.items():
            role = _pick(str(raw_key), _REFERENCE_LABELS, "")
            text = str(raw_text or "").strip()
            if role and text:
                normalized_notes[role] = text[:500]
    return {
        "platform": normalize_platform(str(data.get("platform") or "")),
        "market": normalize_market(str(data.get("market") or "")),
        "category": _normalize_category(str(data.get("category") or "")),
        "work_type": _normalize_work_type(str(data.get("work_type") or "")),
        "selling_point": normalize_selling_point(str(data.get("selling_point") or "")),
        # 自由补充：用户用一句话说的额外要求。**不参与枚举校验**，只是透传。
        "extra": str(data.get("extra") or "").strip()[:500],
        "reference_roles": [
            normalize_reference_role(str(role)) for role in roles
        ]
        if isinstance(roles, list)
        else [],
        "role_notes": normalized_notes,
    }


def missing_required_roles(spec: dict) -> list[str]:
    """必选参考节点里**没被满足**的那些。"""
    present = set(spec.get("reference_roles") or []) | set(
        (spec.get("role_notes") or {}).keys()
    )
    return [role for role in REQUIRED_REFERENCE_ROLES if role not in present]


def submission_warnings(spec: dict, *, has_reference_images: bool) -> list[str]:
    """提交时要**提示**（不拦截）的事。合成一个列表，前端逐条显示。"""
    warnings: list[str] = []
    missing = [reference_role_label(role) for role in missing_required_roles(spec)]
    if missing:
        warnings.append(
            f"缺少必选的「{'、'.join(missing)}」参考节点 —— 模型会自行编一个产品，"
            "结果很可能与实物对不上。"
        )
    note = main_image_warning(spec, has_reference_images=has_reference_images)
    if note:
        warnings.append(note)
    return warnings


def _normalize_category(value: str) -> str:
    from image_hub.prompt_optimize import normalize_category

    return normalize_category(value)


def _normalize_work_type(value: str) -> str:
    from image_hub.prompt_optimize import normalize_work_type

    return normalize_work_type(value)


def spec_labels(spec: dict) -> dict:
    """把 key 翻成人话，给 prompt 组装、历史展示、日志共用一份。"""
    return {
        "platform": platform_label(spec.get("platform", "")),
        "market": market_label(spec.get("market", "")),
        "category": category_label(spec.get("category", "")),
        "work_type": work_type_label(spec.get("work_type", "")),
        "selling_point": selling_point_label(spec.get("selling_point", "")),
    }


def spec_to_script_params(spec: dict) -> dict:
    """给脚本的参数字典（键 + 标签一起给，脚本不用再抄一份分类表）。"""
    labels = spec_labels(spec)
    params = {
        axis: {"key": spec.get(axis, ""), "label": labels[axis]}
        for axis in ("platform", "market", "category", "work_type", "selling_point")
    }
    params["reference_roles"] = [
        # ⚠️ 用 reference_role_label 而不是直接查表：留空的职责是合法取值
        {"key": role, "label": reference_role_label(role)}
        for role in (spec.get("reference_roles") or [])
    ]
    # 用文字代替图的那部分（如「场景用文字描述」）—— 脚本按提示词注入。
    params["role_notes"] = [
        {"key": role, "label": _REFERENCE_LABELS[role], "text": text}
        for role, text in (spec.get("role_notes") or {}).items()
    ]
    params["extra"] = spec.get("extra", "")
    return params


def options_payload() -> dict:
    """给前端的可选项 —— **唯一来源**，前端不抄第二份，所以不会漂移。"""
    def pairs(rows):
        return [{"key": key, "label": label} for key, label in rows]

    return {
        "platforms": pairs(PLATFORMS),
        "markets": pairs(MARKETS),
        "categories": pairs(CATEGORIES),
        "work_types": pairs(WORK_TYPES),
        "selling_points": pairs(SELLING_POINTS),
        "reference_roles": pairs(REFERENCE_ROLES),
        # 留空时的说法，前端直接用它当那个空选项的文案 —— 不在前端抄第二份。
        "unlabeled_role_label": UNLABELED_ROLE_LABEL,
        # 前端据此把「产品外观」标成必填；服务端提交时也按它校验。**只有这一处定义**。
        "required_reference_roles": list(REQUIRED_REFERENCE_ROLES),
        # 每个工作类型调用哪些参考节点 —— 前端按它决定"这个功能该显示几个输入口"。
        "work_type_roles": {
            key: {"required": list(value["required"]), "optional": list(value["optional"])}
            for key, value in WORK_TYPE_ROLES.items()
        },
        "defaults": {
            "platform": DEFAULT_PLATFORM,
            "market": DEFAULT_MARKET,
            "category": CATEGORIES[0][0],
            "work_type": WORK_TYPES[0][0],
            "selling_point": DEFAULT_SELLING_POINT,
        },
        # ⚠️ 前端据此在卡片上直接说明「还不能用」：agent 节点的计划要由服务端
        "plan_available": prompt_optimize.is_configured(mode=prompt_optimize.MODE_PLAN),
    }


# ---------------------------------------------------------------------------


def platform_norms_dir() -> Path:
    return Path(__file__).resolve().parent / PLATFORM_NORMS_DIR_NAME


#: 调研产出的**机读约束字典**（唯一数据源，随包发布）。
SPECS_FILE_NAME = "platform_image_specs.json"
_SPECS_CACHE: dict | None = None


def _specs() -> dict:
    """读一次、缓存。文件缺失或坏掉返回 `{}` —— 少一份资料不该让生图崩。"""
    global _SPECS_CACHE
    if _SPECS_CACHE is None:
        path = platform_norms_dir() / SPECS_FILE_NAME
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        _SPECS_CACHE = payload if isinstance(payload, dict) else {}
    return _SPECS_CACHE


def platform_record(platform: str) -> dict:
    """某个平台的规范条目（比例 / 背景 / 填充率 / 来源等级）。认不出返回 `{}`。"""
    key = normalize_platform(platform)
    for item in _specs().get("platforms") or []:
        if isinstance(item, dict) and str(item.get("key") or "") == key:
            return item
    return {}


#: 工作类型 → 字典里的 profile 后缀。
_WORK_TYPE_PROFILE_SUFFIX = {
    "white_background": "main",
    "replace": "lifestyle",
    "repair": "main",
}


def default_profile_key(platform: str, work_type: str = "") -> str:
    """挑一个 profile 键。挑不到就退回该平台在字典里的第一个 profile。"""
    profiles = _specs().get("profiles") or {}
    if not isinstance(profiles, dict):
        return ""
    key = normalize_platform(platform)
    suffix = _WORK_TYPE_PROFILE_SUFFIX.get(_normalize_work_type(work_type), "main")
    candidate = f"{key}_{suffix}"
    if candidate in profiles:
        return candidate
    for name, item in profiles.items():
        if isinstance(item, dict) and str(item.get("platform") or "") == key:
            return str(name)
    return ""


def platform_prompt_constraints(platform: str, work_type: str = "") -> list[str]:
    """该平台**该进提示词**的约束原文（英文，取自字典的 `constraint_blocks`）。"""
    specs = _specs()
    blocks = specs.get("constraint_blocks") or {}
    profiles = specs.get("profiles") or {}
    profile = profiles.get(default_profile_key(platform, work_type))
    if not isinstance(profile, dict):
        return []
    keys = profile.get("constraint_keys")
    if not isinstance(keys, list):
        return []
    lines = []
    for key in keys:
        block = blocks.get(str(key))
        if isinstance(block, dict):
            text = str(block.get("en") or "").strip()
            if text:
                lines.append(text)
    return lines


def platform_aspect_ratios(platform: str) -> list[str]:
    """该平台的**主比例**（单元素列表）。空 = 不限制，让前端回落到模型自身的比例表。"""
    ratio = str((platform_record(platform).get("canvas") or {}).get("ratio") or "").strip()
    return [ratio] if ratio else []


def machine_check_spec(platform: str) -> dict:
    """后置机检需要的两项 —— **机器真判得了**的那两项。"""
    record = platform_record(platform)
    background = record.get("background")
    fill = record.get("fill_pct")
    raw_fill = fill.get("main_min") if isinstance(fill, dict) else None
    return {
        "pure_white": isinstance(background, dict)
        and str(background.get("main") or "") == "pure_white",
        "rgb": background.get("rgb") if isinstance(background, dict) else None,
        "frame_fill_min": (
            float(raw_fill) / 100.0 if isinstance(raw_fill, (int, float)) else None
        ),
    }


def render_platform_norms(platform: str, work_type: str = "") -> str:
    """把该平台**该进提示词**的约束渲染成文本段。"""
    if normalize_platform(platform) == DEFAULT_PLATFORM:
        return ""
    record = platform_record(platform)
    constraints = platform_prompt_constraints(platform, work_type)
    if not record or not constraints:
        return ""
    lines = ["\n\n---\n\n# 平台规范（硬约束，必须落进上面的对应字段）", ""]
    lines.append(f"- 平台：**{record.get('name_zh') or platform}**")
    if str(record.get("source_tier") or "") != "official":
        # 来源等级要**如实说**：二手来源的数字上线前必须核后台
        lines.append(
            "- ⚠️ 本条规范来自**二手来源**（多源交叉一致），上线前请以平台后台为准；"
            "冲突时以「与实物一致、不虚构」为最高原则。"
        )
    if normalize_platform(platform) in _AI_MAIN_IMAGE_RESTRICTED:
        lines.append(
            "- ⚠️ 该平台对「用 AI 凭空生成主图」有限制。这条**不由你拦**"
            "（产品会提示用户），你只需按要求把提示词写好。"
        )
    lines.extend(f"- {text}" for text in constraints)
    return "\n".join(lines)


#: 明确对「AI 凭空生成主图」有约束的平台。
_AI_MAIN_IMAGE_RESTRICTED = frozenset({"amazon", "etsy", "walmart"})


def main_image_warning(spec: dict, *, has_reference_images: bool) -> str:
    """要不要弹「主图风险」提示。"""
    if has_reference_images:
        return ""
    platform = normalize_platform(spec.get("platform", ""))
    if platform not in _AI_MAIN_IMAGE_RESTRICTED:
        return ""
    if _normalize_work_type(spec.get("work_type", "")) != "white_background":
        return ""
    return (
        f"{platform_label(platform)} 不允许用 AI 凭空生成与实物不符的主图"
        "（后果可能是 listing suppression 甚至封号）。"
        "建议上传实物产品图作为参考图；继续生成的风险由你自行承担。"
    )


# ---------------------------------------------------------------------------

#: 顺序有意义：渲染成 prompt 时按这个顺序拼接。
PLAN_FIELDS: tuple[str, ...] = (
    "use_case",
    "asset_type",
    "input_images",
    "primary_request",
    "scene_backdrop",
    "subject",
    "style_medium",
    "composition_framing",
    "lighting_mood",
    "materials_textures",
    "color_palette",
    "text_verbatim",
    "constraints",
    "avoid",
)

#: 至少要有这几段，否则这次计划不算数（**缺提示词比缺元信息严重得多**）。
REQUIRED_PLAN_FIELDS: tuple[str, ...] = ("primary_request",)

#: 渲染时**不**并进 prompt 的字段：它们是元信息，不是画面描述。
_NON_PROMPT_FIELDS = frozenset({"use_case", "asset_type", "input_images"})

#: 诊断/说明字段的候选名（与 14 字段分开存：一句给机器，一段给人）。
_PLAN_NOTE_KEYS = ("diagnosis", "note", "analysis", "reason")


def parse_plan(payload: dict | str) -> tuple[dict, str]:
    """把脚本输出收敛成 ``(14 字段 dict, 给人看的说明)``。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError("agent 计划没有返回 JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("agent 计划不是 JSON 对象")

    # 允许模型把字段嵌在 `fields` / `plan` 下。
    for wrapper in ("fields", "plan", "prompt_fields"):
        inner = payload.get(wrapper)
        if isinstance(inner, dict):
            payload = {**inner, **{k: v for k, v in payload.items() if k != wrapper}}
            break

    flat = {_key(str(k)): v for k, v in payload.items()}
    fields: dict[str, str] = {}
    for name in PLAN_FIELDS:
        value = flat.get(name)
        if isinstance(value, (list, tuple)):
            value = "; ".join(str(item) for item in value if str(item).strip())
        fields[name] = str(value).strip() if isinstance(value, str) else ""

    if not any(fields[name] for name in REQUIRED_PLAN_FIELDS):
        raise RuntimeError(f"agent 计划缺少必填字段：{'/'.join(REQUIRED_PLAN_FIELDS)}")

    note = ""
    for key in _PLAN_NOTE_KEYS:
        value = flat.get(_key(key))
        if isinstance(value, str) and value.strip():
            note = value.strip()
            break
    return fields, note


def _key(name: str) -> str:
    """``Primary request`` / ``primary-request`` / ``PrimaryRequest`` → ``primary_request``。"""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in name.strip().lower())
    return "_".join(part for part in cleaned.split("_") if part)


def render_prompt(fields: dict) -> str:
    """14 字段 → 一段发得出去的提示词。"""
    lines = []
    for name in PLAN_FIELDS:
        if name in _NON_PROMPT_FIELDS:
            continue
        value = (fields.get(name) or "").strip()
        if value:
            lines.append(f"{name.replace('_', ' ')}: {value}")
    return "\n".join(lines).strip()
