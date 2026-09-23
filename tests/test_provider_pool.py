"""平台账号池的路由语义：固定顺序、粘性游标、失效跳过、CAS 推进。

账号池最危险的失败方式不是报错，而是**静默用错号**，所以断言都落在「选中的到底是哪一个
id」上。
"""

import pytest
from sqlalchemy import select

from image_hub import provider_pool, providers
from image_hub.config import settings
from image_hub.db import SessionLocal
from image_hub.models import ProviderAccount, ProviderRoutingCursor, utcnow


@pytest.fixture(autouse=True)
def _empty_pool():
    """池是全局的：每个用例开始前清空，避免共用 test.db 时互相污染。"""
    with SessionLocal() as session:
        for row in session.scalars(select(ProviderAccount)):
            session.delete(row)
        for row in session.scalars(select(ProviderRoutingCursor)):
            session.delete(row)
        session.commit()
    yield


def _add(
    session,
    position: int,
    *,
    provider: str = "libtv",
    enabled: int = 1,
    exhausted: bool = False) -> ProviderAccount:
    account = ProviderAccount(provider=provider, position=position, enabled=enabled)
    if exhausted:
        account.exhausted_at = utcnow()
    session.add(account)
    session.flush()
    return account


def test_empty_pool_falls_back_to_the_default_slot():
    """池里一个号都没有 ⇒ 空串，也就是账号池之前的单账号行为。"""
    with SessionLocal() as session:
        assert providers.select_provider_account(session, provider="libtv") == ""
        # API 通道根本没有账号概念，永远不该被路由到池里。
        assert providers.select_provider_account(session, provider="api") == ""
        assert provider_pool.is_pool_provider("api") is False


def test_fixed_order_picks_the_lowest_position_then_sticks_to_the_cursor():
    """固定顺序取 ``position`` 最小的号；游标一旦指向某号，就不再被顺序拉回去。"""
    with SessionLocal() as session:
        first = _add(session, 0)
        second = _add(session, 1)
        third = _add(session, 2)
        session.commit()

        assert provider_pool.select_account(session, "libtv").id == first.id

        # 粘性：游标指向 3 号时，即使 1 号仍然可用，也必须继续用 3 号。
        provider_pool.set_cursor(session, "libtv", third.id)
        session.commit()
        assert provider_pool.select_account(session, "libtv").id == third.id

        provider_pool.set_cursor(session, "libtv", second.id)
        session.commit()
        assert provider_pool.select_account(session, "libtv").id == second.id


def test_exhausted_and_disabled_accounts_are_skipped():
    with SessionLocal() as session:
        _add(session, 0, exhausted=True)
        _add(session, 1, enabled=0)
        third = _add(session, 2)
        session.commit()
        assert provider_pool.select_account(session, "libtv").id == third.id


def test_advance_moves_to_the_next_position_and_never_wraps_around():
    """推进只往后走；到了最后一个号必须返回 None（回绕会在全耗尽时绕成死循环）。"""
    with SessionLocal() as session:
        first = _add(session, 0)
        second = _add(session, 1)
        session.commit()

        provider_pool.set_cursor(session, "libtv", first.id)
        session.commit()
        moved = provider_pool.advance_account(session, "libtv", first.id)
        assert moved.id == second.id
        session.commit()
        assert provider_pool.cursor_account_id(session, "libtv") == second.id

        assert provider_pool.advance_account(session, "libtv", second.id) is None


def test_advance_is_compare_and_set_so_a_stale_worker_cannot_regress_the_cursor():
    """两个 worker 同时推进：后到者的 CAS 失败，**不许**把游标覆盖回去。"""
    with SessionLocal() as session:
        first = _add(session, 0, exhausted=True)
        second = _add(session, 1)
        third = _add(session, 2)
        session.commit()

        provider_pool.set_cursor(session, "libtv", first.id)
        session.commit()
        assert provider_pool.advance_account(session, "libtv", first.id).id == second.id
        session.commit()

        # 拿着过期视图（还以为游标在 1 号）的第二个 worker：不许把游标推到 3 号。
        stale = provider_pool.advance_account(session, "libtv", first.id)
        session.commit()
        assert provider_pool.cursor_account_id(session, "libtv") == second.id
        assert stale is not None and stale.id == second.id
        assert third.id != provider_pool.cursor_account_id(session, "libtv")


