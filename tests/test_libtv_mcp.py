"""Tests for the LibTV Remote MCP OAuth client (all network calls are mocked)."""

import json
import time

import pytest

from image_hub import libtv_mcp
from image_hub.config import settings


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(
        settings, "libtv_oauth_redirect_uri", "http://localhost:8000/admin/providers/libtv/callback"
    )


def _fake_client(monkeypatch):
    monkeypatch.setattr(
        libtv_mcp,
        "_http_post_json",
        lambda url, payload, headers=None, timeout=30: (
            201,
            {},
            json.dumps({
                "client_id": "client-1",
                "client_secret": "secret-1",
                "client_secret_expires_at": int(time.time()) + 90 * 86400,
            })))


def test_authorization_url_carries_resource_and_state(monkeypatch):
    """授权请求必须带 RFC 8707 resource 参数，且 state 落盘时记下目标账号槽。"""
    _fake_client(monkeypatch)
    url = libtv_mcp.build_authorization_url()
    assert "resource=https%3A%2F%2Fmcp.liblib.tv%2Fmcp" in url
    assert "code_challenge_method=S256" in url
    assert "state=" in url
    state = url.split("state=")[1].split("&")[0]
    pending = libtv_mcp._read_json(libtv_mcp._pending_path(state))
    assert pending["account_id"] == ""
    assert pending["code_verifier"]


def test_complete_authorization_exchanges_code_and_stores_tokens(monkeypatch):
    _fake_client(monkeypatch)
    url = libtv_mcp.build_authorization_url()
    state = url.split("state=")[1].split("&")[0]

    captured = {}

    def fake_token_post(url, fields, timeout=30):
        captured.update(fields)
        return 200, {}, json.dumps({
            "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600,
        })

    monkeypatch.setattr(libtv_mcp, "_http_post_form", fake_token_post)
    message = libtv_mcp.complete_authorization( f"http://localhost:8000/admin/providers/libtv/callback?code=abc&state={state}"
    )
    assert "授权完成" in message
    assert captured["code"] == "abc"
    assert captured["resource"] == "https://mcp.liblib.tv/mcp"
    assert captured["code_verifier"]

    tokens = libtv_mcp.load_tokens()
    assert tokens["access_token"] == "at-1"
    assert tokens["expires_at"] - tokens["obtained_at"] == 3600
    assert libtv_mcp.authorized() is True
    # 一次性 state 用完即删。
    assert libtv_mcp._read_json(libtv_mcp._pending_path(state)) is None


def test_complete_authorization_rejects_a_foreign_state(monkeypatch):
    _fake_client(monkeypatch)
    url = libtv_mcp.build_authorization_url()
    state = url.split("state=")[1].split("&")[0]
    with pytest.raises(libtv_mcp.LibTVAuthError):
        libtv_mcp.complete_authorization(
            f"http://localhost:8000/admin/providers/libtv/callback?code=abc&state={state}")


def test_complete_authorization_rejects_expired_pending_state(monkeypatch):
    _fake_client(monkeypatch)
    url = libtv_mcp.build_authorization_url()
    state = url.split("state=")[1].split("&")[0]
    pending = libtv_mcp._read_json(libtv_mcp._pending_path(state))
    pending["created_at"] = int(time.time()) - libtv_mcp.PENDING_TTL_SECONDS - 10
    libtv_mcp._atomic_write_json(libtv_mcp._pending_path(state), pending)
    with pytest.raises(libtv_mcp.LibTVAuthError):
        libtv_mcp.complete_authorization(
            f"http://localhost:8000/admin/providers/libtv/callback?code=abc&state={state}")


def test_refresh_rotates_and_persists_only_on_success(monkeypatch):
    libtv_mcp._store_tokens( {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600})
    _fake_client(monkeypatch)

    def failing_refresh(url, fields, timeout=30):
        assert fields["refresh_token"] == "rt-1"
        return 400, {}, json.dumps({"error": "invalid_grant"})

    monkeypatch.setattr(libtv_mcp, "_http_post_form", failing_refresh)
    with pytest.raises(libtv_mcp.LibTVAuthError):
        libtv_mcp.refresh_tokens()
    # 刷新失败时旧 token 必须原样保留（旋转式 token 的安全底线）。
    assert libtv_mcp.load_tokens()["refresh_token"] == "rt-1"

    def working_refresh(url, fields, timeout=30):
        return 200, {}, json.dumps({
            "access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600,
        })

    monkeypatch.setattr(libtv_mcp, "_http_post_form", working_refresh)
    libtv_mcp.refresh_tokens()
    tokens = libtv_mcp.load_tokens()
    assert tokens["access_token"] == "at-2"
    assert tokens["refresh_token"] == "rt-2"


