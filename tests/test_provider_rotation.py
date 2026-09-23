"""平台账号的**额度失效轮换**：什么时候可以换号、什么时候绝对不能换。

两条容易写反、写反就要么赔钱要么卡死任务的规则：

1. **可换号的前提是 ``external_task_id`` 为空** —— 它才是计费身份；写入之后上游可能已在
   计费，重提等于同一张图付两次钱。判据不能用 ``external_project_id``：平台在建完项目
   （免费、幂等）之后才提交，用它当判据会封死最需要轮换的场景。
2. **只有额度类错误才换号**：``unauthorized`` / ``invalid`` 是凭据坏了或参数错了，
   换号没有意义，还会把真问题掩盖成「换个号好像就好了」。
"""

import json
import uuid

import pytest
from sqlalchemy import select

from image_hub import provider_pool, providers
from image_hub.db import SessionLocal
from image_hub.models import Generation, Project, ProviderAccount, ProviderRoutingCursor, User

_EXHAUSTED_ERROR = "LibTV 工具执行失败：credits not enough"


@pytest.fixture(autouse=True)
def _empty_pool(monkeypatch):
    """清空全局账号池，并把池里的号一律视为凭据齐备。

    凭据就绪性另有专门用例覆盖（``tests/test_provider_pool.py``），这里要测的是
    **轮换语义**本身。
    """
    monkeypatch.setattr(providers.libtv_mcp, "authorized", lambda account_id="": True)
    with SessionLocal() as session:
        for row in session.scalars(select(ProviderAccount)):
            session.delete(row)
        for row in session.scalars(select(ProviderRoutingCursor)):
            session.delete(row)
        session.commit()
    yield


def _seed_generation(*, external_task_id: str = "") -> str:
    """造一条真在跑的 LibTV 任务：``external_project_id`` 已写（libtv 的真实形态）。"""
    with SessionLocal() as session:
        user = User(
            username=f"pool-user-{uuid.uuid4().hex[:8]}",
            password_hash="x",
            role="user",
            must_change_password=0)
        session.add(user)
        session.flush()
        project = Project(user_id=user.id, name=f"pool-project-{uuid.uuid4().hex[:8]}")
        session.add(project)
        session.flush()
        generation = Generation(
            user_id=user.id,
            project_id=project.id,
            idempotency_key=f"pool-{uuid.uuid4().hex}",
            original_prompt="额度轮换用例",
            provider="libtv",
            model_id="probe",
            model_label="探针模型",
            status="running",
            # 比例/分辨率/画质在 ``parameters_json`` 里（没有独立列）。
            parameters_json=json.dumps(
                {"ratio": "1:1", "resolution": "2K", "quality": "standard"}
            ),
            external_project_id="libtv-project-1",
            external_task_id=external_task_id)
        session.add(generation)
        session.commit()
        return generation.id


def _add_account(session, position: int) -> ProviderAccount:
    account = ProviderAccount(provider="libtv", position=position, enabled=1)
    session.add(account)
    session.flush()
    return account


def _freeze_onto(session, generation_id: str, account_id: str):
    """把任务冻结到指定账号上，返回执行侧实际用的 profile。

    冻结配置里那个 ``provider_account_id`` 就是「这笔任务在用哪个号」的权威来源，
    轮换从它读起点 —— 所以走真实的冻结链路，而不是手搓一个假快照。
    """
    profile = providers.ModelProfile(
        id="libtv:probe", label="探针模型", provider="libtv", upstream_model="probe", enabled=True
    )
    profile = providers.freeze_profile_execution(profile, account_id=account_id)
    generation = session.get(Generation, generation_id)
    generation.provider_account_id = account_id
    generation.provider_snapshot_json = json.dumps(profile.snapshot_dict(), ensure_ascii=False)
    session.commit()
    return profile


def test_quota_error_rotates_to_the_next_account_and_the_task_survives():
    """额度不足 + 任务尚未被受理 ⇒ 换下一个号重提，并把新号冻结进配置。"""
    generation_id = _seed_generation()
    seen: list[str] = []

    with SessionLocal() as session:
        first = _add_account(session, 0)
        second = _add_account(session, 1)
        session.commit()
        profile = _freeze_onto(session, generation_id, first.id)

        def executor(generation, active_profile):
            seen.append(
                providers._provider_account_id(
                    providers._native_execution_config(active_profile)
                )
            )
            if len(seen) == 1:
                raise RuntimeError(_EXHAUSTED_ERROR)

        rotated = providers._execute_with_account_rotation(
            session, session.get(Generation, generation_id), profile, executor
        )

        # 第一次用 1 号，被拒后换到 2 号重提 —— 而且**真的**换了号。
        assert seen == [first.id, second.id]
        assert providers._provider_account_id(
            providers._native_execution_config(rotated)
        ) == second.id
        assert rotated.execution_config_id != profile.execution_config_id

        # 1 号被标记失效，原文留档。
        session.refresh(first)
        assert first.exhausted_at is not None
        assert first.exhausted_reason == _EXHAUSTED_ERROR
        # 游标推进到 2 号：管理页据此显示「当前走哪个号」。
        assert provider_pool.cursor_account_id(session, "libtv") == second.id
        # 任务留痕与快照都指向新号。
        generation = session.get(Generation, generation_id)
        assert generation.provider_account_id == second.id
        assert json.loads(generation.provider_snapshot_json)["execution_config_id"] == (
            rotated.execution_config_id
        )


