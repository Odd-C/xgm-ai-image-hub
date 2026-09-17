"""Background generation worker.

``web.py`` and ``app.py`` both import ``generation_worker`` from here, and the
queue only ever advances because of it. It owns a single daemon thread that
claims queued generations and hands them to
:func:`image_hub.providers.execute_generation`.

Claiming writes a lease (``lease_owner`` + ``lease_expires_at``) rather than
popping the row, so the queue stays durable in SQLite and no work is lost when
the process dies. A lease that lapses is **not** silently re-run: the previous
attempt may already have reached the upstream provider and started billing, so
re-submitting could double-charge. Those rows are parked in
``recovery_required`` for an operator to resolve from ``/admin/tasks``
(re-query the upstream task, or converge it to ``failed``). That behaviour
matches the recovery affordances the rest of the app already exposes.

The worker is deliberately single-threaded. Generation is dominated by upstream
latency, and one worker keeps provider rate limits and local CPU predictable;
``IMAGE_HUB_WORKER_POLL_SECONDS`` tunes how eagerly it looks for new work.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from datetime import timedelta

from sqlalchemy import select, update

from image_hub.config import settings
from image_hub.models import Generation, utcnow

logger = logging.getLogger("image_hub.worker")


class GenerationWorker:
    """Durable, lease-based queue drain for generations."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Woken by ``wake()`` so a fresh submission is picked up immediately
        # instead of waiting out the poll interval.
        self._work_event = threading.Event()
        self._identity = f"{socket.gethostname()}:{os.getpid()}"

    # ------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        # Park anything a previous process left mid-flight before serving work.
        self.reconcile_stale()
        self._stop_event.clear()
        self._work_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="generation-worker", daemon=True
        )
        self._thread.start()
        logger.info("生成队列 worker 已启动，标识 %s", self._identity)
        self.wake()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._work_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        logger.info("生成队列 worker 已停止")

    def wake(self) -> None:
        """Signal that new work may exist. Safe to call from any thread."""
        self._work_event.set()

    # ---------------------------------------------------------------- looping

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                worked = self._drain_once()
            except Exception:  # the loop must never die
                logger.exception("worker 处理队列时出现未预期错误")
                worked = False
            if worked:
                # More may be waiting; check again without sleeping.
                continue
            self._work_event.wait(timeout=max(settings.worker_poll_seconds, 0.1))
            self._work_event.clear()

    def _drain_once(self) -> bool:
        """Claim and run at most one generation. Returns whether work was done."""
        generation_id = self.claim_next()
        if generation_id is None:
            return False
        try:
            self._execute(generation_id)
        finally:
            self._release(generation_id)
        return True

    # ---------------------------------------------------------------- claiming

    def claim_next(self) -> str | None:
        """Atomically claim the oldest queued generation, or return None.

        Only ``queued`` rows are claimable. A ``running`` row whose lease has
        lapsed is intentionally left alone — see the module docstring.
        """
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
                    lease_owner=self._identity,
                    lease_expires_at=now
                    + timedelta(minutes=max(settings.worker_stale_minutes, 1)),
                )
            )
            session.commit()
            # ``rowcount`` guards against a second worker winning the race.
            return candidate if result.rowcount == 1 else None

    def _release(self, generation_id: str) -> None:
        """Drop our lease once the run has finished or failed."""
        from image_hub.db import SessionLocal

        with SessionLocal() as session:
            generation = session.get(Generation, generation_id)
            if generation is None or generation.lease_owner != self._identity:
                return
            generation.lease_owner = ""
            generation.lease_expires_at = None
            session.commit()

    def reconcile_stale(self) -> int:
        """Park generations whose lease lapsed while their worker was running.

        These are *not* requeued: the interrupted attempt may already be billing
        upstream, so the operator decides from ``/admin/tasks`` whether to
        re-query the upstream task or converge it to ``failed``.
        """
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

    # --------------------------------------------------------------- executing

    def _execute(self, generation_id: str) -> None:
        """Run one generation while keeping its lease alive.

        A generation can legitimately outlast the lease window (upstream image
        models take minutes), so the lease is renewed from a companion thread.
        Without this the row would look abandoned and ``reconcile_stale`` would
        park a perfectly healthy run.
        """
        from image_hub.providers import execute_generation

        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(generation_id, heartbeat_stop),
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

    def _heartbeat(self, generation_id: str, stop: threading.Event) -> None:
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
                        Generation.lease_owner == self._identity,
                    )
                    .values(
                        lease_expires_at=now
                        + timedelta(minutes=max(settings.worker_stale_minutes, 1))
                    )
                )
                session.commit()


generation_worker = GenerationWorker()
