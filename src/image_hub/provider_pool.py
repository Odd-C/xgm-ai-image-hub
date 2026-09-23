"""平台账号池：固定顺序路由、粘性游标、响应式失效轮换。"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from image_hub.models import (
    PROVIDER_POOL_PROVIDERS,
    ProviderAccount,
    ProviderRoutingCursor,
    utcnow,
)

#: 「这个号此刻可用吗」的调用方注入谓词（凭据齐备性等）。
ReadyPredicate = Callable[[ProviderAccount], bool]

#: 参与池路由的渠道。转出 ``models`` 的定义，免得调用方为此再 import 一遍 ORM 模型。
POOL_PROVIDERS = PROVIDER_POOL_PROVIDERS

#: 上游错误原文的留存上限（用于校准额度错误识别词表）。
_ERROR_TEXT_LIMIT = 2000


def is_pool_provider(provider: str) -> bool:
    """只有平台渠道有账号池。``api`` 通道按 ``base_url`` 单一路由，没有账号概念。"""
    return provider in PROVIDER_POOL_PROVIDERS


def pool_accounts(session: Session, provider: str) -> list[ProviderAccount]:
    """按 position 升序取池里的号。"""
    return list(
        session.scalars(
            select(ProviderAccount)
            .where(ProviderAccount.provider == provider)
            .order_by(ProviderAccount.position, ProviderAccount.id)
        )
    )


def candidate_accounts(
    session: Session,
    provider: str,
    *,
    exclude_ids: Iterable[str] = (),
    ready: ReadyPredicate | None = None,
) -> list[ProviderAccount]:
    """按「启用 → 未失效 → 凭据齐备 → 顺序」筛出候选号。"""
    excluded = {item for item in exclude_ids if item}
    candidates: list[ProviderAccount] = []
    for account in pool_accounts(session, provider):
        if account.id in excluded or not account.enabled:
            continue
        # 非空 = 已被上游判为额度用尽。没有自动恢复，只能由管理员手工清除。
        if account.exhausted_at is not None:
            continue
        if ready is not None and not ready(account):
            continue
        candidates.append(account)
    return candidates


def cursor_account_id(session: Session, provider: str) -> str:
    """游标当前指向的号；没有游标行时返回空串。"""
    return str(
        session.scalar(
            select(ProviderRoutingCursor.current_account_id).where(
                ProviderRoutingCursor.provider == provider
            )
        )
        or ""
    )


def select_account(
    session: Session,
    provider: str,
    *,
    exclude_ids: Iterable[str] = (),
    ready: ReadyPredicate | None = None,
) -> ProviderAccount | None:
    """按粘性游标选号：优先用当前号，失效时才落到顺序里下一个可用号。"""
    candidates = candidate_accounts(session, provider, exclude_ids=exclude_ids, ready=ready)
    if not candidates:
        return None
    current = cursor_account_id(session, provider)
    if current:
        for account in candidates:
            if account.id == current:
                return account
    return candidates[0]


def advance_account(
    session: Session,
    provider: str,
    from_account_id: str,
    *,
    exclude_ids: Iterable[str] = (),
    ready: ReadyPredicate | None = None,
) -> ProviderAccount | None:
    """把游标从 ``from_account_id`` 推进到固定顺序里的**下一个**可用号。"""
    from_account = next(
        (item for item in pool_accounts(session, provider) if item.id == from_account_id),
        None,
    )
    candidates = candidate_accounts(session, provider, exclude_ids=exclude_ids, ready=ready)
    if from_account is None:
        # 传进来的号已经不在池里（被删了）：交给固定顺序决定。
        target = candidates[0] if candidates else None
    else:
        target = next(
            (item for item in candidates if item.position > from_account.position), None
        )
    if target is None:
        return None
    if _cas_cursor(session, provider, from_account_id, target.id):
        return target
    # CAS 失败 = 别人已经推进过游标。采用它指向的号，别把游标拽回来。
    return select_account(session, provider, exclude_ids=exclude_ids, ready=ready)


def _cas_cursor(session: Session, provider: str, expected_current: str, new_current: str) -> bool:
    """用 UPSERT 推进游标：当前值不匹配就不改，避免并发覆盖。"""
    statement = sqlite_insert(ProviderRoutingCursor).values(
        provider=provider,
        current_account_id=new_current,
        updated_at=utcnow(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[ProviderRoutingCursor.provider],
        set_={"current_account_id": new_current, "updated_at": utcnow()},
        where=ProviderRoutingCursor.current_account_id == expected_current,
    )
    return session.execute(statement).rowcount == 1


def set_cursor(session: Session, provider: str, account_id: str) -> None:
    """无条件把游标指向某个号（管理页手工切换用）。"""
    _cas_cursor(session, provider, cursor_account_id(session, provider), account_id)


def mark_exhausted(session: Session, account_id: str, reason: str) -> bool:
    """标记额度耗尽；只有第一次落标记时返回 True。"""
    account = session.get(ProviderAccount, account_id)
    if account is None:
        return False
    first = account.exhausted_at is None
    account.exhausted_at = utcnow()
    account.exhausted_reason = reason[:_ERROR_TEXT_LIMIT]
    account.last_error_at = utcnow()
    account.last_error_message = reason[:_ERROR_TEXT_LIMIT]
    return first


def record_error(session: Session, account_id: str, message: str) -> None:
    """记下某号上的一次错误原文（不改失效状态）—— 校准额度错误识别词表的依据。"""
    account = session.get(ProviderAccount, account_id)
    if account is None:
        return
    account.last_error_at = utcnow()
    account.last_error_message = message[:_ERROR_TEXT_LIMIT]


def clear_exhausted(session: Session, account_id: str) -> bool:
    """清除失效标记（管理页手工操作）。返回是否确实清掉了一个标记。"""
    account = session.get(ProviderAccount, account_id)
    if account is None or account.exhausted_at is None:
        return False
    account.exhausted_at = None
    account.exhausted_reason = ""
    return True