def test_call_tool_refreshes_once_on_401_then_retries(monkeypatch):
    libtv_mcp._store_tokens( {"access_token": "stale", "refresh_token": "rt-1", "expires_in": 99999})
    _fake_client(monkeypatch)
    refresh_calls = []

    def fake_refresh(account_id=""):
        refresh_calls.append(account_id)
        libtv_mcp._store_tokens( {"access_token": "fresh", "refresh_token": "rt-2", "expires_in": 99999})

    monkeypatch.setattr(libtv_mcp, "refresh_tokens", fake_refresh)

    responses = [
        (401, {"content-type": "application/json"}, "{}"),
        (
            200,
            {"content-type": "text/event-stream"},
            'data:' + json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "result": {"structuredContent": {"ok": True}},
            })),
    ]

    def fake_rpc(token, payload, timeout=60):
        assert token == ("stale" if len(responses) == 2 else "fresh")
        return responses.pop(0)

    monkeypatch.setattr(libtv_mcp, "_rpc_raw", fake_rpc)
    assert libtv_mcp.call_tool( "doctor") == {"ok": True}
    assert refresh_calls == [""]


def test_call_tool_surfaces_tool_errors_without_secrets(monkeypatch):
    libtv_mcp._store_tokens( {"access_token": "at", "refresh_token": "rt", "expires_in": 99999})
    monkeypatch.setattr(libtv_mcp, "_rpc_raw", lambda token, payload, timeout=60: (
        200,
        {"content-type": "text/event-stream"},
        'data:' + json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "result": {"isError": True, "content": [{"type": "text", "text": "PROJECT_REQUIRED: boom"}]},
        })))
    with pytest.raises(libtv_mcp.LibTVMCPError) as excinfo:
        libtv_mcp.call_tool( "run_agent", {})
    assert "PROJECT_REQUIRED" in str(excinfo.value)


def test_status_contains_no_secrets_and_reports_expiry(monkeypatch):
    libtv_mcp._store_tokens( {"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    libtv_mcp._atomic_write_json(libtv_mcp._client_path(), {
        "client_id": "c", "client_secret": "s",
        "client_secret_expires_at": int(time.time()) + 10 * 86400,
        "registered_at": 0,
    })
    status = libtv_mcp.authorization_status()
    assert status["connected"] is True
    assert status["secret_expiring_soon"] is True
    assert status["secret_days_left"] <= 14
    # token 本体（key 或值）都不能出现；"at"/"rt" 是字面 token 值，必须带引号匹配。
    text = json.dumps(status)
    assert "access_token" not in text and "refresh_token" not in text
    assert '"at"' not in text and '"rt"' not in text
    assert '"s"' not in text


def test_unconfigured_slot_has_no_tokens_and_invalid_ids_are_refused():
    assert libtv_mcp.load_tokens() is None
    with pytest.raises(libtv_mcp.LibTVAuthError):
        libtv_mcp.load_tokens("not-hex")
    with pytest.raises(libtv_mcp.LibTVAuthError):
        libtv_mcp.access_token("../../evil")


def test_pool_account_tokens_are_isolated_from_each_other_and_from_the_default_slot():
    """多个平台账号各存各的令牌，**互不覆盖**，也不覆盖默认槽。

    这是账号池的地基：两个号若共用一个令牌文件，「切到下一个号」就永远切不动 ——
    而且是在**用错账号出图**这种最坏的方式上失败，而不是报错。
    """
    first, second = "c" * 32, "d" * 32
    libtv_mcp._store_tokens( {"access_token": "default-at", "refresh_token": "d-rt", "expires_in": 3600}
    )
    libtv_mcp._store_tokens(
        {"access_token": "first-at", "refresh_token": "f-rt", "expires_in": 3600},
        first)
    libtv_mcp._store_tokens(
        {"access_token": "second-at", "refresh_token": "s-rt", "expires_in": 3600},
        second)

    assert libtv_mcp.access_token() == "default-at"
    assert libtv_mcp.access_token( first) == "first-at"
    assert libtv_mcp.access_token( second) == "second-at"

    libtv_mcp.disconnect( first)
    assert libtv_mcp.authorized( first) is False
    # 删一个号既不牵连池里的其它号，也不牵连默认槽。
    assert libtv_mcp.authorized( second) is True
    assert libtv_mcp.authorized() is True


def test_pool_account_ids_also_refuse_path_traversal():
    """账号 id 会直接拼进文件名，所以必须防穿越。"""
    with pytest.raises(libtv_mcp.LibTVAuthError):
        libtv_mcp.load_tokens( "../../evil")
    with pytest.raises(libtv_mcp.LibTVAuthError):
        libtv_mcp.access_token( "not-hex")