def test_no_rotation_once_the_upstream_accepted_the_task():
    """``external_task_id`` 已写入 ⇒ **绝不重提**，只标记失效供后续任务路由。"""
    generation_id = _seed_generation(external_task_id="task-accepted")
    calls = 0

    with SessionLocal() as session:
        first = _add_account(session, 0)
        _add_account(session, 1)
        session.commit()
        profile = _freeze_onto(session, generation_id, first.id)
        generation = session.get(Generation, generation_id)

        def executor(active_generation, active_profile):
            nonlocal calls
            calls += 1
            raise RuntimeError(_EXHAUSTED_ERROR)

        with pytest.raises(RuntimeError) as caught:
            providers._execute_with_account_rotation(session, generation, profile, executor)

        assert calls == 1, "已受理的任务不许换号重提"
        # 抛出的是**原错误**（交给既有 recovery_required 收敛），不是「全部耗尽」。
        assert not isinstance(caught.value, providers.ProviderAccountsExhaustedError)
        session.refresh(first)
        assert first.exhausted_at is not None


def test_non_quota_errors_never_rotate_and_still_leave_the_raw_text():
    """``unauthorized`` 之类的错误换号没有意义 —— 不换号，但原文要留档。"""
    generation_id = _seed_generation()
    calls = 0
    error = "LibTV 工具执行失败：unauthorized: invalid access token"

    with SessionLocal() as session:
        first = _add_account(session, 0)
        second = _add_account(session, 1)
        session.commit()
        profile = _freeze_onto(session, generation_id, first.id)

        def executor(generation, active_profile):
            nonlocal calls
            calls += 1
            raise RuntimeError(error)

        with pytest.raises(RuntimeError):
            providers._execute_with_account_rotation(
                session, session.get(Generation, generation_id), profile, executor
            )

        assert calls == 1
        session.refresh(first)
        session.refresh(second)
        assert first.exhausted_at is None, "凭据/参数错误不该把号判死"
        assert second.exhausted_at is None
        # 没命中额度词表的原文同样要留：校准词表只能靠没被它命中的真实错误。
        assert first.last_error_message == error
        assert provider_pool.cursor_account_id(session, "libtv") == ""


def test_upstream_pending_is_not_treated_as_an_error_or_a_quota_signal():
    """「上游还在生成」既不是错误、更不是额度不足：不记错误、不换号。"""
    generation_id = _seed_generation()
    calls = 0

    with SessionLocal() as session:
        first = _add_account(session, 0)
        _add_account(session, 1)
        session.commit()
        profile = _freeze_onto(session, generation_id, first.id)

        def executor(generation, active_profile):
            nonlocal calls
            calls += 1
            raise providers.UpstreamPendingError("上游仍在生成")

        with pytest.raises(providers.UpstreamPendingError):
            providers._execute_with_account_rotation(
                session, session.get(Generation, generation_id), profile, executor
            )

        assert calls == 1
        session.refresh(first)
        assert first.exhausted_at is None
        assert first.last_error_message == ""


def test_all_accounts_exhausted_fails_the_task(monkeypatch):
    """所有号都耗尽 ⇒ ``failed``，而不是卡死的人工恢复。

    平台在提交被拒之前就建过项目，按「写过 project_id 就当可能已受理」会落
    ``recovery_required``，而 ``recover_generation`` 又因为缺 ``task_id`` 直接报错 ——
    任务既跑不完、也退不了款。
    """
    generation_id = _seed_generation()

    monkeypatch.setattr(
        providers,
        "_execute_libtv",
        lambda generation, profile: (_ for _ in ()).throw(RuntimeError(_EXHAUSTED_ERROR)))

    with SessionLocal() as session:
        only = _add_account(session, 0)
        session.commit()
        only_id = only.id
        _freeze_onto(session, generation_id, only_id)

    providers.execute_generation(generation_id)

    with SessionLocal() as session:
        generation = session.get(Generation, generation_id)
        assert generation.status == "failed"
        assert "额度都不可用" in generation.error_message
        assert generation.finished_at is not None
        account = session.get(ProviderAccount, only_id)
        assert account.exhausted_at is not None
        assert account.exhausted_reason == _EXHAUSTED_ERROR
