"""同一模型的多 API 路由：**由管理员逐模型人工指定**。"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from image_hub.models import ApiRouteState, utcnow

#: 上游错误原文的留存上限（与账号池同值，便于两边一起从日志里捞样本）。
_ERROR_TEXT_LIMIT = 2000


def route_available(
    session: Session,
    model_id: str,
    route: str,
    *,
    enabled: Iterable[str] | None = None,
) -> str:
    """人工指定的那条路由**此刻**能不能用；不能用返回空串。"""
    name = (route or "").strip()
    if not name:
        return ""
    if enabled is not None and name not in {item for item in enabled if item}:
        return ""
    dead = session.scalar(
        select(ApiRouteState.route).where(
            ApiRouteState.model_id == model_id,
            ApiRouteState.route == name,
            ApiRouteState.exhausted_at.is_not(None),
        )
    )
    return "" if dead else name


def mark_exhausted(session: Session, model_id: str, route: str, reason: str) -> bool:
    """把 ``(模型, 路由)`` 标记为「上游明确报额度不足」。返回是否**首次**标记。"""
    now = utcnow()
    text = (reason or "")[:_ERROR_TEXT_LIMIT]
    existing = session.get(ApiRouteState, (model_id, route))
    first = existing is None or existing.exhausted_at is None
    statement = sqlite_insert(ApiRouteState).values(
        model_id=model_id,
        route=route,
        exhausted_at=now,
        exhausted_reason=text,
        last_error_at=now,
        last_error_message=text,
        updated_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[ApiRouteState.model_id, ApiRouteState.route],
        set_={
            "exhausted_at": now,
            "exhausted_reason": text,
            "last_error_at": now,
            "last_error_message": text,
            "updated_at": now,
        },
    )
    session.execute(statement)
    return first


def record_error(session: Session, model_id: str, route: str, message: str) -> None:
    """记下某条路由上的一次错误原文（**不改**失效状态）。"""
    now = utcnow()
    text = (message or "")[:_ERROR_TEXT_LIMIT]
    statement = sqlite_insert(ApiRouteState).values(
        model_id=model_id,
        route=route,
        last_error_at=now,
        last_error_message=text,
        updated_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[ApiRouteState.model_id, ApiRouteState.route],
        set_={
            "last_error_at": now,
            "last_error_message": text,
            "updated_at": now,
        },
    )
    session.execute(statement)


def clear_exhausted(session: Session, model_id: str, route: str = "") -> int:
    """清除失效标记（管理页手工操作），返回清掉了几个。"""
    statement = update(ApiRouteState).where(
        ApiRouteState.model_id == model_id,
        ApiRouteState.exhausted_at.is_not(None),
    )
    if route:
        statement = statement.where(ApiRouteState.route == route)
    result = session.execute(
        statement.values(exhausted_at=None, exhausted_reason="", updated_at=utcnow())
    )
    return int(result.rowcount or 0)


def route_states(
    session: Session, model_id: str, names: Iterable[str] = ()
) -> list[dict]:
    """诊断/管理页视图：``(模型, 路由)`` 的失效与错误摘要。"""
    rows = {
        item.route: item
        for item in session.scalars(
            select(ApiRouteState).where(ApiRouteState.model_id == model_id)
        )
    }
    declared = [name for name in names if name] or sorted(rows)
    return [
        {
            "route": name,
            "exhausted": bool(rows[name].exhausted_at) if name in rows else False,
            "exhausted_reason": rows[name].exhausted_reason if name in rows else "",
            "last_error_at": rows[name].last_error_at if name in rows else None,
            "last_error_message": rows[name].last_error_message if name in rows else "",
        }
        for name in declared
    ]
