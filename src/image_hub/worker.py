"""Background generation worker."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from datetime import timedelta

from sqlalchemy import func, or_, select, update

from image_hub.config import settings
from image_hub.models import Generation, utcnow

logger = logging.getLogger("image_hub.worker")


class GenerationWorker:
    """Durable, lease-based queue drain for generations."""

    def __init__(self) -> None:
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        # Woken by ``wake()`` so a fresh submission is picked up immediately.
        self._work_event = threading.Event()
        # 每个槽位一个标识：租约归属到槽位而不是进程，槽位才能分清「这是我的租约」
        self._identities: list[str] = []
        # 周期维护（素材到期清理）的节流与互斥。
        self._last_maintenance_at = 0.0
        self._maintenance_lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    @property
    def concurrency(self) -> int:
        return len(self._threads)

    def start(self) -> None:
        if self.running:
            return
        # Park anything a previous process left mid-flight before serving work.
        self.reconcile_stale()
        self._stop_event.clear()
        self._work_event.clear()
        slots = max(int(settings.worker_concurrency), 1)
        prefix = f"{socket.gethostname()}:{os.getpid()}"
        self._identities = [f"{prefix}:slot-{index}" for index in range(slots)]
        self._threads = [
            threading.Thread(
                target=self._run_slot,
                args=(slot,),
                name=f"generation-worker-{slot}",
                daemon=True,
            )
            for slot in range(slots)
        ]
        for thread in self._threads:
            thread.start()
        logger.info("生成队列 worker 已启动，并发槽位 %d，标识前缀 %s", slots, prefix)
        self.wake()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._work_event.set()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=timeout)
        self._threads = []
        self._identities = []
        logger.info("生成队列 worker 已停止")

    def wake(self) -> None:
        """Signal that new work may exist. Safe to call from any thread."""
        self._work_event.set()

    # ---------------------------------------------------------------- looping

    def _run_slot(self, slot: int) -> None:
        identity = self._identities[slot]
        while not self._stop_event.is_set():
            try:
                worked = self._drain_once(identity)
                if not worked:
                    # 只在队列空档复查待恢复任务：这条路径要调上游
                    worked = self.reconcile_required()
                if not worked:
                    # 再空一档才做维护。清理要扫表 + 删文件，同样不该跟出图抢槽位。
                    worked = self.maintain()
            except Exception:  # the loop must never die
                logger.exception("worker 槽位 %d 处理队列时出现未预期错误", slot)
                worked = False
            if worked:
                # More may be waiting; check again without sleeping.
                continue
            self._work_event.wait(timeout=max(settings.worker_poll_seconds, 0.1))
            self._work_event.clear()

    def _drain_once(self, identity: str) -> bool:
        """Claim and run at most one generation. Returns whether work was done."""
        generation_id = self.claim_next(identity)
        if generation_id is None:
            return False
        try:
            self._execute(generation_id, identity)
        finally:
            self._release(generation_id, identity)
        return True

    # ---------------------------------------------------------------- claiming

    def claim_next(self, identity: str) -> str | None:
        """Atomically claim the oldest queued generation, or return None."""
        from image_hub.db import SessionLocal

        now = utcnow()
        with SessionLocal() as session:
            candidate = session.scalar(
                select(Generation.id)
                .where(Generation.status == "queued")
                .order_by(Generation.created_at)
                .limit(1)
            )
            if candidate is None:
                return None
            result = session.execute(
                update(Generation)
                .where(Generation.id == candidate, Generation.status == "queued")
                .values(
                    status="running",
                    started_at=now,
                    attempt_count=Generation.attempt_count + 1,
                    lease_owner=identity,
                    lease_expires_at=now
                    + timedelta(minutes=max(settings.worker_stale_minutes, 1)),
                )
            )
            session.commit()
            # ``rowcount`` guards against another slot (or another process)
            return candidate if result.rowcount == 1 else None

    def _release(self, generation_id: str, identity: str) -> None:
        """Drop our lease once the run has finished or failed."""
        from image_hub.db import SessionLocal

        with SessionLocal() as session:
            generation = session.get(Generation, generation_id)
            if generation is None or generation.lease_owner != identity:
                return
            generation.lease_owner = ""
            generation.lease_expires_at = None
            session.commit()

    def reconcile_stale(self) -> int:
        """Park generations whose lease lapsed while their worker was running."""
        from image_hub.db import SessionLocal

        now = utcnow()
        with SessionLocal() as session:
            stale = session.scalars(
                select(Generation).where(
                    Generation.status == "running",
                    Generation.lease_expires_at.is_not(None),
                    Generation.lease_expires_at < now,
                )
            ).all()
            for generation in stale:
                generation.status = "recovery_required"
                generation.error_message = "执行进程中断；为避免重复扣费，任务没有自动重提。"
                generation.lease_owner = ""
                generation.lease_expires_at = None
            session.commit()
            if stale:
                logger.warning("已将 %d 个中断任务转入恢复队列", len(stale))
            return len(stale)

    # ---------------------------------------------------------- auto reconcile

    def _claim_reconcile(self) -> str | None:
        """把「自增复查计数」和「认领候选」合成一次条件 UPDATE。"""
        from image_hub.db import SessionLocal

        now = utcnow()
        max_attempts = max(settings.worker_reconcile_max_attempts, 1)
        due = or_(
            Generation.next_reconcile_at.is_(None),
            Generation.next_reconcile_at <= now,
        )
        with SessionLocal() as session:
            candidate = session.scalar(
                select(Generation.id)
                .where(
                    Generation.status == "recovery_required",
                    Generation.external_task_id != "",
                    Generation.reconcile_count < max_attempts,
                    due,
                )
                .order_by(Generation.created_at)
                .limit(1)
            )
            if candidate is None:
                return None
            result = session.execute(
                update(Generation)
                .where(
                    Generation.id == candidate,
                    Generation.status == "recovery_required",
                    Generation.reconcile_count < max_attempts,
                    due,
                )
                .values(
                    # coalesce：历史行可能是 NULL
                    reconcile_count=func.coalesce(Generation.reconcile_count, 0) + 1,
                    next_reconcile_at=now
                    + timedelta(seconds=max(settings.worker_reconcile_seconds, 1.0)),
                )
            )
            session.commit()
            return candidate if result.rowcount == 1 else None

    def reconcile_required(self) -> bool:
        """复查一个到期的待恢复任务；返回是否真的处理了一个。"""
        candidate = self._claim_reconcile()
        if candidate is None:
            return False

        from image_hub.providers import recover_generation

        try:
            recovered, message = recover_generation(candidate)
        except Exception:  # 上游查询失败不该拖垮整个 worker
            logger.exception("自动复查任务 %s 时出错", candidate)
            return True
        if recovered:
            logger.info("自动复查已从上游恢复任务 %s", candidate)
        else:
            logger.debug("任务 %s 仍未出图：%s", candidate, message)
        return True

    # ------------------------------------------------------------ maintenance

    def maintain(self) -> bool:
        """周期维护：到点清理过期素材。返回本轮是否真的执行了。"""
        interval = max(float(settings.asset_purge_interval_minutes), 1.0) * 60.0
        now = time.monotonic()
        if now - self._last_maintenance_at < interval:
            return False
        with self._maintenance_lock:
            if now - self._last_maintenance_at < interval:
                return False
            self._last_maintenance_at = now

        from image_hub.assets import purge_expired_assets
        from image_hub.db import SessionLocal

        try:
            with SessionLocal() as session:
                purge_expired_assets(session)
        except Exception:  # 维护失败绝不能拖垮 worker 主循环
            logger.exception("周期维护（素材到期清理）失败")
        return True

    # --------------------------------------------------------------- executing

    def _execute(self, generation_id: str, identity: str) -> None:
        """Run one generation while keeping its lease alive."""
        from image_hub.providers import execute_generation

        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(generation_id, identity, heartbeat_stop),
            name=f"generation-heartbeat-{generation_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            execute_generation(generation_id)
        except Exception:  # execute_generation records failures itself
            logger.exception("生成任务 %s 执行失败", generation_id)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)

    def _heartbeat(self, generation_id: str, identity: str, stop: threading.Event) -> None:
        from image_hub.db import SessionLocal

        interval = max(10.0, settings.worker_stale_minutes * 20.0)
        while not stop.wait(interval):
            now = utcnow()
            with SessionLocal() as session:
                session.execute(
                    update(Generation)
                    .where(
                        Generation.id == generation_id,
                        Generation.status == "running",
                        Generation.lease_owner == identity,
                    )
                    .values(
                        lease_expires_at=now
                        + timedelta(minutes=max(settings.worker_stale_minutes, 1))
                    )
                )
                session.commit()


generation_worker = GenerationWorker()