def test_readiness_predicate_skips_accounts_whose_credentials_are_missing():
    """管理员加了一行、还没走完 OAuth 的号不该被选中。

    选中它的结果是「每次生成都失败」，而失败原因（尚未授权）在管理页上看得见、
    与用户看到的「生成失败」完全对不上号。
    """
    with SessionLocal() as session:
        _add(session, 0)
        ready_account = _add(session, 1)
        session.commit()
        chosen = provider_pool.select_account(
            session,
            "libtv",
            ready=lambda account: account.id == ready_account.id)
        assert chosen.id == ready_account.id


def test_all_accounts_unusable_still_returns_one_instead_of_refusing_to_submit():
    """有号但一个都不可用时仍然返回一个号（按固定顺序）。

    执行侧才是权威：它会轮换、会在全耗尽时给出精确失败。在提交期新增一种失败，
    会让用户同时看到两种口径的错误。
    """
    with SessionLocal() as session:
        only = _add(session, 0)
        session.commit()
        assert providers.select_provider_account(session, provider="libtv") == only.id
        assert provider_pool.select_account(
            session, "libtv", ready=lambda account: False
        ) is None


def test_mark_exhausted_is_idempotent_so_audit_only_records_the_first_one():
    """同一个号被并发任务几乎同时判为耗尽时，只有第一次值得记一条事件。"""
    with SessionLocal() as session:
        account = _add(session, 0)
        session.commit()
        assert provider_pool.mark_exhausted(session, account.id, "credits not enough") is True
        session.commit()
        assert provider_pool.mark_exhausted(session, account.id, "credits not enough") is False
        session.commit()

        stored = session.get(ProviderAccount, account.id)
        assert stored.exhausted_at is not None
        assert stored.exhausted_reason == "credits not enough"
        assert stored.last_error_message == "credits not enough"


def test_clear_exhausted_is_manual_only_and_can_be_repeated_safely():
    with SessionLocal() as session:
        account = _add(session, 0, exhausted=True)
        session.commit()
        assert provider_pool.clear_exhausted(session, account.id) is True
        session.commit()
        assert session.get(ProviderAccount, account.id).exhausted_at is None
        # 再清一次没有可清的东西 → False（管理页据此给「本来就是可用」的提示）
        assert provider_pool.clear_exhausted(session, account.id) is False
        # 已经恢复的号立刻重新参与路由。
        assert providers.select_provider_account(session, provider="libtv") == account.id


def test_frozen_execution_identity_is_per_account(monkeypatch):
    """同一个模型走不同的号 = 两条不同的路由。

    账号 id 进了冻结配置 ⇒ ``execution_config_id``（配置哈希）自动按账号区分，
    执行侧也就能按 ``provider_account_id`` 取对应凭据。
    """
    monkeypatch.setattr(providers.libtv_mcp, "authorized", lambda account_id="": True)
    profile = providers.ModelProfile(
        id="libtv:probe", label="探针模型", provider="libtv", upstream_model="probe", enabled=True
    )
    with SessionLocal() as session:
        first = _add(session, 0)
        second = _add(session, 1)
        session.commit()
        frozen_first = providers.freeze_profile_execution(profile, account_id=first.id)
        frozen_second = providers.freeze_profile_execution(profile, account_id=second.id)
        assert frozen_first.execution_config_id
        assert frozen_first.execution_config_id != frozen_second.execution_config_id
        config = providers._load_native_execution_config(
            frozen_first.execution_config_id, "libtv")
        assert config["provider_account_id"] == first.id
        # 同一个号重复冻结是幂等的（同一份配置 → 同一个标识）。
        again = providers.freeze_profile_execution(profile, account_id=first.id)
        assert again.execution_config_id == frozen_first.execution_config_id


def test_model_listing_keeps_the_channel_visible_when_only_the_pool_has_credentials(
    monkeypatch, tmp_path
):
    """凭据在池里、默认槽为空时，平台渠道**不许**从模型列表里消失。

    判定写在默认槽上会让池化部署下整条渠道凭空消失，界面上一句提示都没有。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(
        providers,
        "_libtv_model_rows",
        lambda account_id="": ([("probe", "探针模型", [{"defaults": {}}])], False))
    with SessionLocal() as session:
        pool_account_id = _add(session, 0).id
        session.commit()

    # 只有**池里那个号**有凭据；默认槽（account_id="")没有。
    monkeypatch.setattr(
        providers.libtv_mcp,
        "authorized",
        lambda account_id="": account_id == pool_account_id)

    with SessionLocal() as session:
        assert "libtv:probe" in {
            profile.id for profile in providers.routed_model_profiles(session)
        }
    # 漏传选号（等于按默认槽判定）→ 渠道消失。这正是必须走 routed_* 入口的原因。
    assert "libtv:probe" not in {profile.id for profile in providers.model_profiles()}
