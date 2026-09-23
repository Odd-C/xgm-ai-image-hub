"""协议事实全部来自对 ``mcp.liblib.tv`` 的实测，**改动前先复测**："""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from image_hub.config import settings

MCP_RESOURCE = "https://mcp.liblib.tv/mcp"
SCOPES = (
    "libtv:account:read",
    "libtv:project:read",
    "libtv:project:write",
    "libtv:media:write",
    "libtv:task:write",
    "libtv:export:write",
)
#: 状态文件的存活时间；授权页通常几分钟内完成，超时必须重新发起。
PENDING_TTL_SECONDS = 600
#: access_token 提前这么多秒视为过期，避免"看着没过期实际已失效"的边界。
TOKEN_EXPIRY_SKEW_SECONDS = 60
#: client_secret 到期前这么多天就在管理页标黄提醒。
SECRET_EXPIRY_WARN_DAYS = 14
#: 未走账号池时的默认槽名。
DEFAULT_SLOT = "default"

_ACCOUNT_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_USER_AGENT = "image-hub/1.0"


class LibTVAuthError(RuntimeError):
    """授权流程失败（注册 / 换码 / 刷新）。消息会展示给管理员，别带密钥。"""


class LibTVMCPError(RuntimeError):
    """MCP 调用失败（协议错误 / 上游拒绝）。消息会进入任务错误信息。"""


# ── 存储布局（全部在 storage/private 下，0600 原子写） ────────────────────


def _base_dir() -> os.PathLike[str]:
    return os.path.join(str(settings.storage_root), "private", "libtv-mcp")


def _client_path() -> str:
    return os.path.join(_base_dir(), "client.json")


def _tokens_dir() -> str:
    return os.path.join(_base_dir(), "tokens")


def _tokens_path(account_id: str = "") -> str:
    """令牌落盘位置：``tokens/<account_id>.json``，空账号走 ``default`` 槽。"""
    return os.path.join(_tokens_dir(), f"{_check_account_id(account_id) or DEFAULT_SLOT}.json")


def _pending_path(state: str) -> str:
    return os.path.join(_base_dir(), "pending", f"{state}.json")


def _check_account_id(account_id: str) -> str:
    """⚠️ 空串是**合法值**（= 未走账号池的默认槽），不是「未提供」。"""
    value = str(account_id or "").strip()
    if value and not _ACCOUNT_ID_RE.match(value):
        raise LibTVAuthError("平台账号标识无效")
    return value