def test_authorization_callback_must_match_the_target_account_slot(monkeypatch):
    """回调把令牌挂到哪个槽，必须与**发起授权时**的槽一致。

    池里可以有多个号，少了这道校验，第二个号的授权会**静默覆盖**第一个号的令牌，
    而用户看到的是「授权成功」。
    """
    _fake_client(monkeypatch)
    first, second = "c" * 32, "d" * 32
    url = libtv_mcp.build_authorization_url( first)
    state = url.split("state=")[1].split("&")[0]
    assert libtv_mcp._read_json(libtv_mcp._pending_path(state))["account_id"] == first

    monkeypatch.setattr(
        libtv_mcp,
        "_http_post_form",
        lambda url, fields, timeout=30: (
            200,
            {},
            json.dumps({"access_token": "at", "refresh_token": "rt", "expires_in": 3600})))

    # 挂到别的槽 → 拒绝，且**不消耗**这次授权会话（pending 留着，槽对上还能完成）。
    with pytest.raises(libtv_mcp.LibTVAuthError):
        libtv_mcp.complete_authorization(
            f"http://localhost:8000/admin/providers/libtv/callback?code=abc&state={state}",
            second)

    libtv_mcp.complete_authorization(
        f"http://localhost:8000/admin/providers/libtv/callback?code=abc&state={state}",
        first)
    assert libtv_mcp.authorized( first) is True
    assert libtv_mcp.authorized( second) is False


def test_catalog_normalizes_object_enums(tmp_path, monkeypatch):
    """上游同一份 schema 里混着字符串数组与对象数组 —— 对象必须先取 value。

    Regression（2026-09-21 用户实测）：``resolution`` 的 enum 是
    ``{"value": "1K", "displayName": "1K"}`` 对象数组，直接 str() 会把字典字面量
    渲染到前端，界面上就是乱码。
    """
    detail = {
        "modelKey": "lib-image-2.5-s",
        "displayName": "Lib Image 2.5 Pro",
        "schema": {
            "properties": {
                "ratio": {"enum": ["1:1", "16:9"], "default": "16:9"},
                "resolution": {
                    "enum": [
                        {"value": "1K", "displayName": "1K"},
                        {"value": "2K", "displayName": "2K"},
                        {"value": "4K", "displayName": "4K"},
                    ],
                    "default": "2K",
                },
                "quality": {
                    "enum": [
                        {"value": "low", "displayName": "低画质"},
                        {"value": "medium", "displayName": "标准画质"},
                    ],
                    "default": "medium",
                },
            }
        },
    }
    entry = libtv_mcp._catalog_entry(detail)
    assert entry["ratios"] == ["1:1", "16:9"]
    assert entry["resolutions"] == ["1K", "2K", "4K"]
    assert entry["qualities"] == ["low", "medium"]
    assert entry["defaults"]["resolution"] == "2K"
    # 任何一项都不能是字典字面量。
    assert not any("{" in value for value in entry["resolutions"] + entry["qualities"])


# ── 模型目录（活的：search_models + get_model，按租户缓存） ────────────────


def _model_detail(key: str, name: str) -> dict:
    return {
        "modelKey": key,
        "displayName": name,
        "schema": {
            "properties": {
                "ratio": {"enum": ["1:1", "16:9"], "default": "16:9"},
                "resolution": {"enum": ["1K", "2K"], "default": "2K"},
                "quality": {
                    "enum": [
                        {"value": "low", "displayName": "低"},
                        {"value": "high", "displayName": "高"},
                    ],
                    "default": "high",
                },
            }
        },
    }


def test_catalog_is_fetched_once_then_served_from_cache(monkeypatch):
    calls = []

    def fake_call_tool( name, arguments=None, *, timeout=60, account_id=""):
        calls.append(name)
        if name == "search_models":
            return {"matches": [
                {"modelKey": "z-image", "displayName": "Z Image Turbo"},
                {"modelKey": "mj-v7", "displayName": "Midjourney V7"},
            ]}
        if name == "get_model":
            key = arguments["model"]
            return _model_detail(key, f"{key} name")
        raise AssertionError(name)

    monkeypatch.setattr(libtv_mcp, "call_tool", fake_call_tool)

    models, stale = libtv_mcp.model_catalog()
    assert stale is False
    assert [item["key"] for item in models] == ["z-image", "mj-v7"]
    entry = models[0]
    assert entry["ratios"] == ["1:1", "16:9"]
    assert entry["resolutions"] == ["1K", "2K"]
    assert entry["qualities"] == ["low", "high"]
    assert entry["defaults"] == {"ratio": "16:9", "resolution": "2K", "quality": "high"}
    assert calls.count("search_models") == 1 and calls.count("get_model") == 2

    # 第二次读缓存：不再发请求。
    again, stale_again = libtv_mcp.model_catalog()
    assert stale_again is False
    assert [item["key"] for item in again] == ["z-image", "mj-v7"]
    assert calls.count("search_models") == 1


