"""同一模型的**多 API 路由**：走哪条由管理员人工选定，系统不换路、只记账。

2026-09-22 拍板「先人工路由」，本文件原来是 ``test_api_route_rotation.py``（测「额度不足就
自动换下一条并重新冻结」）。自动换路已整体撤除，理由写在 ``_execute_api_route`` 的文档里：
代价不是代码量，而是**观测性** —— 「这张图发往了哪条线」不该靠一套推进规则推出来。

现在要守的是三条：

1. **选定即唯一**：模型走哪条 API 全看第 9 字段；选定的那条不可用（停用 / 已标失效）时该
   模型**不出现在用户侧列表里**，绝不回退默认地址（见 ``routed_api_routes`` 的三态约定）。
2. **失败只记账**：上游原文留在 ``(模型, 路由)`` 上；额度/限流/5xx/连不上才额外标「额度
   耗尽」，``unauthorized`` 之类的配置错只记原文 —— 标记**不会自动恢复**，误标要管理员
   手工放行。
3. **被上游受理过的任务绝不重提**：``external_task_id`` 是计费身份，写入之后上游可能已经
   在计费，重提等于同一张图付两次钱。
"""

import json
import uuid

import pytest
from sqlalchemy import delete
from test_provider_config import ROUTE_LIST, _save_routed_api_config, _set_dns

from image_hub import api_routing, providers
from image_hub.config import settings
from image_hub.db import SessionLocal
from image_hub.models import ApiRouteState, Generation, Project, User

#: 命中 ``_EXHAUSTED_MARKERS``（额度不足）⇒ 该标「额度耗尽」。
_QUOTA_ERROR = "上游返回：insufficient balance"
#: 命中 ``_API_ROUTE_AUTH_MARKERS``（凭据坏了）⇒ 只记原文，不标失效。
_AUTH_ERROR = "上游返回：unauthorized"

#: model-a 选了 route-a；model-b 留空（走默认地址）。
_MODELS = (
    "model-a|Model A|1:1|2K|4|true|auto||route-a,"
    "model-b|Model B|1:1|2K|4|true|auto"
)
#: 把 route-a 停用的一份清单（用于「选定的路由被停用」）。
_PAUSED_ROUTE_LIST = (
    "route-a|https://a.example.test/v1|key-a|grsai_native|true|false\n"
    "route-b|https://b.example.test/v1|key-b|openai_compat||true"
)


@pytest.fixture(autouse=True)
def _clean_route_state():
    """清掉失效标记。

    整个测试会话共用一份 ``test.db``，而标记是按 ``(模型, 路由)`` 落库的 —— 模型名在本
    文件里是固定的 ``model-a``，不清就会读到上一条用例留下的标记，断言变成在测别人的残留。
    （配置本身走 ``tmp_path`` 隔离，状态表没有这层隔离。）
    """
    _truncate_route_state()
    yield
    _truncate_route_state()


def _truncate_route_state() -> None:
    with SessionLocal() as session:
        session.execute(delete(ApiRouteState))
        session.commit()


def _prepare(tmp_path, monkeypatch, routes: str = ROUTE_LIST) -> None:
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    _save_routed_api_config(_MODELS, routes)


def _frozen_profile(model_id: str, route: str) -> providers.ModelProfile:
    """走真实冻结链路：``api_route`` 进配置字典 ⇒ 每条路由各有一份冻结文件。"""
    profile = providers.ModelProfile(
        id=f"api:{model_id}",
        label="Model A",
        provider="api",
        upstream_model=model_id,
        enabled=True,
        api_route=route)
    frozen = providers.freeze_profile_execution(profile)
    assert frozen.execution_config_id, "冻结失败说明路由配置没生效"
    return frozen