def _atomic_write_json(path: str, payload: dict) -> None:
    """与 providers._atomic_write_private_json 同纪律：临时文件 + os.replace。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _delete_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ── HTTP（模块级函数，测试通过 monkeypatch 替换；禁用环境代理） ────────────

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_request(url: str, *, data: bytes | None = None, headers: dict | None = None,
                  timeout: int = 30) -> tuple[int, dict, str]:
    request = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": _USER_AGENT})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, _lower_headers(response.headers), response.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, _lower_headers(exc.headers), exc.read(2_000_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LibTVMCPError(f"LibTV 服务不可达：{exc}") from exc


def _lower_headers(raw) -> dict:
    """响应头统一小写键 —— urllib 保留原始大小写，直接 dict() 会丢。"""
    return {str(key).lower(): str(value) for key, value in raw.items()}


def _http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 30):
    body = json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": _USER_AGENT}
    merged.update(headers or {})
    return _http_request(url, data=body, headers=merged, timeout=timeout)


def _http_post_form(url: str, fields: dict, timeout: int = 30):
    body = urllib.parse.urlencode(fields).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    return _http_request(url, data=body, headers=headers, timeout=timeout)


def _parse_sse_data(raw: str, content_type: str) -> dict:
    """SSE 或纯 JSON 两种响应都收成 dict。"""
    if "text/event-stream" not in content_type:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LibTVMCPError("LibTV 返回了无法解析的内容") from exc
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
    raise LibTVMCPError("LibTV SSE 响应中没有 JSON-RPC 结果")


# ── 部署级 client（动态注册，90 天到期自动重注册） ─────────────────────────


def _register_client() -> dict:
    payload = {
        "client_name": "image-hub",
        "redirect_uris": [settings.libtv_oauth_redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }
    status, _, body = _http_post_json("https://mcp.liblib.tv/register", payload)
    if status != 201:
        raise LibTVAuthError(f"LibTV 客户端注册失败（HTTP {status}）")
    registered = json.loads(body)
    client = {
        "client_id": str(registered["client_id"]),
        "client_secret": str(registered["client_secret"]),
        "client_secret_expires_at": int(registered.get("client_secret_expires_at") or 0),
        "registered_at": int(time.time()),
    }
    _atomic_write_json(_client_path(), client)
    return client


def ensure_client() -> dict:
    """取部署级 client；缺失或 14 天内到期就重新注册（注册是开放且幂等的）。"""
    client = _read_json(_client_path())
    now = int(time.time())
    if client and client.get("client_id") and client.get("client_secret"):
        expires_at = int(client.get("client_secret_expires_at") or 0)
        if not expires_at or expires_at - now > SECRET_EXPIRY_WARN_DAYS * 86400:
            return client
    return _register_client()


# ── 授权流程（发起 → 回跳/粘贴 → 换 token） ───────────────────────────────


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorization_url(account_id: str = "") -> str:
    """生成授权链接：每次授权各自落一份 state 与令牌槽。"""
    account_id = _check_account_id(account_id)
    client = ensure_client()
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    pending = {
        "account_id": account_id,
        "code_verifier": verifier,
        "redirect_uri": settings.libtv_oauth_redirect_uri,
        "created_at": int(time.time()),
    }
    _atomic_write_json(_pending_path(state), pending)
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": settings.libtv_oauth_redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # 实测必需（RFC 8707）；缺了授权服务器直接 invalid_request。
        "resource": MCP_RESOURCE,
    })
    return f"https://mcp.liblib.tv/authorize?{params}"


def _extract_code_and_state(source: str) -> tuple[str, str]:
    """接受完整回调 URL、纯查询串或裸 code= 值。"""
    text = str(source or "").strip()
    if not text:
        raise LibTVAuthError("回调内容为空")
    if "?" in text or "/" in text:
        query = text.split("?", 1)[1] if "?" in text else text
        parsed = urllib.parse.parse_qs(query)
        code = (parsed.get("code") or [""])[0]
        state = (parsed.get("state") or [""])[0]
    else:
        code, state = text, ""
    if not code:
        raise LibTVAuthError("回调内容里没有授权码")
    return code, state


def complete_authorization(callback_source: str, account_id: str = "") -> str:
    """用授权码换 token 并按账号槽落盘；返回确认信息（不含任何密钥）。"""
    account_id = _check_account_id(account_id)
    code, state = _extract_code_and_state(callback_source)
    if state and not _STATE_RE.match(state):
        raise LibTVAuthError("回调 state 无效")
    if not state:
        # 粘贴的内容只有 code 没有 state（少见）：无法校验归属，直接拒绝。
        raise LibTVAuthError("回调内容缺少 state，无法确认授权归属")

    pending = _read_json(_pending_path(state))
    if pending is None:
        raise LibTVAuthError("授权会话不存在或已过期，请重新发起授权")
    # 省略 account_id 时以 pending 里记的槽为准：OAuth 自动回跳带不出这个参数
    if account_id and str(pending.get("account_id") or "") != account_id:
        raise LibTVAuthError("授权会话与目标平台账号不匹配")
    # 落盘用 pending 的槽（磁盘文件同样不该无条件信任，再过一次校验）。
    account_id = _check_account_id(str(pending.get("account_id") or ""))
    if int(pending.get("created_at") or 0) + PENDING_TTL_SECONDS < time.time():
        _delete_file(_pending_path(state))
        raise LibTVAuthError("授权会话已过期，请重新发起授权")
    verifier = str(pending.get("code_verifier") or "")
    redirect_uri = str(pending.get("redirect_uri") or settings.libtv_oauth_redirect_uri)

    client = ensure_client()
    status, _, body = _http_post_form("https://mcp.liblib.tv/token", {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "code_verifier": verifier,
        "resource": MCP_RESOURCE,
    })
    _delete_file(_pending_path(state))
    if status != 200:
        raise LibTVAuthError(f"LibTV 授权码交换失败（HTTP {status}）")
    _store_tokens(json.loads(body), account_id)
    return "LibTV 授权完成"


def _store_tokens(payload: dict, account_id: str = "") -> None:
    """归一化 token 响应后原子落盘；只在成功拿到新值时才覆盖旧文件。"""
    access = str(payload.get("access_token") or "")
    refresh = str(payload.get("refresh_token") or "")
    if not access or not refresh:
        raise LibTVAuthError("LibTV 返回的 token 不完整")
    obtained_at = int(time.time())
    record = {
        "access_token": access,
        "refresh_token": refresh,
        "scope": str(payload.get("scope") or " ".join(SCOPES)),
        "expires_at": obtained_at + int(payload.get("expires_in") or 3600),
        "obtained_at": obtained_at,
    }
    _atomic_write_json(_tokens_path(account_id), record)


# ── token 生命周期（自动刷新 + 旋转式 refresh 的安全落盘） ─────────────────


def load_tokens(account_id: str = "") -> dict | None:
    return _read_json(_tokens_path(account_id))


def authorized(account_id: str = "") -> bool:
    return load_tokens(account_id) is not None


def refresh_tokens(account_id: str = "") -> None:
    """用 refresh_token 换新 token。旋转式：**新值落盘成功后旧值才算作废**。"""
    current = load_tokens(account_id)
    if not current:
        raise LibTVAuthError("LibTV 尚未授权")
    client = ensure_client()
    status, _, body = _http_post_form("https://mcp.liblib.tv/token", {
        "grant_type": "refresh_token",
        "refresh_token": current["refresh_token"],
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "resource": MCP_RESOURCE,
    })
    if status != 200:
        # 刷新失败时保留旧文件：可能只是网络抖动；真失效由下次调用暴露。
        raise LibTVAuthError(f"LibTV 授权刷新失败（HTTP {status}），可能需要重新授权")
    _store_tokens(json.loads(body), account_id)


def access_token(account_id: str = "") -> str:
    """取当前可用 token，临近过期自动刷新（60 秒偏移）。"""
    record = load_tokens(account_id)
    if record is None:
        raise LibTVAuthError("LibTV 尚未授权连接")
    if int(record.get("expires_at") or 0) - TOKEN_EXPIRY_SKEW_SECONDS <= time.time():
        refresh_tokens(account_id)
        record = load_tokens(account_id) or record
    return str(record["access_token"])


def authorization_status(account_id: str = "") -> dict:
    """管理页展示用的授权状态：只读本地文件，不含密钥与路径。"""
    record = load_tokens(account_id)
    if record is None:
        return {"connected": False}
    client = _read_json(_client_path()) or {}
    expires_at = int(client.get("client_secret_expires_at") or 0)
    days_left = max(0, (expires_at - int(time.time())) // 86400) if expires_at else None
    expires_label = ""
    if expires_at:
        expires_label = time.strftime("%Y-%m-%d", time.localtime(expires_at))
    return {
        "connected": True,
        "authorized_at": int(record.get("obtained_at") or 0),
        "secret_expires_at": expires_at or None,
        "secret_expires_label": expires_label,
        "secret_days_left": days_left,
        "secret_expiring_soon": bool(
            expires_at and expires_at - int(time.time()) < SECRET_EXPIRY_WARN_DAYS * 86400
        ),
    }


def disconnect(account_id: str = "") -> None:
    """撤销授权并删除本地 token。撤销失败不阻塞本地清除（best effort）。"""
    account_id = _check_account_id(account_id)
    record = load_tokens(account_id)
    if record:
        client = _read_json(_client_path()) or {}
        if client.get("client_id") and client.get("client_secret"):
            try:
                _http_post_form("https://mcp.liblib.tv/revoke", {
                    "token": record["access_token"],
                    "client_id": client["client_id"],
                    "client_secret": client["client_secret"],
                }, timeout=15)
            except LibTVMCPError:
                pass
    _delete_file(_tokens_path(account_id))


# ── MCP JSON-RPC 调用（401 → 刷新 → 重试一次） ────────────────────────────


def _rpc_raw(token: str, payload: dict, timeout: int = 60):
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "User-Agent": _USER_AGENT,
    }
    return _http_request(
        "https://mcp.liblib.tv/mcp", data=body, headers=headers, timeout=timeout
    )


def _rpc_call(name: str, arguments: dict | None, timeout: int, account_id: str) -> dict:
    account_id = _check_account_id(account_id)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }
    for attempt in (1, 2):
        status, headers, raw = _rpc_raw(access_token(account_id), payload, timeout=timeout)
        if status == 401 and attempt == 1:
            refresh_tokens(account_id)
            continue
        if status != 200:
            raise LibTVMCPError(f"LibTV MCP 调用失败（HTTP {status}）")
        envelope = _parse_sse_data(raw, headers.get("content-type", ""))
        if envelope.get("error"):
            raise LibTVMCPError(f"LibTV MCP 错误：{envelope['error']}")
        return envelope.get("result") or {}
    raise LibTVMCPError("LibTV MCP 调用失败：重试后仍未成功")


def call_tool(
    name: str, arguments: dict | None = None, *, timeout: int = 60, account_id: str = ""
) -> dict:
    """调用一个 MCP 工具，返回结果对象。401 时自动刷新 token 重试一次。"""
    result = _rpc_call(name, arguments, timeout, account_id)
    if result.get("isError"):
        content = result.get("content") or []
        text = next(
            (item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"),
            "",
        )
        raise LibTVMCPError(f"LibTV 工具执行失败：{text[:500]}")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    return result


def call_tool_raw(
    name: str, arguments: dict | None = None, *, timeout: int = 60, account_id: str = ""
) -> dict:
    """返回完整 JSON-RPC result（含 content），供需要文本块的调用方使用。"""
    return _rpc_call(name, arguments, timeout, account_id)


# ── 模型目录（活的：来自 search_models + get_model，全局缓存） ──────────────

CATALOG_TTL_SECONDS = 12 * 3600
CATALOG_FETCH_WORKERS = 5
#: 目录缓存的**解析版本**。解析形状一变就 +1 —— 旧缓存会被当作不存在而重拉。
CATALOG_PARSER_VERSION = 2


def _catalog_path() -> str:
    return os.path.join(_base_dir(), "catalog", "models.json")


def _enum_values(raw: object) -> list[str]:
    """把 schema 里的 enum 归一化成字符串列表。"""
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        value = item.get("value") if isinstance(item, dict) else item
        if value is None or value == "":
            continue
        values.append(str(value))
    return values


def _catalog_entry(detail: dict) -> dict:
    """把 get_model 的 schema 压成我们能用的能力面。"""
    schema = detail.get("schema") or {}
    properties = schema.get("properties") or {}
    ratio = properties.get("ratio") or {}
    resolution = properties.get("resolution") or {}
    quality = properties.get("quality") or {}
    return {
        "key": str(detail.get("modelKey") or detail.get("modelId") or ""),
        "name": str(detail.get("displayName") or detail.get("modelName") or ""),
        "ratios": _enum_values(ratio.get("enum")),
        "resolutions": _enum_values(resolution.get("enum")),
        "qualities": _enum_values(quality.get("enum")),
        "defaults": {
            "ratio": str(ratio.get("default") or ""),
            "resolution": str(resolution.get("default") or ""),
            "quality": str(quality.get("default") or ""),
        },
    }


def fetch_model_catalog(*, account_id: str = "") -> list[dict]:
    """拉一次图像模型目录 + 逐模型能力，写缓存并返回。失败时抛 LibTVMCPError。"""
    listing = call_tool("search_models", {
        "action": "search", "nodeType": "image",
    }, timeout=60, account_id=account_id)
    matches = [item for item in (listing.get("matches") or []) if isinstance(item, dict)]
    keys = [str(item.get("modelKey") or item.get("modelId") or "") for item in matches]
    keys = [key for key in keys if key]
    names = {
        str(item.get("modelKey") or item.get("modelId") or ""): str(
            item.get("displayName") or item.get("modelName") or ""
        )
        for item in matches
    }

    def detail_for(key: str) -> dict:
        try:
            detail = call_tool("get_model", {"model": key}, timeout=45, account_id=account_id)
        except (LibTVMCPError, LibTVAuthError):
            # 单个模型查不到能力不影响整张表：保留条目，能力面留空由上游兜底。
            return {"key": key, "name": names.get(key, key), "ratios": [], "resolutions": [],
                    "qualities": [], "defaults": {}}
        entry = _catalog_entry(detail)
        if not entry["name"]:
            entry["name"] = names.get(key, key)
        return entry

    models: list[dict] = []
    with ThreadPoolExecutor(max_workers=CATALOG_FETCH_WORKERS) as pool:
        for entry in pool.map(detail_for, keys):
            if entry.get("key"):
                models.append(entry)
    _atomic_write_json(_catalog_path(), {
        "parser": CATALOG_PARSER_VERSION,
        "fetched_at": int(time.time()),
        "models": models,
    })
    return models


def _cache_is_usable(cached: dict | None) -> bool:
    """缓存可用性：解析版本要对得上，档位必须是纯字符串。"""
    if not isinstance(cached, dict) or not isinstance(cached.get("models"), list):
        return False
    if not cached["models"]:
        return False
    if cached.get("parser") != CATALOG_PARSER_VERSION:
        return False
    for entry in cached["models"]:
        if not isinstance(entry, dict):
            return False
        for field in ("ratios", "resolutions", "qualities"):
            for value in entry.get(field) or []:
                if not isinstance(value, str) or "{" in value or "}" in value:
                    return False
    return True


def model_catalog(*, allow_fetch: bool = True, account_id: str = "") -> tuple[list[dict], bool]:
    """取模型目录；只有完全没有缓存时才同步拉取一次。"""
    cached = _read_json(_catalog_path())
    if not _cache_is_usable(cached):
        cached = None
    if cached is not None:
        age = int(time.time()) - int(cached.get("fetched_at") or 0)
        return cached["models"], age > CATALOG_TTL_SECONDS
    if not allow_fetch:
        return [], True
    try:
        return fetch_model_catalog(account_id=account_id), False
    except (LibTVMCPError, LibTVAuthError):
        # 拉不到就交给调用方的兜底清单（providers.LIBTV_FALLBACK_MODELS）。
        return [], True
