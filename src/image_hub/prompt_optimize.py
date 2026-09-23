"""提示词优化：调用**服务器上部署的本地 skill 脚本**（脚本内部自己去调 LLM API）。"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import tempfile
from collections.abc import Sequence
from pathlib import Path

from image_hub.config import settings

logger = logging.getLogger("image_hub.prompt_optimize")

#: 管理台写入的配置文件（落在 storage 下，权限 0600）。
CONFIG_FILE_NAME = "prompt-optimize-config.json"

#: 脚本输出里接受的字段名，按顺序取第一个非空的。
_RESULT_KEYS = ("prompt", "optimized_prompt", "text")

#: 优化任务在 generations 上的状态取值。
STATE_NONE = "none"
STATE_REQUESTED = "requested"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_SKIPPED = "skipped"

#: 两种模式。**共用同一支 skill 脚本，但知识来源不同**
MODE_OPTIMIZE = "optimize"
MODE_AGENT = "agent"
MODES = (MODE_OPTIMIZE, MODE_AGENT)

#: ⚠️ 第三种模式**故意不放进 ``MODES``**：它属于 **agent 节点**（另一类生成节点）
MODE_PLAN = "plan"

#: 第四种模式：**文本节点的「反推提示词」**。同样**不在 `MODES` 里**（不是提示词优化的选项）。
MODE_REVERSE = "reverse"

#: Agent 模式的可选**类目**。
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("general", "通用（无专属清单，按 SKILL.md 的通用锁定项写）"),
    ("ride_on_car", "童车（Ride-On Car）"),
    ("recliner", "躺椅 / 电动升降椅（Recliner / Lift Chair）"),
    ("table_chair", "桌椅套装（Table / Chair Set）"),
    ("mattress", "床垫 / 电动床（Mattress / Adjustable Bed）"),
    ("cover", "车罩 / 软性罩品（Cover / Soft Goods）"),
)

#: 可选**工作类型**。``key`` 与脚本里 templates 的映射一一对应。
WORK_TYPES: tuple[tuple[str, str], ...] = (
    ("replace", "替换（换场景 / 换产品）"),
    ("white_background", "白底主图"),
    ("repair", "精修（产品纠错 / 去 AI 感）"),
)

DEFAULT_CATEGORY, DEFAULT_WORK_TYPE = CATEGORIES[0][0], WORK_TYPES[0][0]
_CATEGORY_LABELS = dict(CATEGORIES)
_WORK_TYPE_LABELS = dict(WORK_TYPES)


def normalize_category(value: str) -> str:
    candidate = (value or "").strip().lower()
    return candidate if candidate in _CATEGORY_LABELS else DEFAULT_CATEGORY


def normalize_work_type(value: str) -> str:
    candidate = (value or "").strip().lower()
    return candidate if candidate in _WORK_TYPE_LABELS else DEFAULT_WORK_TYPE


def category_label(value: str) -> str:
    return _CATEGORY_LABELS[normalize_category(value)]


def work_type_label(value: str) -> str:
    return _WORK_TYPE_LABELS[normalize_work_type(value)]


def options_payload() -> dict:
    """给前端的可选项（**唯一来源**，避免前端再抄一份然后两边漂移）。"""
    return {
        "categories": [{"key": key, "label": label} for key, label in CATEGORIES],
        "work_types": [{"key": key, "label": label} for key, label in WORK_TYPES],
        "defaults": {"category": DEFAULT_CATEGORY, "work_type": DEFAULT_WORK_TYPE},
    }


def agent_params(category: str, work_type: str) -> dict:
    """整理成给脚本的参数字典（键 + 标签一起给，脚本就不用再抄一份分类表）。"""
    resolved_category = normalize_category(category)
    resolved_work = normalize_work_type(work_type)
    return {
        "category": {"key": resolved_category, "label": _CATEGORY_LABELS[resolved_category]},
        "work_type": {"key": resolved_work, "label": _WORK_TYPE_LABELS[resolved_work]},
    }


def normalize_mode(value: str) -> str:
    """把前端传来的模式收敛成已知值。"""
    candidate = (value or "").strip().lower()
    return candidate if candidate in MODES else MODE_OPTIMIZE


def config_path() -> Path:
    return settings.storage_root / CONFIG_FILE_NAME


def _defaults() -> dict:
    return {
        "enabled": bool(settings.prompt_optimize_enabled),
        "script": str(settings.prompt_optimize_script or ""),
        "args": str(settings.prompt_optimize_args or "--prompt-file {prompt_file}"),
        "api_base_url": str(settings.prompt_optimize_api_base_url or ""),
        "api_key": str(settings.prompt_optimize_api_key or ""),
        "model": str(settings.prompt_optimize_model or ""),
        "timeout": int(settings.prompt_optimize_timeout_seconds),
        # Agent 模式单独开关：视觉模型没接好之前可以先不上，不影响纯文本模式。
        "agent_enabled": bool(settings.prompt_optimize_agent_enabled),
        "agent_model": str(settings.prompt_optimize_agent_model or ""),
    }


_BOOL_KEYS = ("enabled", "agent_enabled")


def load_config() -> dict:
    """管理台配置优先，缺项回落到 ``.env``。读不出来就当没配。"""
    config = _defaults()
    path = config_path()
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            for key in config:
                if key in payload and payload[key] not in (None, ""):
                    config[key] = payload[key]
            # 布尔键要单独处理：``False`` 是**有效值**，不能被上面的「空值即缺项」滤掉
            for key in _BOOL_KEYS:
                if isinstance(payload.get(key), bool):
                    config[key] = payload[key]
    return config


def save_config(
    *,
    enabled: bool,
    script: str,
    args: str,
    api_base_url: str,
    api_key: str,
    model: str,
    timeout: int,
    agent_enabled: bool = False,
    agent_model: str = "",
) -> None:
    """原子写入。文件 0600 —— 里面有 LLM API Key。"""
    payload = {
        "enabled": bool(enabled),
        "script": script.strip(),
        "args": (args.strip() or "--prompt-file {prompt_file}"),
        "api_base_url": api_base_url.strip(),
        "api_key": api_key.strip(),
        "model": model.strip(),
        "timeout": max(int(timeout), 1),
        "agent_enabled": bool(agent_enabled),
        "agent_model": agent_model.strip(),
    }
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def public_config() -> dict:
    """给管理台回显用：脚本路径与模型照原样给，API Key 只回显「已配置」与否。"""
    config = load_config()
    return {
        "enabled": config["enabled"],
        "script": config["script"],
        "args": config["args"],
        "api_base_url": config["api_base_url"],
        "model": config["model"],
        "timeout": config["timeout"],
        "has_api_key": bool(config["api_key"]),
        "agent_enabled": config["agent_enabled"],
        "agent_model": config["agent_model"],
    }


def script_path(config: dict | None = None) -> str:
    resolved = config if config is not None else load_config()
    return str(resolved.get("script") or "").strip()


def is_configured(config: dict | None = None, mode: str = MODE_OPTIMIZE) -> bool:
    """该模式是否可用：**脚本路径非空**，且**这个模式的开关打开**。"""
    resolved = config if config is not None else load_config()
    if not script_path(resolved):
        return False
    # agent 节点（plan）与 agent 模式、反推提示词要的都是「能读图的模型」，共用同一个开关。
    key = "agent_enabled" if mode in (MODE_AGENT, MODE_PLAN, MODE_REVERSE) else "enabled"
    return bool(resolved.get(key))


def model_for(config: dict, mode: str) -> str:
    """要读图的那几种模式，没有单独配模型时复用纯文本那一个。"""
    if mode in (MODE_AGENT, MODE_PLAN, MODE_REVERSE):
        return str(config.get("agent_model") or config.get("model") or "")
    return str(config.get("model") or "")


def args_template(config: dict) -> str:
    return str(config.get("args") or "--prompt-file {prompt_file}")


def optimize_prompt(
    prompt: str,
    *,
    config: dict | None = None,
    mode: str = MODE_OPTIMIZE,
    image_paths: Sequence[str] = (),
    category: str = "",
    work_type: str = "",
) -> tuple[str, str]:
    """调用本地 skill 脚本，返回 ``(发给上游的提示词, 诊断结论)``。"""
    resolved = config if config is not None else load_config()
    resolved_mode = normalize_mode(mode)
    if resolved_mode == MODE_AGENT and not image_paths:
        # 不静默降级成纯文本：用户按 agent 的价格付了钱，就该拿到 agent 的行为。
        raise RuntimeError("Agent 模式需要参考图，但这次任务没有参考图")
    stdout = _invoke_script(
        prompt,
        resolved=resolved,
        mode=resolved_mode,
        image_paths=image_paths,
        params=agent_params(category, work_type),
    )
    return _extract_result(stdout)


def plan_prompt(
    prompt: str,
    *,
    config: dict | None = None,
    image_paths: Sequence[str] = (),
    params: dict | None = None,
    platform_norms_text: str = "",
) -> tuple[dict, str, dict]:
    """跑 **agent 节点**的计划步骤，返回 ``(14 字段, 说明, 用量)``。"""
    resolved = config if config is not None else load_config()
    stdout = _invoke_script(
        prompt,
        resolved=resolved,
        mode=MODE_PLAN,
        image_paths=image_paths,
        params=params or {},
        platform_norms_text=platform_norms_text,
    )
    payload = _parse_payload(stdout)
    if not isinstance(payload, dict):
        raise TypeError("agent 计划没有返回 JSON 对象")
    fields = payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise RuntimeError("agent 计划没有返回 fields")
    note = payload.get("diagnosis") or payload.get("note") or ""
    usage = payload.get("usage")
    return (
        {str(key): ("" if value is None else value) for key, value in fields.items()},
        note if isinstance(note, str) else "",
        usage if isinstance(usage, dict) else {},
    )


#: 反推的 user 消息。
_REVERSE_INSTRUCTION = "Reverse this image into a single generation prompt."


def reverse_prompt(
    *,
    image_paths: Sequence[str],
    config: dict | None = None,
) -> tuple[str, dict]:
    """对一张图**反推**出它的提示词，返回 ``(英文提示词, 用量)``。"""
    if not image_paths:
        raise RuntimeError("反推提示词需要一张图作输入")
    resolved = config if config is not None else load_config()
    stdout = _invoke_script(
        _REVERSE_INSTRUCTION,
        resolved=resolved,
        mode=MODE_REVERSE,
        image_paths=image_paths,
        params={},
    )
    payload = _parse_payload(stdout)
    if isinstance(payload, str):
        candidate, usage = payload, {}
    elif isinstance(payload, dict):
        candidate = ""
        for key in _RESULT_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                candidate = value
                break
        raw_usage = payload.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else {}
    else:
        candidate, usage = "", {}
    if not candidate.strip():
        raise RuntimeError("反推提示词没有返回可用的结果")
    return candidate.strip(), usage


def _invoke_script(
    prompt: str,
    *,
    resolved: dict,
    mode: str,
    image_paths: Sequence[str],
    params: dict,
    platform_norms_text: str = "",
) -> str:
    """跑一次 skill 脚本，返回**原始 stdout**（不裁剪）。"""
    # ⚠️ 延迟导入：providers 在模块层导入本模块（worker 要在派发前调它）
    from image_hub.providers import _run_skill_script

    script = Path(script_path(resolved))
    if not script.is_file():
        raise RuntimeError(f"未找到提示词优化 skill 脚本：{script}")

    handle, temp_name = tempfile.mkstemp(prefix="prompt-optimize-", suffix=".txt")
    prompt_file = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(prompt)
        model = model_for(resolved, mode)
        args = _render_args(
            args_template(resolved), prompt=prompt, prompt_file=str(prompt_file), model=model
        )
        env_overrides = {
            "PROMPT_OPTIMIZE_API_KEY": str(resolved.get("api_key") or ""),
            "PROMPT_OPTIMIZE_API_BASE_URL": str(resolved.get("api_base_url") or ""),
            "PROMPT_OPTIMIZE_MODEL": model,
            "PROMPT_OPTIMIZE_PROMPT_FILE": str(prompt_file),
            "PROMPT_OPTIMIZE_MODE": mode,
            # JSON 数组：路径里的空格/中文交给 JSON 与子进程各管一段，不经过 shell。
            "PROMPT_OPTIMIZE_IMAGES": json.dumps(list(image_paths), ensure_ascii=False),
            # 各条轴（键 + 标签）装在一个 JSON 里，免得为每个参数加一个环境变量。
            "PROMPT_OPTIMIZE_PARAMS": json.dumps(params, ensure_ascii=False),
        }
        if platform_norms_text:
            # 平台规范由服务端从 YAML 渲染过来 —— 规范只该有一份
            env_overrides["PROMPT_OPTIMIZE_PLATFORM_NORMS"] = platform_norms_text
        return _run_skill_script(
            script,
            args,
            env_overrides=env_overrides,
            timeout=max(int(resolved.get("timeout") or 120), 1),
            secret_values=(str(resolved.get("api_key") or ""),),
            script_dir=script,
        )
    finally:
        prompt_file.unlink(missing_ok=True)


def _render_args(template: str, *, prompt: str, prompt_file: str, model: str) -> list[str]:
    """渲染参数模板。用 ``shlex`` 切分，所以带引号的常量参数也能写。"""
    try:
        rendered = template.format(
            prompt_file=shlex.quote(prompt_file),
            prompt=shlex.quote(prompt),
            model=shlex.quote(model),
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise RuntimeError(f"提示词优化参数模板无法渲染：{template}") from exc
    try:
        return shlex.split(rendered, posix=True)
    except ValueError as exc:
        raise RuntimeError(f"提示词优化参数模板引号不匹配：{template}") from exc


#: 诊断结论的候选字段名。agent 模式的输出是「一句提示词 + 一段诊断」。
_NOTE_KEYS = ("diagnosis", "note", "analysis", "reason")


def _parse_payload(stdout: str) -> dict | str:
    """把脚本 stdout 解析成 JSON 负载（原始形状，不裁剪字段）。"""
    from image_hub.providers import _parse_skill_json  # 延迟导入，见 optimize_prompt

    return _parse_skill_json(stdout, "提示词优化脚本没有输出 JSON")


def _extract_result(stdout: str) -> tuple[str, str]:
    """从脚本输出里取 ``(提示词, 诊断结论)``。提示词缺失就明确报错，不猜。"""
    payload = _parse_payload(stdout)
    if isinstance(payload, str):
        candidate, note = payload, ""
    elif isinstance(payload, dict):
        candidate, note = "", ""
        for key in _RESULT_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                candidate = value
                break
        for key in _NOTE_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                note = value
                break
    else:
        candidate, note = "", ""
    if not candidate.strip():
        raise RuntimeError("提示词优化脚本没有返回可用的提示词")
    return candidate.strip(), note.strip()
