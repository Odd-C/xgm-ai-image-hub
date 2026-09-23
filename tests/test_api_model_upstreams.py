"""一个展示名对多个上游模型：平台侧身份与「发给上游的模型名」解绑。

配置串的第 11 字段（``模型标识|显示名|比例|分辨率|参考图|启用|质量|请求头|路由|介绍|上游名候选``，
分号分隔、按优先级）是这个机制的入口。同一个模型在中转平台之间叫法不同是常态，
而用户只是想用「Nano Banana 2」，不该被逼着在列表里认出两个同名不同源的条目。

要守的四条：

1. **旧配置零迁移**：没有第 11 字段时候选就是模型标识它自己，行为与从前逐字节相同。
2. **候选按序取用**：谁的路由还没被标死就用谁；全被标死才算这个模型不可用。
3. **失败只记账在「实际发出去」的那个名字上** —— 挂着平台侧身份会让没出问题的上游替别人背锅。
4. **人工放行要覆盖全部候选**：标记挂在具体名字上，只清一个会让管理员觉得「明明清过了还是
   不可用」。
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from test_api_route_selection import _listed, _seed_generation
from test_provider_config import (
    ROUTED_MODELS,
    _login,
    _run_api_executor_with_fake_client,
    _save_routed_api_config,
    _set_dns,
)

from image_hub import api_routing, management, providers, web
from image_hub.app import app
from image_hub.config import settings
from image_hub.db import SessionLocal
from image_hub.models import ApiRouteState, Generation

#: model-a 的身份是 ``model-a``，但它在这家中转里叫 ``nb2``，另一家里叫 ``nb2-alt``。
_ALIAS_MODELS = "model-a|Model A|1:1|2K|4|true|auto||route-a||nb2;nb2-alt;nb2"
#: 命中 ``_EXHAUSTED_MARKERS``（额度不足）⇒ 该名字会被标「额度耗尽」。
_QUOTA_ERROR = "上游返回：insufficient balance"


@pytest.fixture(autouse=True)
def _clean_route_state():
    """清掉失效标记。

    整个测试会话共用一份 ``test.db``，而标记是按 ``(上游名, 路由)`` 落库的 —— 名字在本文件里
    是固定的 ``nb2`` / ``nb2-alt``，不清就会读到上一条用例留下的标记（配置走 ``tmp_path``
    隔离，状态表没有这层隔离）。
    """
    _truncate_route_state()
    yield
    _truncate_route_state()


def _truncate_route_state() -> None:
    with SessionLocal() as session:
        session.execute(delete(ApiRouteState))
        session.commit()


def _prepare(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    _save_routed_api_config(_ALIAS_MODELS)


def _mark(*names: str, route: str = "route-a") -> None:
    with SessionLocal() as session:
        for name in names:
            api_routing.mark_exhausted(session, name, route, _QUOTA_ERROR)
        session.commit()


def _states(session, name: str, route: str = "route-a") -> list[dict]:
    """读路由状态（**必须传调用方的会话**）。

    出错路径上 ``_execute_api_route`` 只 ``flush`` 不 ``commit``（落库交给失败收敛那一步），
    所以另开一个会话读不到它刚记下的那条 —— 断言会变成「怎么都没记」的假失败。
    """
    return api_routing.route_states(session, name, (route,))


def _profile() -> providers.ModelProfile:
    """按配置里的候选走一次真实冻结链路（候选来自第 11 字段，不手抄）。"""
    profile = providers.ModelProfile(
        id="api:model-a",
        label="Model A",
        provider="api",
        upstream_model="model-a",
        upstreams=providers.api_upstream_candidates("model-a"),
        enabled=True,
        api_route="route-a",
        ratios=("1:1"),
        resolutions=("2K"),
        qualities=("auto"))
    frozen = providers.freeze_profile_execution(profile)
    assert frozen.execution_config_id, "冻结失败说明路由配置没生效"
    return frozen


def _generation(profile: providers.ModelProfile) -> SimpleNamespace:
    return SimpleNamespace(
        id="alias-generation",
        parameters_json=json.dumps(
            {"ratio": "1:1", "resolution": "2K", "quality": "high"}
        ),
        references=[],
        original_prompt="一个红苹果",
        optimized_prompt="")


def test_a_legacy_config_has_no_alias_candidates(tmp_path, monkeypatch):
    """第 11 字段缺席（旧配置）⇒ 候选就是模型标识自己，不引入任何新行为。"""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    _set_dns(monkeypatch, "8.8.8.8")
    _save_routed_api_config(ROUTED_MODELS)
    parsed = providers._parse_api_models(providers._api_config()["models"])
    assert [item["upstreams"] for item in parsed] == [("model-a",), ("model-b",)]
    assert providers.api_upstream_candidates("model-b") == ("model-b",)
    # 唯一入口上的兜底：候选为空必须退回 ``fallback``，不能让模型变成「没有上游可用」。
    assert providers.api_upstream_names((), "model-a") == ("model-a",)


def test_alias_candidates_are_deduped_but_keep_their_order(tmp_path, monkeypatch):
    """候选串去重但**保序**：顺序就是优先级（先用完的先退场）。"""
    _prepare(tmp_path, monkeypatch)
    parsed = providers._parse_api_models(providers._api_config()["models"])
    assert parsed[0]["upstreams"] == ("nb2", "nb2-alt")
    assert providers.api_upstream_candidates("model-a") == ("nb2", "nb2-alt")


def test_a_marked_name_stands_down_alone(tmp_path, monkeypatch):
    """A 家的名字被标死，只让 A 家的名字退场：同一个展示名的另一个叫法照常接力。"""
    _prepare(tmp_path, monkeypatch)
    _mark("nb2")
    with SessionLocal() as session:
        assert providers.pick_api_upstream(session, ("nb2", "nb2-alt"), "route-a") == "nb2-alt"
    # 模型对用户仍然可用 —— 这正是「一个展示名对多个上游」的意义。
    assert _listed() is True

    _mark("nb2-alt")
    with SessionLocal() as session:
        assert providers.pick_api_upstream(session, ("nb2", "nb2-alt"), "route-a") == ""
    assert _listed() is False


def test_execution_sends_the_next_name_and_marks_that_name(tmp_path, monkeypatch):
    """执行时先把「这一笔发给谁」定下来：请求用它，失败记账也挂在它身上。"""
    _prepare(tmp_path, monkeypatch)
    _mark("nb2")
    profile = _profile()
    generation_id = _seed_generation(profile)
    seen: list[str] = []

    with SessionLocal() as session:
        def executor(generation, active_profile):
            seen.append(active_profile.upstream_exec)
            raise RuntimeError(_QUOTA_ERROR)

        with pytest.raises(RuntimeError):
            providers._execute_api_route(
                session, session.get(Generation, generation_id), profile, executor
            )

        assert seen == ["nb2-alt"], "nb2 已被标死，这一笔必须走下一个候选"
        # 记账挂在实际发出去的名字上：挂着平台侧身份会让 nb2 和 nb2-alt 一起被标死。
        assert _states(session, "nb2-alt")[0]["exhausted"] is True
        assert _states(session, "nb2")[0]["exhausted"] is True, "已有的标记不该被改写"


def test_every_name_marked_fails_before_touching_the_upstream(tmp_path, monkeypatch):
    """全部候选都被标死 ⇒ 明确失败，且**不调用上游**（别把图发到一个已知不可用的线上）。"""
    _prepare(tmp_path, monkeypatch)
    _mark("nb2", "nb2-alt")
    profile = _profile()
    generation_id = _seed_generation(profile)
    calls: list[str] = []

    with SessionLocal() as session:
        def executor(generation, active_profile):
            calls.append(active_profile.upstream_exec)

        with pytest.raises(RuntimeError) as caught:
            providers._execute_api_route(
                session, session.get(Generation, generation_id), profile, executor
            )

    assert calls == []
    assert "所有上游" in str(caught.value)


def test_the_request_body_carries_the_selected_upstream_name(tmp_path, monkeypatch):
    """真正发出去的 ``model`` 是选中的那个上游名，不是平台侧身份。"""
    profile = providers.ModelProfile(
        id="api:model-a",
        label="Model A",
        provider="api",
        # 平台侧身份与上游名不同：请求体里绝不能出现 ``model-a``。
        upstream_model="model-a",
        upstreams=("nb2", "nb2-alt"),
        upstream_exec="nb2-alt",
        enabled=True,
        ratios=("1:1"),
        resolutions=("2K"),
        qualities=("high"))
    captured = _run_api_executor_with_fake_client(
        profile, _generation(profile), monkeypatch, tmp_path
    )
    assert captured["body"]["model"] == "nb2-alt"


def test_clearing_a_mark_releases_every_name_of_the_model(tmp_path, monkeypatch):
    """管理页的清标记要把它名下**所有**候选一起放开，否则「清过了还是不可用」。"""
    _prepare(tmp_path, monkeypatch)
    _mark("nb2", "nb2-alt")
    public_id = f"api:{providers._public_api_model_id('model-a')}"

    with TestClient(app) as client:
        token = _login(client)
        page = client.get("/admin/providers").text
        # 管理页不认识任何真实上游名，它只显示「这个模型此刻怎么了」。
        assert public_id in page
        assert "nb2" not in page
        response = client.post(
            "/admin/providers/api/routes/clear_exhausted",
            data={"csrf": token, "model": public_id, "route": "route-a"},
            follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as session:
        # 放行**只清标记、保留原文**（原文是校准识别词表的证据，不该被顺手删掉）。
        for name in ("nb2", "nb2-alt"):
            states = _states(session, name)
            assert states[0]["exhausted"] is False
            assert _QUOTA_ERROR in states[0]["last_error_message"]
        # 放行之后模型立刻回到可用状态（用户侧列表与执行期选名是同一套判据）。
        assert providers.routed_api_routes(session)["model-a"] == "route-a"
    assert _listed() is True


def test_a_label_holding_any_alias_never_reaches_the_user(tmp_path, monkeypatch):
    """显示名恰好是某个别名时，用户侧看到的仍然是「API 模型」，而不是那个裸上游名。

    旧行把裸上游名当展示名存下来过（打标迁移之前），所以这是一条历史路径；解绑之后判据
    从「单个上游名」变成了集合，漏掉一个名字就等于把上游名递给了浏览器。
    """
    _prepare(tmp_path, monkeypatch)
    profile = _profile()
    generation_id = _seed_generation(profile)

    with SessionLocal() as session:
        generation = session.get(Generation, generation_id)
        generation.model_label = "nb2-alt"
        session.commit()
        session.refresh(generation)
        assert web._generation_dict(generation)["model_label"] == "API 模型"
        assert management._safe_model_label(generation) == "API 模型"
