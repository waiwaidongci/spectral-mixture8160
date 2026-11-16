"""Backfill of a legacy database that predates source tracking."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LEGACY_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(160) UNIQUE, name VARCHAR(100), role VARCHAR(24), password_hash VARCHAR(200));
CREATE TABLE spectral_libraries (id INTEGER PRIMARY KEY, code VARCHAR(40) UNIQUE, name VARCHAR(120), description TEXT, wavelengths JSON, source VARCHAR(160));
CREATE TABLE endmembers (id INTEGER PRIMARY KEY, library_id INT, code VARCHAR(40), name VARCHAR(80), spectrum JSON);
CREATE TABLE mixture_samples (id INTEGER PRIMARY KEY, code VARCHAR(80) UNIQUE, library_id INT, name VARCHAR(120), observed JSON, note TEXT);
CREATE TABLE unmix_runs (id INTEGER PRIMARY KEY, name VARCHAR(120), library_id INT, sample_ids JSON, noise_sigma DOUBLE, repeats INT, seed INT, input_snapshot JSON, status VARCHAR(24), result JSON, error TEXT, owner VARCHAR(100), created_at VARCHAR(64));
CREATE TABLE replication_notes (id INTEGER PRIMARY KEY, run_id INT, title VARCHAR(120), interpretation TEXT, proposal TEXT, status VARCHAR(24), owner VARCHAR(100), reviewer VARCHAR(100), review_note TEXT, created_at VARCHAR(64));
CREATE TABLE audits (id INTEGER PRIMARY KEY, actor VARCHAR(160), action VARCHAR(100), target VARCHAR(120), created_at VARCHAR(64));
"""


def legacy_db(path):
    import numpy as np

    if os.path.exists(path):
        os.remove(path)
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    waves = np.arange(400, 700, 25, dtype=float).tolist()
    spectra = [[0.2 + 0.01 * i + 0.02 * j for i in range(12)] for j in range(3)]
    connection.execute(
        "INSERT INTO spectral_libraries VALUES (1,'LS-A','校准库','旧库',?,'x.json')",
        (str(waves).replace(" ", ""),),
    )
    for index, spectrum in enumerate(spectra):
        connection.execute(
            "INSERT INTO endmembers VALUES (?,1,?,?,?)",
            (index + 1, f"EM-0{index + 1}", f"端元{index + 1}", str(spectrum)),
        )
    for index in range(3):
        connection.execute(
            "INSERT INTO mixture_samples VALUES (?,?,1,?,?,?)",
            (
                index + 1,
                f"LS-A-00{index + 1}",
                f"样本{index + 1}",
                str([0.21 + 0.005 * index] * 12),
                "旧批注",
            ),
        )
    import json

    snapshot = {
        "library": {"id": 1, "code": "LS-A", "wavelengths": waves},
        "endmembers": [
            {"id": i + 1, "code": f"EM-0{i + 1}", "spectrum": spectra[i]}
            for i in range(3)
        ],
        "samples": [
            {
                "id": i + 1,
                "code": f"LS-A-00{i + 1}",
                "observed": [0.21 + 0.005 * i] * 12,
            }
            for i in range(3)
        ],
    }
    connection.execute(
        "INSERT INTO unmix_runs VALUES (1,'旧研究',1,'[1,2,3]',0.005,80,2026,?,'completed',?, '', '苏砚', '2026-11-01T00:00:00+00:00')",
        (json.dumps(snapshot), json.dumps({"input_hash": "legacyhash", "samples": []})),
    )
    connection.execute(
        "INSERT INTO replication_notes VALUES (1,1,'旧建议','旧解释文本','旧方案文本','approved','苏砚','贺青','旧批准','2026-11-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()


def prepare(monkeypatch, tmp_path):
    path = str(tmp_path / "legacy.db")
    legacy_db(path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("SEED_DEMO", "false")
    for name in list(sys.modules):
        if name == "src" or name.startswith("src.") or name == "scripts" or name.startswith("scripts."):
            del sys.modules[name]
    import fakeredis

    from src.services import job_queue

    monkeypatch.setattr(job_queue, "queue", fakeredis.FakeRedis(decode_responses=True))
    from scripts.backfill_sources import backfill

    backfill()
    backfill()
    return path


def test_backfill_legacy_database(monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path)
    from src.config.database import SessionLocal
    from src.models import (
        MixtureSample,
        ReplicationNote,
        RunControl,
        RunSourceLink,
        SpectrumSource,
        UnmixRun,
    )

    with SessionLocal() as db:
        assert db.query(SpectrumSource).count() == db.query(MixtureSample).count() == 3
        assert db.query(RunSourceLink).filter_by(run_id=1).count() == 3
        assert db.query(RunControl).filter_by(run_id=1).count() == 1
        run = db.get(UnmixRun, 1)
        assert run.result["input_hash"] == "legacyhash"
        note = db.get(ReplicationNote, 1)
        assert note.status == "approved"
        assert note.approved_hash == "legacyhash"
        # Spectra are copied from the sample and remain unmodified by a re-run.
        for sample in db.query(MixtureSample).all():
            source = db.query(SpectrumSource).filter_by(sample_id=sample.id).first()
            assert source.observed == sample.observed
            assert source.status == "active"


def test_legacy_approval_does_not_authorize_new_result(monkeypatch, tmp_path):
    """A shipped-then-recomputed result needs a fresh approval to export."""
    prepare(monkeypatch, tmp_path)
    from src.config.database import SessionLocal
    from src.models import UnmixRun
    from src.services.run_service import export_run

    with SessionLocal() as db:
        approved = export_run(db, 1)
        assert approved
        run = db.get(UnmixRun, 1)
        run.result = dict(run.result, input_hash="recomputed")
        db.commit()
        try:
            export_run(db, 1)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 403
        else:  # pragma: no cover
            raise AssertionError("旧批准不应授权新结果导出")
