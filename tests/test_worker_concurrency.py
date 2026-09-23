"""Worker 并发池与认领互斥的单元测试。

这些用例全部走「新实例 ``GenerationWorker()``」而不是模块级单例 ``generation_worker``，
避免污染其他用例。每个用例结束前都通过 ``try/finally`` 把 worker 停掉，否则残留线程
会继续认领真实任务、搅乱别的用例。

⚠️ 历史坑提醒：本仓库测试共用一份 runtime DB，所以凡是「数任务」的断言都按
``project_id`` 过滤；造出来的行在 finally 里连同项目一起删掉，不污染别的用例。
"""

import threading
import time
from datetime import timedelta

from sqlalchemy import update

from image_hub import providers
from image_hub.config import settings
from image_hub.db import SessionLocal
from image_hub.models import Generation, Project, User, utcnow
from image_hub.worker import GenerationWorker


def _admin_user(session) -> User:
    # conftest 的 autouse fixture 保证 bootstrap 管理员一定存在。
    return session.query(User).filter_by(username=settings.bootstrap_admin_username).one()


def _make_project(session, name: str) -> Project:
    project = Project(name=name, user_id=_admin_user(session).id)
    session.add(project)
    session.commit()
    return project


def _queue_task(session, project: Project, key: str) -> str:
    generation = Generation(
        user_id=project.user_id,
        project_id=project.id,
        idempotency_key=key,
        original_prompt="x",
        provider="api",
        model_id="m",
        model_label="m",
        status="queued")
    session.add(generation)
    session.commit()
    return generation.id


def _cleanup_project(session, project_id: str) -> None:
    project = session.get(Project, project_id)
    if project is not None:
        # 级联删除其下的 generations，避免留下 running 残行干扰别的用例。
        session.delete(project)
        session.commit()


def test_worker_runs_queued_tasks_at_full_concurrency(tmp_path, monkeypatch):
    """A1. 并发槽位真并发：3 个槽位同时跑时，峰值并发 == 3。

    用新实例 + 阻塞桩（threading.Event 卡住并记录同时在跑的最大数），塞 5 个 queued
    任务，start() 后等峰值到 3，放行、stop()。桩不碰 DB，最后按 project_id 删除。
    """
    monkeypatch.setattr(settings, "worker_concurrency", 3)
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    # 空闲槽位可能去调 recover_generation（真连上游）；本用例只测并发，直接禁掉。
    monkeypatch.setattr(providers, "recover_generation", lambda _id: (False, "skipped"))

    state = {"current": 0, "peak": 0}
    lock = threading.Lock()
    gate = threading.Event()

    def fake_execute(generation_id):  # 替换 providers.execute_generation
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        gate.wait()  # 卡住，直到主线程放行
        with lock:
            state["current"] -= 1

    monkeypatch.setattr(providers, "execute_generation", fake_execute)

    worker = GenerationWorker()
    with SessionLocal() as session:
        project = _make_project(session, "并发测试-A1")
        created = [_queue_task(session, project, f"a1-{i}") for i in range(5)]
        project_id = project.id

    try:
        worker.start()
        # 等峰值冲到 3（3 个槽位各自认领一条并卡在桩里）。
        deadline = time.monotonic() + 10
        while state["peak"] < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert state["peak"] == 3, state
        # 槽位数 = worker_concurrency
        assert worker.concurrency == 3
    finally:
        gate.set()
        worker.stop()
        # 按 project_id 清理：我的 5 个任务应全部被 worker 跑过（峰值 3 说明并发到位）。
        with SessionLocal() as session:
            count = session.query(Generation).filter_by(project_id=project_id).count()
            assert count == len(created)
            _cleanup_project(session, project_id)


def test_concurrent_claim_next_never_duplicates(tmp_path, monkeypatch):
    """A2. 并发认领不重复：多线程同时 claim_next，拿到的 id 集合无重复、总数 <= 任务数。

    靠条件 UPDATE 的 rowcount 裁决，这是池内唯一的互斥；两个空闲槽位不会拿到同一行。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    worker = GenerationWorker()
    n = 6
    with SessionLocal() as session:
        project = _make_project(session, "并发认领-A2")
        ids = [_queue_task(session, project, f"a2-{i}") for i in range(n)]
        project_id = project.id

    results: list = []
    rlock = threading.Lock()

    def claim(index):
        got = worker.claim_next(f"slot-{index}")
        with rlock:
            results.append(got)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    non_none = [r for r in results if r is not None]
    # 无重复 + 不超过任务总数 + 都来自我们造的行
    assert len(non_none) == len(set(non_none))
    assert len(non_none) <= n
    assert set(non_none).issubset(set(ids))

    with SessionLocal() as session:
        _cleanup_project(session, project_id)


def test_claim_reconcile_race_picks_exactly_one_winner(tmp_path, monkeypatch):
    """A3. _claim_reconcile() 竞态：一个到期的 recovery_required 任务，两个线程同时抢。

    恰好一个返回该 id、另一个返回 None；reconcile_count 只 +1（条件 UPDATE 的 rowcount
    裁决，把「自增计数」和「认领候选」合成一次原子 UPDATE）。
    """
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    worker = GenerationWorker()
    with SessionLocal() as session:
        project = _make_project(session, "竞态-A3")
        generation = Generation(
            user_id=project.user_id,
            project_id=project.id,
            idempotency_key="a3-1",
            original_prompt="x",
            provider="api",
            model_id="m",
            model_label="m",
            status="recovery_required",
            external_task_id="ext-1",  # _claim_reconcile 要求 external_task_id 非空
            reconcile_count=0,
            next_reconcile_at=None,  # None => 立即到期（due）
        )
        session.add(generation)
        # 把本任务人为改成「最旧」：_claim_reconcile 按 created_at 取最旧候选，这样即使
        generation.created_at = utcnow() - timedelta(days=1)
        session.commit()
        generation_id = generation.id
        project_id = project.id

        # ⚠️ **必做的一步**：共享 runtime DB 里还躺着别的 ``recovery_required`` 行
        with SessionLocal() as session:
            session.execute(
                update(Generation)
                .where(
                    Generation.status == "recovery_required",
                    Generation.id != generation_id)
                .values(status="failed")
            )
            session.commit()

        results: list = []
        rlock = threading.Lock()

        def race():
            got = worker._claim_reconcile()
            with rlock:
                results.append(got)

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0] == generation_id

    with SessionLocal() as session:
        reloaded = session.get(Generation, generation_id)
        # 只有赢家那次把 reconcile_count 从 0 自增到 1
        assert reloaded.reconcile_count == 1
        _cleanup_project(session, project_id)