def test_catalog_marks_stale_cache_and_survives_a_failed_refresh(monkeypatch):
    def fake_call_tool( name, arguments=None, *, timeout=60, account_id=""):
        if name == "search_models":
            return {"matches": [{"modelKey": "z-image", "displayName": "Z Image Turbo"}]}
        if name == "get_model":
            return _model_detail(arguments["model"], "Z Image Turbo")
        raise AssertionError(name)

    monkeypatch.setattr(libtv_mcp, "call_tool", fake_call_tool)
    models, _ = libtv_mcp.model_catalog()
    assert models

    # 把缓存时间改老 → 报过期，但仍然可用。
    path = libtv_mcp._catalog_path()
    payload = libtv_mcp._read_json(path)
    payload["fetched_at"] = int(time.time()) - libtv_mcp.CATALOG_TTL_SECONDS - 60
    libtv_mcp._atomic_write_json(path, payload)
    stale_models, stale = libtv_mcp.model_catalog()
    assert stale is True
    assert stale_models  # 过期不等于不可用

    # 刷新失败 → 继续用旧缓存，不能让页面挂掉。
    def boom(*args, **kwargs):
        raise libtv_mcp.LibTVMCPError("upstream down")

    monkeypatch.setattr(libtv_mcp, "call_tool", boom)
    fallback_models, still_stale = libtv_mcp.model_catalog( allow_fetch=False)
    assert fallback_models and still_stale is True


def test_catalog_ignores_a_cache_written_by_an_older_parser(monkeypatch, tmp_path):
    """解析逻辑修好后，旧缓存必须失效 —— 否则界面继续吃修复前的乱码。

    Regression（2026-09-21 用户实测「分辨率还是乱码」）：`_enum_values` 修好了，
    但 12 小时 TTL 的缓存里还存着 `str(dict)` 写进去的
    `"{'value': '1K', 'displayName': '1K'}"`，于是刷新页面仍是乱码。
    """
    # (a) 老缓存没有 parser 字段，且档位是字典字面量 —— 必须被忽略
    libtv_mcp._atomic_write_json(libtv_mcp._catalog_path(), {
        "fetched_at": int(time.time()),
        "models": [{"key": "z-image", "name": "Z Image Turbo",
                    "ratios": ["1:1"], "resolutions": ["{'value': '1K', 'displayName': '1K'}"],
                    "qualities": [], "defaults": {}}],
    })
    models, stale = libtv_mcp.model_catalog( allow_fetch=False)
    assert models == [] and stale is True

    # (b) 版本对、内容干净 —— 正常读缓存
    libtv_mcp._atomic_write_json(libtv_mcp._catalog_path(), {
        "parser": libtv_mcp.CATALOG_PARSER_VERSION,
        "fetched_at": int(time.time()),
        "models": [{"key": "z-image", "name": "Z Image Turbo",
                    "ratios": ["1:1"], "resolutions": ["1K"], "qualities": [], "defaults": {}}],
    })
    models, stale = libtv_mcp.model_catalog( allow_fetch=False)
    assert [item["key"] for item in models] == ["z-image"] and stale is False

    # (c) 版本号落后 —— 同样失效
    payload = libtv_mcp._read_json(libtv_mcp._catalog_path())
    payload["parser"] = libtv_mcp.CATALOG_PARSER_VERSION - 1
    libtv_mcp._atomic_write_json(libtv_mcp._catalog_path(), payload)
    models, stale = libtv_mcp.model_catalog( allow_fetch=False)
    assert models == [] and stale is True


def test_fetched_catalog_stamps_the_parser_version(monkeypatch):
    def fake_call_tool( name, arguments=None, *, timeout=60, account_id=""):
        if name == "search_models":
            return {"matches": [{"modelKey": "z-image", "displayName": "Z Image Turbo"}]}
        if name == "get_model":
            return _model_detail(arguments["model"], "Z Image Turbo")
        raise AssertionError(name)

    monkeypatch.setattr(libtv_mcp, "call_tool", fake_call_tool)
    libtv_mcp.fetch_model_catalog()
    cached = libtv_mcp._read_json(libtv_mcp._catalog_path())
    assert cached["parser"] == libtv_mcp.CATALOG_PARSER_VERSION


def test_catalog_returns_empty_when_nothing_is_cached_and_fetch_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise libtv_mcp.LibTVMCPError("upstream down")

    monkeypatch.setattr(libtv_mcp, "call_tool", boom)
    models, stale = libtv_mcp.model_catalog()
    assert models == [] and stale is True