def _seed_generation(profile: providers.ModelProfile, *, external_task_id: str = "") -> str:
    with SessionLocal() as session:
        user = User(
            username=f"route-user-{uuid.uuid4().hex[:8]}",
            password_hash="x",
            role="user",
            must_change_password=0)
        session.add(user)
        session.flush()
        project = Project(user_id=user.id, name=f"route-project-{uuid.uuid4().hex[:8]}")
        session.add(project)
        session.flush()
        generation = Generation(
            user_id=user.id,
            project_id=project.id,
            idempotency_key=f"route-{uuid.uuid4().hex}",
            original_prompt="人工选路用例",
            provider="api",
            model_id=profile.id,
            model_label=profile.label,
            status="running",
            parameters_json=json.dumps(
                {"ratio": "1:1", "resolution": "2K", "quality": "standard"}
            ),
            provider_snapshot_json=json.dumps(
                profile.snapshot_dict(), ensure_ascii=False
            ),
            external_task_id=external_task_id)
        session.add(generation)
        session.commit()
        return generation.id


def _states(session, model_id: str = "model-a", routes=("route-a",)) -> list[dict]:
    """读路由状态。

    刻意**复用调用方的会话**：出错路径上只 flush 不 commit（落库交给失败收敛那一步，
    见 ``_execute_api_route``），另开一个会话读不到这些改动。
    """
    return api_routing.route_states(session, model_id, routes)


def test_the_selected_route_is_the_one_that_runs(tmp_path, monkeypatch):
    """选定 route-a ⇒ 路由表里 model-a 指向 route-a；没选路由的 model-b 不在这张表里。"""
    _prepare(tmp_path, monkeypatch)
    with SessionLocal() as session:
        routed = providers.routed_api_routes(session)
    assert routed == {"model-a": "route-a"}
    assert "model-b" not in routed, "没选路由 = 走默认地址，不该在选路表里出现"


def _listed(model_id: str = "model-a") -> bool:
    """该 API 模型在用户侧列表里是否可用（``enabled`` 就是列表渲染的开关）。

    ⚠️ 「不摆出来」的实现是 ``enabled=False`` 而**不是**把档案从元组里摘掉，所以断言
    要看这个标志；另外档案 id 是**哈希过的**公开 id，真实模型名不出现在 id 里。
    """
    with SessionLocal() as session:
        profiles = {
            item.id: item for item in providers.routed_model_profiles(session)
        }
    return profiles[f"api:{providers._public_api_model_id(model_id)}"].enabled


def test_a_paused_route_takes_the_model_out_of_the_list(tmp_path, monkeypatch):
    """选定的路由被停用 ⇒ 该模型从用户侧列表消失，且绝不退回默认地址。"""
    _prepare(tmp_path, monkeypatch, _PAUSED_ROUTE_LIST)
    with SessionLocal() as session:
        routed = providers.routed_api_routes(session)
        # 键必须**存在**且值为空串：把「没选」与「不可用」写成一个样子，就会让一个被停
        assert "model-a" in routed
        assert routed["model-a"] == ""
    assert _listed() is False
    # 没选路由的 model-b 不受影响：它走默认地址，而默认地址是好的。
    assert _listed("model-b") is True


def test_a_marked_route_takes_the_model_out_of_the_list(tmp_path, monkeypatch):
    """被标「额度耗尽」同理：先修上游，再决定清标记还是改选另一条。"""
    _prepare(tmp_path, monkeypatch)
    with SessionLocal() as session:
        api_routing.mark_exhausted(session, "model-a", "route-a", _QUOTA_ERROR)
        session.commit()
    with SessionLocal() as session:
        assert providers.routed_api_routes(session) == {"model-a": ""}
    assert _listed() is False
    # 清掉标记（= 管理员确认上游已充值）之后，同一个模型立刻回来。
    with SessionLocal() as session:
        api_routing.clear_exhausted(session, "model-a", "route-a")
        session.commit()
        assert providers.routed_api_routes(session) == {"model-a": "route-a"}
    assert _listed() is True


