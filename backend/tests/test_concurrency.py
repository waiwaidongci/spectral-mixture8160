"""Concurrency, interruption and replacement-uniqueness scenarios."""
import threading

import pytest

from conftest import Analyst as User
from conftest import completed_run as _completed_run
from conftest import process_all as _process_all


def _new_run(db, library, sample_ids, name, repeats=20, seed=11):
    from src.schemas.run import RunInput
    from src.services.run_service import create_run

    return create_run(
        db,
        RunInput(
            name=name,
            library_id=library.id,
            sample_ids=sample_ids,
            noise_sigma=0.005,
            repeats=repeats,
            seed=seed,
        ),
        User,
    )


def test_revoke_during_computation_never_publishes(env, monkeypatch):
    """The worker finishes after a revoke; the late write must not land."""
    from src.models import MixtureSample, SpectralLibrary, SpectrumSource, UnmixRun
    from src.services import analysis_service, source_service
    from src.worker import process

    with env() as db:
        library = db.query(SpectralLibrary).filter_by(code="LS-A").first()
        sample_ids = [
            s.id
            for s in db.query(MixtureSample)
            .filter_by(library_id=library.id)
            .order_by(MixtureSample.code)
            .offset(25)
            .limit(2)
        ]
        run = _new_run(db, library, sample_ids, "运行中撤销")
        run_id = run["id"]
        source = db.query(SpectrumSource).filter_by(sample_id=sample_ids[0]).first()

        original = analysis_service.analyze_unmix
        revoked = {"done": False}

        def analyze_then_revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            source_service.revoke_source(
                db, sample_ids[0], source.id, "质检发现计算期间发现污染", User
            )
            revoked["done"] = True
            return result

        monkeypatch.setattr("src.worker.analyze_unmix", analyze_then_revoke)
        process(str(run_id))
        assert revoked["done"]
        db.expire_all()
        late = db.get(UnmixRun, run_id)
        assert late.status == "blocked"
        assert late.result == {}
        assert "撤销" in late.error

        # The computed-but-rejected result is gone; replacement is blocked
        # until a replacement spectrum is registered, then recomputed once.
        from src.services.replacement_service import ensure_replacements
        from src.services.source_service import register_replacement

        monkeypatch.setattr("src.worker.analyze_unmix", original)
        ensure_replacements(None, "test")
        control = source_service.control_for(db, run_id)
        assert control.replacement_run_id is None
        assert control.replacement_state == "blocked"
        register_replacement(db, sample_ids[0], list(source.observed), "重新测谱", User)
        ensure_replacements(None, "test")
        _process_all()
        db.expire_all()
        run = db.get(UnmixRun, run_id)
        assert run.result == {}
        replacements = (
            db.query(UnmixRun)
            .filter(UnmixRun.id != run_id, UnmixRun.sample_ids == sample_ids)
            .all()
        )
        assert len(replacements) == 1
        assert replacements[0].status == "completed"


def test_interrupted_worker_recovers_without_duplicate_result(env):
    """A run left RUNNING by a crash is re-queued and committed exactly once."""
    from src.models import MixtureSample, RunControl, SpectralLibrary, UnmixRun
    from src.services.job_queue import dequeue
    from src.worker import recover

    with env() as db:
        library = db.query(SpectralLibrary).filter_by(code="LS-A").first()
        sample_ids = [
            s.id
            for s in db.query(MixtureSample)
            .filter_by(library_id=library.id)
            .order_by(MixtureSample.code)
            .offset(30)
            .limit(1)
        ]
        run = _new_run(db, library, sample_ids, "中断恢复")
        run_id = run["id"]
        # Simulate a crash after claim: status RUNNING, stale token, no result.
        row = db.get(UnmixRun, run_id)
        row.status = "running"
        row.job_token = "stale-token"
        db.commit()
        while dequeue(timeout=0):
            pass

        recover()
        jobs = []
        while True:
            job = dequeue(timeout=0)
            if not job:
                break
            jobs.append(job[1])
        assert jobs.count(str(run_id)) == 1

        from src.worker import process

        for job_value in jobs:
            process(job_value)
        with env() as check:
            done = check.get(UnmixRun, run_id)
            assert done.status == "completed"
            assert done.result["input_hash"]
            # A stale late delivery for an already completed run is a no-op.
            process(str(run_id))
        with env() as check:
            again = check.get(UnmixRun, run_id)
            assert again.result["input_hash"] == done.result["input_hash"]
            assert check.query(RunControl).filter_by(run_id=run_id).count() == 1


