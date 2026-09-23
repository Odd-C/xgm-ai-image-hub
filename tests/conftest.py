import os
from pathlib import Path

import pytest

TEST_ROOT = Path(__file__).parent / ".runtime"
os.environ["IMAGE_HUB_DATABASE_URL"] = f"sqlite:///{TEST_ROOT / 'test.db'}"
os.environ["IMAGE_HUB_STORAGE_ROOT"] = str(TEST_ROOT / "storage")
os.environ["IMAGE_HUB_SESSION_SECRET"] = "test-session-secret-0123456789abcdef"
os.environ["IMAGE_HUB_BOOTSTRAP_ADMIN_USERNAME"] = "admin"
os.environ["IMAGE_HUB_BOOTSTRAP_ADMIN_PASSWORD"] = "test-password"
os.environ["IMAGE_HUB_WORKER_ENABLED"] = "false"


def pytest_sessionstart(session):
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    database = TEST_ROOT / "test.db"
    if database.exists():
        database.unlink()


@pytest.fixture(autouse=True)
def _settle_previous_tasks():
    """每个用例前把遗留的活任务收尾，释放账号级配额。

    ``max_active_tasks_per_user`` 是**账号级**配额且只统计 queued/running，而整个测试
    会话共用一份 test.db：前面用例留下的 queued 任务会把这个配额吃掉，让后面任何一个
    提交都变成 429。在用例执行**前**收尾，既让每个用例从干净配额起步，也不影响那些
    自己要断言 429 的用例（它们在用例内部造活任务）。
    """
    from image_hub.db import init_database

    init_database()
    settle_pending_tasks()
    yield


def settle_pending_tasks() -> None:
    """把历史用例遗留的 queued/running 任务置为终态，释放账号级配额。"""
    from image_hub.db import SessionLocal
    from image_hub.models import Generation

    with SessionLocal() as session:
        for generation in session.query(Generation).filter(
            Generation.status.in_(("queued", "running"))
        ):
            generation.status = "succeeded"
        session.commit()