def test_a_quota_error_marks_the_route_and_keeps_the_raw_text(tmp_path, monkeypatch):
    """额度不足 ⇒ 如实抛出**原错误**（不换路、不重提），并把原文与标记留在状态表。"""
    _prepare(tmp_path, monkeypatch)
    profile = _frozen_profile("model-a", "route-a")
    generation_id = _seed_generation(profile)
    calls: list[str] = []

    with SessionLocal() as session:
        def executor(generation, active_profile):
            calls.append(active_profile.api_route)
            raise RuntimeError(_QUOTA_ERROR)

        with pytest.raises(RuntimeError) as caught:
            providers._execute_api_route(
                session, session.get(Generation, generation_id), profile, executor
            )

        assert calls == ["route-a"], "只执行一次：人工路由下没有下一条可换"
        assert str(caught.value) == _QUOTA_ERROR, "抛原错误，不换成「路由耗尽」这类转述"
        states = _states(session)
        assert states[0]["exhausted"] is True
        # 上游原文原样留档：校准识别词表靠的就是它，而不是我们自己转述的文案。
        assert _QUOTA_ERROR in states[0]["exhausted_reason"]


def test_auth_errors_leave_the_raw_text_without_marking(tmp_path, monkeypatch):
    """``unauthorized`` 只是配置错 ⇒ 记原文，**不**标失效（标记要管理员手工放行）。"""
    _prepare(tmp_path, monkeypatch)
    profile = _frozen_profile("model-a", "route-a")
    generation_id = _seed_generation(profile)

    with SessionLocal() as session:
        def executor(generation, active_profile):
            raise RuntimeError(_AUTH_ERROR)

        with pytest.raises(RuntimeError):
            providers._execute_api_route(
                session, session.get(Generation, generation_id), profile, executor
            )

        states = _states(session)
        assert states[0]["exhausted"] is False
        assert _AUTH_ERROR in states[0]["last_error_message"]


def test_an_accepted_task_is_marked_but_never_resubmitted(tmp_path, monkeypatch):
    """已被上游受理（``external_task_id`` 有值）：只标记，绝不重提。"""
    _prepare(tmp_path, monkeypatch)
    profile = _frozen_profile("model-a", "route-a")
    generation_id = _seed_generation(profile)
    calls: list[str] = []

    with SessionLocal() as session:
        def executor(generation, active_profile):
            calls.append(active_profile.api_route)
            generation.external_task_id = "paid-upstream-task"
            raise RuntimeError(_QUOTA_ERROR)

        with pytest.raises(RuntimeError) as caught:
            providers._execute_api_route(
                session, session.get(Generation, generation_id), profile, executor
            )

        assert calls == ["route-a"], "已受理的任务不许重提"
        # 抛出的必须是**原错误**：这张图可能已经在上游扣了费，不能被判成「从未受理」
        assert not isinstance(caught.value, providers.ProviderAccountsExhaustedError)
        assert _states(session)[0]["exhausted"] is True


def test_pending_upstream_is_not_an_error_and_is_not_recorded(tmp_path, monkeypatch):
    """``UpstreamPendingError`` = 上游还在生成：不记错误、不标记失效（否则白标一条路由）。"""
    _prepare(tmp_path, monkeypatch)
    profile = _frozen_profile("model-a", "route-a")
    generation_id = _seed_generation(profile)

    with SessionLocal() as session:
        def executor(generation, active_profile):
            raise providers.UpstreamPendingError("上游仍在生成")

        with pytest.raises(providers.UpstreamPendingError):
            providers._execute_api_route(
                session, session.get(Generation, generation_id), profile, executor
            )

        states = _states(session)
        assert states[0]["exhausted"] is False
        assert states[0]["last_error_message"] == ""


def test_a_model_without_a_route_is_never_recorded(tmp_path, monkeypatch):
    """走默认地址的模型（第 9 字段留空）没有路由可记：状态表里不该凭空多出一行。"""
    _prepare(tmp_path, monkeypatch)
    profile = _frozen_profile("model-b", "")
    generation_id = _seed_generation(profile)

    with SessionLocal() as session:
        def executor(generation, active_profile):
            raise RuntimeError(_QUOTA_ERROR)

        with pytest.raises(RuntimeError):
            providers._execute_api_route(
                session, session.get(Generation, generation_id), profile, executor
            )

        assert api_routing.route_states(session, "model-b") == []