def test_concurrent_replacement_creation_is_single(env):
    """Two callers racing to create the replacement produce exactly one."""
    from src.models import MixtureSample, SpectralLibrary, SpectrumSource, UnmixRun
    from src.services.replacement_service import ensure_replacements
    from src.services.source_service import register_replacement, revoke_source

    with env() as db:
        library = db.query(SpectralLibrary).filter_by(code="LS-A").first()
        sample_ids = [
            s.id
            for s in db.query(MixtureSample)
            .filter_by(library_id=library.id)
            .order_by(MixtureSample.code)
            .offset(35)
            .limit(2)
        ]
        run = _new_run(db, library, sample_ids, "并发替代")
        run_id = run["id"]
        _process_all()
        source = db.query(SpectrumSource).filter_by(sample_id=sample_ids[1]).first()
        revoke_source(db, sample_ids[1], source.id, "质检发现污染，需替代", User)
        register_replacement(db, sample_ids[1], list(source.observed), "重新测谱", User)

        errors = []

        def worker():
            try:
                ensure_replacements([run_id], "racer")
            except Exception as exc:  # pragma: no cover - surfaced via errors
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        with env() as check:
            replacements = (
                check.query(UnmixRun)
                .filter(
                    UnmixRun.name == check.get(UnmixRun, run_id).name + " · 替代"
                )
                .all()
            )
            assert len(replacements) == 1


def test_second_revocation_refreshes_same_replacement(env):
    """A later revocation never spawns a replacement-of-a-replacement."""
    from src.models import SpectrumSource, UnmixRun
    from src.models import RunControl
    from src.services.replacement_service import ensure_replacements
    from src.services.source_service import register_replacement, revoke_source

    with env() as db:
        library, sample_ids, run = _completed_run(db)
        first = db.query(SpectrumSource).filter_by(sample_id=sample_ids[0]).first()
        revoke_source(db, sample_ids[0], first.id, "质检发现第一份测谱污染", User)
        register_replacement(db, sample_ids[0], [v + 0.005 for v in first.observed], "重测", User)
        ensure_replacements(None, "test")
        _process_all()
        db.expire_all()
        control = db.query(RunControl).filter_by(run_id=run.id).first()
        replacement_id = control.replacement_run_id
        assert replacement_id
        replacement = db.get(UnmixRun, replacement_id)
        assert replacement.status == "completed"
        assert replacement.name == run.name + " · 替代"

        # Second sample's original measurement is now found to be contaminated.
        second = db.query(SpectrumSource).filter_by(sample_id=sample_ids[1]).first()
        revoke_source(db, sample_ids[1], second.id, "追溯发现第二份测谱也污染", User)
        db.expire_all()
        replacement = db.get(UnmixRun, replacement_id)
        assert replacement.status in ("queued", "failed", "blocked") or replacement.status == "completed"
        control = db.query(RunControl).filter_by(run_id=run.id).first()
        assert control.replacement_run_id == replacement_id, "不得生成第二份替代研究"
        register_replacement(db, sample_ids[1], [v + 0.004 for v in second.observed], "重测二", User)
        ensure_replacements(None, "test")
        _process_all()
        db.expire_all()
        runs = db.query(UnmixRun).all()
        assert len([r for r in runs if r.sample_ids == sample_ids]) == 2, "原研究 + 唯一替代"
        replacement = db.get(UnmixRun, replacement_id)
        assert replacement.status == "completed"
        reused = {
            s["sample_id"]: s.get("reused_from_run_id") for s in replacement.result["samples"]
        }
        # Round 1's recomputed row for sample 0 is still backed by the active
        # spectrum, so round 2 reuses it verbatim; only sample 1 is recomputed.
        assert reused[sample_ids[0]] == replacement_id
        assert reused[sample_ids[1]] is None
        assert reused[sample_ids[2]] == run.id


