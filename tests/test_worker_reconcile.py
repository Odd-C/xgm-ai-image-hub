"""worker 自动复查与 LibTV 轮询语义的回归测试。

缺陷原文：``_poll_libtv_session`` 的 ``raise`` 写在 ``while`` 体内且无分支 ——
循环只跑一次、``deadline`` 是死代码、``lovart_chat_timeout_seconds`` 对它完全
无效；而同一段注释声称「worker 的外层 recovery loop 会让任务活下去」，那个
loop 从来不存在（``claim_next()`` 只认领 ``queued``）。结果是 LibTV 任务只要
首次查询时上游还没出图，就会永远停在 ``recovery_required`` 等管理员手工恢复。

这组用例锁住两件事：轮询确实是单次的，以及 worker 真的会把待恢复任务捞回来。
"""

import uuid
from datetime import timedelta

from sqlalchemy import select

from image_hub import providers as provider_module
from image_hub.config import settings
from image_hub.db import SessionLocal
from image_hub.models import Generation, Project, User, utcnow
from image_hub.worker import generation_worker


def _clear_pending_reconcile_tasks() -> None:
    """把别的用例留下的待恢复任务挪开，保证断言只针对本用例造的那一条。"""
    with SessionLocal() as session:
        for row in session.scalars(
            select(Generation).where(Generation.status == "recovery_required")
        ):
            row.status = "failed"
        session.commit()


def _seed_recoverable_task(
    *,
    reconcile_count: int = 0,
    next_reconcile_at=None,
    external_task_id: str = "session-1") -> str:
    with SessionLocal() as session:
        user = User(
            username=f"reconcile-user-{uuid.uuid4().hex[:8]}",
            password_hash="x",
            role="user",
            must_change_password=0)
        session.add(user)
        session.flush()
        project = Project(user_id=user.id, name=f"reconcile-project-{uuid.uuid4().hex[:8]}")
        session.add(project)
        session.flush()
        generation = Generation(
            user_id=user.id,
            project_id=project.id,
            idempotency_key=f"reconcile-{uuid.uuid4().hex}",
            original_prompt="待恢复任务",
            provider="libtv",
            model_id="libtv-default",
            model_label="LibTV",
            status="recovery_required",
            external_project_id="libtv-project-1",
            external_task_id=external_task_id,
            reconcile_count=reconcile_count,
            next_reconcile_at=next_reconcile_at)
        session.add(generation)
        session.commit()
        session.refresh(generation)
        return generation.id


def _detached_generation() -> Generation:
    """只用于调用纯函数，不落库。"""
    return Generation(
        user_id="detached-user",
        project_id="detached-project",
        idempotency_key="detached-key",
        original_prompt="x",
        provider="libtv",
        model_id="libtv-default",
        model_label="LibTV",
        status="running")


# ---------------------------------------------------------------------------


class _FakeLibTVMCP:
    """Minimal stand-in for the libtv_mcp module used by the poll path."""

    def __init__(self, call_tool):
        self._call_tool = call_tool

    def call_tool(self, name, arguments=None, *, timeout=60, account_id=""):
        return self._call_tool(name, arguments, timeout=timeout)

    def __getattr__(self, name):
        raise AttributeError(f"unexpected libtv_mcp access: {name}")


def test_poll_libtv_task_stores_the_artifact_when_the_result_is_there(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    result_dir = tmp_path / "generations" / "g1" / "results"
    result_dir.mkdir(parents=True)
    artifact = result_dir / "result_01.png"
    artifact.write_bytes(b"png-bytes")

    status = {
        "outcome": "SUCCEEDED",
        "readiness": "TERMINAL_READY",
        "artifacts": [{"node_key": "i-1", "kind": "image", "resource_id": "res_1"}],
    }
    calls = []

    def fake_call_tool( name, arguments=None, *, timeout=60):
        calls.append(name)
        if name == "task_status":
            return status
        if name == "inspect_media":
            return {"media": {"urls": ["https://media.test/abc.png"]}}
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(provider_module, "libtv_mcp", _FakeLibTVMCP(fake_call_tool))
    monkeypatch.setattr(provider_module, "_validated_images", lambda paths: list(paths))
    monkeypatch.setattr(
        provider_module, "_fetch_urls",
        lambda urls, output_dir, prefix: [artifact])

    generation = _detached_generation()
    provider_module._poll_libtv_task(
        generation, "project-1", "task-1", result_dir, "LibTV"
    )

    assert generation.artifact_storage_key == "generations/g1/results/result_01.png"
    assert calls == ["task_status", "inspect_media"]


# ---------------------------------------------------------------------------


def test_reconcile_required_picks_up_a_pending_task(monkeypatch):
    _clear_pending_reconcile_tasks()
    generation_id = _seed_recoverable_task()
    calls = []

    def fake_recover(identifier):
        calls.append(identifier)
        return False, "上游仍在生成中"

    monkeypatch.setattr(provider_module, "recover_generation", fake_recover)

    assert generation_worker.reconcile_required() is True
    assert calls == [generation_id]

    with SessionLocal() as session:
        row = session.get(Generation, generation_id)
        assert row.reconcile_count == 1
        assert row.next_reconcile_at is not None


def test_reconcile_required_records_before_calling_upstream(monkeypatch):
    """先记账后执行：即使复查把进程带崩，也不会陷入每轮都重试。"""
    _clear_pending_reconcile_tasks()
    generation_id = _seed_recoverable_task()

    def exploding_recover(identifier):
        raise RuntimeError("上游查询炸了")

    monkeypatch.setattr(provider_module, "recover_generation", exploding_recover)

    assert generation_worker.reconcile_required() is True

    with SessionLocal() as session:
        row = session.get(Generation, generation_id)
        assert row.reconcile_count == 1  # 计数已经落盘
        assert row.status == "recovery_required"


def test_reconcile_required_respects_the_throttle_window(monkeypatch):
    _clear_pending_reconcile_tasks()
    _seed_recoverable_task(next_reconcile_at=utcnow() + timedelta(seconds=300))
    calls = []

    def fake_recover(identifier):
        calls.append(identifier)
        return False, ""

    monkeypatch.setattr(provider_module, "recover_generation", fake_recover)

    assert generation_worker.reconcile_required() is False
    assert calls == []


def test_reconcile_required_stops_at_the_attempt_cap(monkeypatch):
    """超限就停手，交回 /admin/tasks 人工收敛，避免无休止打上游。"""
    _clear_pending_reconcile_tasks()
    _seed_recoverable_task(reconcile_count=settings.worker_reconcile_max_attempts)
    calls = []

    def fake_recover(identifier):
        calls.append(identifier)
        return False, ""

    monkeypatch.setattr(provider_module, "recover_generation", fake_recover)

    assert generation_worker.reconcile_required() is False
    assert calls == []


def test_reconcile_required_ignores_tasks_without_an_external_id(monkeypatch):
    """没有外部任务标识就没有可查询的对象。"""
    _clear_pending_reconcile_tasks()
    _seed_recoverable_task(external_task_id="")
    calls = []

    def fake_recover(identifier):
        calls.append(identifier)
        return False, ""

    monkeypatch.setattr(provider_module, "recover_generation", fake_recover)

    assert generation_worker.reconcile_required() is False
    assert calls == []
