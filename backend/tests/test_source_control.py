"""End-to-end tests for source revocation, replacement and publication gating."""
import pytest

from conftest import Analyst
from conftest import completed_run as _completed_run
from conftest import process_all as _process_all


def test_seed_creates_sources_links_and_controls(env):
    from src.models import MixtureSample, RunControl, RunSourceLink, SpectrumSource, UnmixRun

    with env() as db:
        assert db.query(SpectrumSource).count() == db.query(MixtureSample).count()
        run = db.query(UnmixRun).first()
        assert run is not None
        assert db.query(RunSourceLink).filter_by(run_id=run.id).count() == len(run.sample_ids)
        assert db.query(RunControl).filter_by(run_id=run.id).count() == 1
        for link in db.query(RunSourceLink).filter_by(run_id=run.id):
            source = db.get(SpectrumSource, link.source_id)
            assert source.status == "active"


def test_revoke_blocks_export_but_keeps_result(env):
    from src.models import SpectrumSource, UnmixRun
    from src.services.run_service import export_run
    from src.services.source_service import revoke_source, serialize_run

    with env() as db:
        library, sample_ids, run = _completed_run(db)
        source = db.query(SpectrumSource).filter_by(sample_id=sample_ids[0]).first()
        old_result = run.result
        prior_hash = run.result["input_hash"]
        assert export_run(db, run.id)

        revoke_source(db, sample_ids[0], source.id, "质检发现该测谱受交叉污染", Analyst)

        db.expire_all()
        run = db.query(UnmixRun).filter_by(id=run.id).first()
        assert run.result == old_result, "旧结果必须保留可查"
        assert run.result["input_hash"] == prior_hash
        payload = serialize_run(db, run)
        assert payload["publishable"] is False
        assert "已撤销" in payload["blocked_reason"]
        with pytest.raises(Exception) as exc:
            export_run(db, run.id)
        assert getattr(exc.value, "status_code", None) == 403
        # Spectrum itself is retained.
        db.refresh(source)
        assert source.status == "revoked"
        assert source.observed


def test_replacement_reuses_unaffected_samples_and_recomputes_affected(env):
    from src.models import ReplicationNote, SpectrumSource, UnmixRun
    from src.services.replacement_service import ensure_replacements
    from src.services.run_service import export_run
    from src.services.source_service import register_replacement, revoke_source, serialize_run

    with env() as db:
        library, sample_ids, run = _completed_run(db)
        original_result = {s["sample_id"]: s for s in run.result["samples"]}
        source = db.query(SpectrumSource).filter_by(sample_id=sample_ids[1]).first()
        revoke_source(db, sample_ids[1], source.id, "质检发现该测谱受交叉污染", Analyst)
        affected = sample_ids[1]
        new_spectrum = [float(v) + 0.01 for v in source.observed]
        register_replacement(db, affected, new_spectrum, "重新测谱并复核端元", Analyst)
        ensure_replacements(None, "test")
        ensure_replacements(None, "test")

        replacements = (
            db.query(UnmixRun)
            .filter(UnmixRun.name == run.name + " · 替代")
            .all()
        )
        assert len(replacements) == 1, "替代研究绝不能重复生成"
        replacement = replacements[0]
        assert replacement.status == "queued"
        assert replacement.noise_sigma == run.noise_sigma
        assert replacement.repeats == run.repeats
        assert replacement.seed == run.seed
        assert replacement.sample_ids == run.sample_ids

        _process_all()
        db.expire_all()
        replacement = db.get(UnmixRun, replacement.id)
        assert replacement.status == "completed"
        by_id = {s["sample_id"]: s for s in replacement.result["samples"]}
        # Unaffected samples: exact same replicate weights/rss/quantiles.
        for sid in sample_ids:
            if sid == affected:
                continue
            old, new = original_result[sid], by_id[sid]
            assert new["replicate_weights"] == old["replicate_weights"]
            assert new["replicate_rss"] == old["replicate_rss"]
            assert new["lower"] == old["lower"]
            assert new["point_fit"]["weights"] == old["point_fit"]["weights"]
            assert new["reused_from_run_id"] == run.id
        # Affected sample: recomputed from the registered replacement spectrum.
        assert by_id[affected]["observed"] == pytest.approx(new_spectrum)
        assert by_id[affected]["replicate_weights"] != original_result[affected]["replicate_weights"]
        assert by_id[affected]["reused_from_run_id"] is None

        # Replacement is publishable; original remains visible but blocked.
        assert serialize_run(db, replacement)["publishable"] is True
        db.expire_all()
        original = db.get(UnmixRun, run.id)
        assert serialize_run(db, original)["publishable"] is False
        assert original.result

        # Approval gate: export requires an approved note on the replacement.
        with pytest.raises(Exception):
            export_run(db, replacement.id)
        db.add(
            ReplicationNote(
                run_id=replacement.id,
                title="替代研究复核",
                interpretation="仅重算受影响样本，其余逐次配比原样复用。",
                proposal="按新测谱安排独立复配验证。",
                status="approved",
                approved_hash=replacement.result["input_hash"],
                owner="苏砚 · 光谱研究员",
                reviewer="贺青 · 材料负责人",
                review_note="批准替代研究归档，原研究保留查阅但停止导出。",
            )
        )
        db.commit()
        assert export_run(db, replacement.id)


