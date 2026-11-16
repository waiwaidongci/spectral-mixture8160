"""Shared fixtures and helpers for the source-control test suite.

Tests run against SQLite and a fake Redis so the same service/worker code paths
used in Compose are exercised without external services.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("SEED_DEMO", "false")
    for name in list(sys.modules):
        if name == "src" or name.startswith("src.") or name == "scripts" or name.startswith("scripts."):
            del sys.modules[name]
    import fakeredis

    from src.services import job_queue

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(job_queue, "queue", fake)
    from src.config.database import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    from scripts.seed_demo import seed

    seed()
    return SessionLocal


def process_all(max_jobs=50):
    from src.services.job_queue import dequeue
    from src.worker import process

    processed = []
    for _ in range(max_jobs):
        job = dequeue(timeout=0)
        if not job:
            break
        process(job[1])
        processed.append(int(job[1]))
    return processed


def completed_run(db, library_code="LS-A"):
    """The seeded completed study over the first three LS-A samples."""
    from src.models import MixtureSample, SpectralLibrary, UnmixRun

    library = db.query(SpectralLibrary).filter_by(code=library_code).first()
    sample_ids = [
        s.id
        for s in db.query(MixtureSample)
        .filter_by(library_id=library.id)
        .order_by(MixtureSample.code)
        .limit(3)
    ]
    run = (
        db.query(UnmixRun)
        .filter(UnmixRun.sample_ids == sample_ids)
        .order_by(UnmixRun.id)
        .first()
    )
    return library, sample_ids, run


class Analyst:
    id = 2
    email = "analyst@lengsha.demo"
    name = "苏砚 · 光谱研究员"