def test_third_revocation_reuses_valid_later_archive(env):
    """Round 3 reuses the earlier replacement's still-valid rows, not the
    polluted original archive, and recomputes only the newly affected sample."""
    from src.models import RunControl, SpectrumSource, UnmixRun
    from src.services.replacement_service import ensure_replacements
    from src.services.source_service import register_replacement, revoke_source

    with env() as db:
        library, sample_ids, run = _completed_run(db)
        first = db.query(SpectrumSource).filter_by(sample_id=sample_ids[0]).first()
        revoke_source(db, sample_ids[0], first.id, "质检发现第一份测谱污染", User)
        ensure_replacements(None, "test")
        _process_all()
        # Register the replacement spectrum so the replacement can complete.
        register_replacement(db, sample_ids[0], [v + 0.005 for v in first.observed], "重测一", User)
        ensure_replacements(None, "test")
        _process_all()
        db.expire_all()
        control = db.query(RunControl).filter_by(run_id=run.id).first()
        replacement_id = control.replacement_run_id
        round_one = db.get(UnmixRun, replacement_id)
        assert round_one.status == "completed"

        second = db.query(SpectrumSource).filter_by(sample_id=sample_ids[1]).first()
        revoke_source(db, sample_ids[1], second.id, "追溯发现第二份测谱污染", User)
        register_replacement(db, sample_ids[1], [v + 0.004 for v in second.observed], "重测二", User)
        ensure_replacements(None, "test")
        _process_all()
        db.expire_all()
        round_two = db.get(UnmixRun, replacement_id)
        assert round_two.status == "completed"
        round_two_rows = {s["sample_id"]: s for s in round_two.result["samples"]}
        round_two_hash = round_two.result["input_hash"]

        third = db.query(SpectrumSource).filter_by(sample_id=sample_ids[2]).first()
        revoke_source(db, sample_ids[2], third.id, "追溯发现第三份测谱污染", User)
        register_replacement(db, sample_ids[2], [v + 0.003 for v in third.observed], "重测三", User)
        ensure_replacements(None, "test")
        _process_all()
        db.expire_all()
        final = db.get(UnmixRun, replacement_id)
        assert final.status == "completed"
        assert len(db.query(UnmixRun).filter(UnmixRun.sample_ids == sample_ids).all()) == 2

        rows = {s["sample_id"]: s for s in final.result["samples"]}
        # Newly affected sample: recomputed from its new observation.
        assert rows[sample_ids[2]]["reused_from_run_id"] is None
        assert rows[sample_ids[2]]["observed"] == pytest.approx(
            [v + 0.003 for v in third.observed]
        )
        # Samples unaffected since the previous round: identical replicate rows.
        for sid in sample_ids[:2]:
            assert rows[sid]["reused_from_run_id"] == replacement_id
            assert rows[sid]["replicate_weights"] == round_two_rows[sid]["replicate_weights"]
            assert rows[sid]["replicate_rss"] == round_two_rows[sid]["replicate_rss"]
            assert rows[sid]["point_fit"]["weights"] == round_two_rows[sid]["point_fit"]["weights"]
        # The reused observations are the registered replacement spectra, never
        # the revoked originals.
        assert rows[sample_ids[0]]["observed"] == pytest.approx(
            [v + 0.005 for v in first.observed]
        )
        assert rows[sample_ids[1]]["observed"] == pytest.approx(
            [v + 0.004 for v in second.observed]
        )
        # Every superseded replacement result stays queryable by fingerprint.
        history = db.query(RunControl).filter_by(run_id=replacement_id).first().result_history
        assert len(history) == 2
        hashes = {entry["input_hash"] for entry in history}
        assert round_two_hash in hashes
        assert len(hashes) == 2
        assert all(entry["result"]["samples"] for entry in history)