def test_late_worker_cannot_restore_polluted_result(env):
    """Revoke while a study is queued; the worker must block instead of publish."""
    from src.models import MixtureSample, SpectralLibrary, SpectrumSource, UnmixRun
    from src.schemas.run import RunInput
    from src.services.run_service import create_run
    from src.services.source_service import revoke_source

    with env() as db:
        library = db.query(SpectralLibrary).filter_by(code="LS-A").first()
        sample_ids = [
            s.id
            for s in db.query(MixtureSample)
            .filter_by(library_id=library.id)
            .order_by(MixtureSample.code)
            .offset(10)
            .limit(2)
        ]
        run = create_run(
            db,
            RunInput(
                name="并发撤销测试",
                library_id=library.id,
                sample_ids=sample_ids,
                noise_sigma=0.005,
                repeats=20,
                seed=7,
            ),
            Analyst,
        )
        run_id = run["id"]
        source = db.query(SpectrumSource).filter_by(sample_id=sample_ids[0]).first()
        revoke_source(db, sample_ids[0], source.id, "质检发现该测谱受交叉污染", Analyst)

        _process_all()
        db.expire_all()
        late = db.get(UnmixRun, run_id)
        assert late.status == "blocked"
        assert late.result == {}
        assert "撤销" in late.error

        # Register replacement; replacement recomputes all samples because the
        # original was never archived, and publishes normally.
        from src.services.source_service import register_replacement
        from src.services.replacement_service import ensure_replacements

        register_replacement(
            db, sample_ids[0], list(source.observed), "重新测谱复核", Analyst
        )
        ensure_replacements(None, "test")
        _process_all()
        db.expire_all()
        replacements = (
            db.query(UnmixRun)
            .filter(UnmixRun.id != run_id, UnmixRun.sample_ids == sample_ids)
            .all()
        )
        assert len(replacements) == 1
        assert replacements[0].status == "completed"
        assert len(replacements[0].result["samples"]) == 2


def test_legacy_run_with_backfilled_links_is_controlled(env):
    from src.models import RunSourceLink, SpectrumSource, UnmixRun
    from src.services.run_service import export_run
    from src.services.source_service import (
        ensure_all_links,
        register_replacement,
        revoke_source,
        serialize_run,
    )

    with env() as db:
        _, sample_ids, run = _completed_run(db)
        # Simulate a study created before lineage existed.
        db.query(RunSourceLink).filter_by(run_id=run.id).delete()
        db.commit()
        ensure_all_links(db)
        db.commit()
        assert db.query(RunSourceLink).filter_by(run_id=run.id).count() == len(sample_ids)
        source = db.query(SpectrumSource).filter_by(sample_id=sample_ids[2]).first()
        revoke_source(db, sample_ids[2], source.id, "质检发现该测谱受污染", Analyst)
        db.expire_all()
        run = db.get(UnmixRun, run.id)
        assert serialize_run(db, run)["publishable"] is False
        with pytest.raises(Exception):
            export_run(db, run.id)
        register_replacement(db, sample_ids[2], source.observed, "重新测谱", Analyst)


def test_create_run_requires_active_source(env):
    from src.models import MixtureSample, SpectralLibrary, SpectrumSource
    from src.schemas.run import RunInput
    from src.services.run_service import create_run
    from src.services.source_service import revoke_source

    with env() as db:
        library = db.query(SpectralLibrary).filter_by(code="LS-A").first()
        sample = (
            db.query(MixtureSample)
            .filter_by(library_id=library.id)
            .order_by(MixtureSample.code.desc())
            .first()
        )
        source = db.query(SpectrumSource).filter_by(sample_id=sample.id).first()
        revoke_source(db, sample.id, source.id, "质检发现该测谱污染", Analyst)
        with pytest.raises(Exception) as exc:
            create_run(
                db,
                RunInput(
                    name="撤销后新建",
                    library_id=library.id,
                    sample_ids=[sample.id],
                    noise_sigma=0.005,
                    repeats=20,
                    seed=1,
                ),
                Analyst,
            )
        assert "替代谱" in str(exc.value)


def test_impact_report_lists_affected_runs_and_progress(env):
    from src.models import SpectrumSource
    from src.services.source_service import impact, revoke_source

    with env() as db:
        _, sample_ids, run = _completed_run(db)
        source = db.query(SpectrumSource).filter_by(sample_id=sample_ids[0]).first()
        revoke_source(db, sample_ids[0], source.id, "质检发现该测谱污染", Analyst)
        report = impact(db)
        assert report["summary"]["revoked_sources"] >= 1
        row = next(x for x in report["samples"] if x["sample_id"] == sample_ids[0])
        assert row["active_source"] is None
        assert any(r["id"] == run.id for r in row["affected_runs"])
        assert report["summary"]["affected_runs"] >= 1


def test_worker_duplicate_delivery_is_idempotent(env):
    from src.models import MixtureSample, SpectralLibrary
    from src.schemas.run import RunInput
    from src.services.run_service import create_run

    with env() as db:
        library = db.query(SpectralLibrary).filter_by(code="LS-A").first()
        sample_ids = [
            s.id
            for s in db.query(MixtureSample)
            .filter_by(library_id=library.id)
            .order_by(MixtureSample.code)
            .offset(20)
            .limit(1)
        ]
        run = create_run(
            db,
            RunInput(
                name="重复投递",
                library_id=library.id,
                sample_ids=sample_ids,
                noise_sigma=0.005,
                repeats=20,
                seed=3,
            ),
            Analyst,
        )
        run_id = run["id"]
        from src.worker import process

        process(str(run_id))
        with env() as check_db:
            from src.models import UnmixRun

            first = check_db.get(UnmixRun, run_id)
            result_hash = first.result["input_hash"]
        process(str(run_id))
        with env() as check_db:
            from src.models import UnmixRun

            again = check_db.get(UnmixRun, run_id)
            assert again.status == "completed"
            assert again.result["input_hash"] == result_hash