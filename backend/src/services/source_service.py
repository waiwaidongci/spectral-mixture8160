"""Source lineage, revocation and publication gating.

A mixture sample owns one or more ``SpectrumSource`` observations.  Studies
(``UnmixRun``) are linked to the exact observations their frozen snapshot used
through ``RunSourceLink``.  A revoked source never disappears, so the original
spectrum, the link and the old result all stay queryable; only publication and
export are gated.
"""
import math

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..constants.status import PublishState, RunStatus, SourceKind, SourceStatus
from ..models import (
    Endmember,
    MixtureSample,
    RunControl,
    RunSourceLink,
    SpectralLibrary,
    SpectrumSource,
    UnmixRun,
)
from ..utils.clock import now
from ..utils.serialize import record
from .audit_service import log


# ---------------------------------------------------------------------------
# Reading sources and lineage
# ---------------------------------------------------------------------------
def list_sources(db: Session, sample_id: int):
    sample = db.get(MixtureSample, sample_id)
    if not sample:
        raise HTTPException(404, "样本不存在")
    ensure_sample_sources(db)
    db.commit()
    return [
        serialize_source(s)
        for s in db.query(SpectrumSource)
        .filter_by(sample_id=sample_id)
        .order_by(SpectrumSource.id)
    ]


def serialize_source(source: SpectrumSource):
    data = record(source)
    data["active"] = source.status == SourceStatus.ACTIVE
    return data


def active_source(db: Session, sample_id: int):
    return (
        db.query(SpectrumSource)
        .filter_by(sample_id=sample_id, status=SourceStatus.ACTIVE)
        .order_by(SpectrumSource.id.desc())
        .first()
    )


def ensure_sample_sources(db: Session):
    """Backfill one measured source for legacy samples that predate lineage."""
    missing = (
        db.query(MixtureSample)
        .outerjoin(SpectrumSource, SpectrumSource.sample_id == MixtureSample.id)
        .filter(SpectrumSource.id.is_(None))
        .order_by(MixtureSample.id)
        .all()
    )
    for sample in missing:
        db.add(
            SpectrumSource(
                code=_next_source_code(db, sample.id, sample.code),
                library_id=sample.library_id,
                sample_id=sample.id,
                kind=SourceKind.MEASURED,
                status=SourceStatus.ACTIVE,
                observed=list(sample.observed),
                owner="系统补录",
            )
        )
        db.flush()


def _next_source_code(db: Session, sample_id: int, sample_code: str):
    used = {
        s.code
        for s in db.query(SpectrumSource).filter_by(sample_id=sample_id)
    }
    index = 1
    while f"{sample_code}-SRC-{index:02d}" in used:
        index += 1
    return f"{sample_code}-SRC-{index:02d}"


def _resolve_source(db: Session, snapshot_sample: dict):
    source_id = snapshot_sample.get("source_id")
    if source_id:
        source = db.get(SpectrumSource, source_id)
        if source:
            return source
    code = snapshot_sample.get("source_code")
    if code:
        source = db.query(SpectrumSource).filter_by(code=code).first()
        if source:
            return source
    candidates = (
        db.query(SpectrumSource)
        .filter_by(sample_id=snapshot_sample.get("id"))
        .order_by(SpectrumSource.id)
        .all()
    )
    observed = snapshot_sample.get("observed")
    exact = [s for s in candidates if list(s.observed) == list(observed or [])]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def ensure_links(db: Session, run: UnmixRun):
    """Create missing source links for a run, including pre-existing studies."""
    snapshot_samples = (run.input_snapshot or {}).get("samples", [])
    existing = {
        link.sample_id
        for link in db.query(RunSourceLink).filter_by(run_id=run.id).all()
    }
    for snap in snapshot_samples:
        sample_id = snap.get("id")
        if sample_id is None or sample_id in existing:
            continue
        source = _resolve_source(db, snap)
        db.add(
            RunSourceLink(
                run_id=run.id,
                sample_id=sample_id,
                source_id=source.id if source else None,
                source_code=source.code if source else snap.get("source_code", ""),
                relation="snapshot",
            )
        )
        existing.add(sample_id)


def ensure_all_links(db: Session):
    ensure_sample_sources(db)
    for run in db.query(UnmixRun).order_by(UnmixRun.id).all():
        ensure_links(db, run)
    db.flush()


def _link_source(db: Session, link: RunSourceLink):
    """Resolve the exact observation a frozen link points at.

    Source codes are never reused (``_next_source_code`` only picks unused
    codes), so the code fallback is still a precise identity, never "whatever
    is active now".
    """
    if link.source_id:
        source = db.get(SpectrumSource, link.source_id)
        if source:
            return source
    if link.source_code:
        return db.query(SpectrumSource).filter_by(code=link.source_code).first()
    return None


def source_for_link(db: Session, link: RunSourceLink):
    return _link_source(db, link)


def revoked_link_reasons(db: Session, run: UnmixRun):
    """Why a run may not publish because a linked observation was revoked."""
    reasons = []
    sample_codes = {
        s.get("id"): s.get("code")
        for s in (run.input_snapshot or {}).get("samples", [])
    }
    for link in db.query(RunSourceLink).filter_by(run_id=run.id).order_by(RunSourceLink.id):
        source = source_for_link(db, link)
        code = sample_codes.get(link.sample_id, link.sample_id)
        if not source:
            # A frozen link that cannot be resolved to one observation cannot be
            # certified as clean; refuse publication rather than guess.
            reasons.append(f"{code} 的来源关系无法确认（缺少可追溯的测谱记录）")
            continue
        if source.status == SourceStatus.ACTIVE:
            continue
        detail = f"{code} 的来源 {source.code} 已撤销"
        if source.reason:
            detail += f"：{source.reason}"
        reasons.append(detail)
    return reasons


def affected_sample_ids(db: Session, run_id: int):
    sample_ids = set()
    for link in db.query(RunSourceLink).filter_by(run_id=run_id).all():
        source = source_for_link(db, link)
        if source and source.status != SourceStatus.ACTIVE:
            sample_ids.add(link.sample_id)
    return sorted(sample_ids)


def affected_run_ids(db: Session, source: SpectrumSource):
    links = db.query(RunSourceLink).filter_by(source_id=source.id).all()
    if not links:
        links = db.query(RunSourceLink).filter_by(source_code=source.code).all()
    return sorted({link.run_id for link in links})


# ---------------------------------------------------------------------------
# Snapshot construction: always use the currently active observation
# ---------------------------------------------------------------------------
def build_snapshot(db: Session, library: SpectralLibrary, sample_ids):
    ensure_sample_sources(db)
    members = (
        db.query(Endmember)
        .filter_by(library_id=library.id)
        .order_by(Endmember.code)
        .all()
    )
    samples = (
        db.query(MixtureSample)
        .filter(MixtureSample.id.in_(sample_ids), MixtureSample.library_id == library.id)
        .order_by(MixtureSample.code)
        .all()
    )
    if len(samples) != len(sample_ids):
        raise HTTPException(422, "样本重复、不存在或不属于所选光谱库")
    snapshot_samples = []
    missing = []
    for sample in samples:
        source = active_source(db, sample.id)
        if not source:
            missing.append(sample.code)
            continue
        row = record(sample)
        row["observed"] = list(source.observed)
        row["source_id"] = source.id
        row["source_code"] = source.code
        row["source_kind"] = source.kind
        snapshot_samples.append(row)
    if missing:
        raise HTTPException(
            422,
            "以下样本的测谱已撤销且尚未登记替代谱，不能创建或替代解混研究："
            + "、".join(missing),
        )
    return {
        "library": record(library),
        "endmembers": [record(e) for e in members],
        "samples": snapshot_samples,
    }


def link_run_sources(db: Session, run: UnmixRun):
    for snap in (run.input_snapshot or {}).get("samples", []):
        db.add(
            RunSourceLink(
                run_id=run.id,
                sample_id=snap.get("id"),
                source_id=snap.get("source_id"),
                source_code=snap.get("source_code", ""),
                relation="snapshot",
            )
        )


# ---------------------------------------------------------------------------
# Publication state
# ---------------------------------------------------------------------------
def control_for(db: Session, run_id: int, create=False, lock=False):
    query = db.query(RunControl).filter_by(run_id=run_id)
    if lock:
        query = query.with_for_update()
    control = query.first()
    if not control and create:
        control = RunControl(
            run_id=run_id,
            publish_state=PublishState.ACTIVE,
            replacement_state="none",
            replacement_sample_ids=[],
        )
        db.add(control)
        db.flush()
        if lock:
            control = (
                db.query(RunControl).filter_by(run_id=run_id).with_for_update().first()
            )
    return control


def publication_reasons(db: Session, run: UnmixRun):
    """Every reason a study may not publish or export, newest gate included."""
    control = control_for(db, run.id)
    reasons = []
    if control and control.publish_state == PublishState.REVOKED:
        reasons.append(control.blocked_reason or "来源测谱已撤销，结果停止发布与导出")
    reasons.extend(revoked_link_reasons(db, run))
    return reasons


def serialize_run(db: Session, run: UnmixRun):
    control = control_for(db, run.id)
    reasons = publication_reasons(db, run)
    publishable = run.status == RunStatus.COMPLETED and bool(run.result) and not reasons
    replacement_run = None
    if control and control.replacement_run_id:
        replacement_run = db.get(UnmixRun, control.replacement_run_id)
    replacement_state = control.replacement_state if control else "none"
    if replacement_run:
        replacement_state = replacement_run.status
    result = record(run)
    result.update(
        {
            "publish_state": control.publish_state if control else PublishState.ACTIVE,
            "publishable": publishable,
            "blocked_reason": "；".join(reasons),
            "replacement_of_run_id": control.replacement_of_run_id if control else None,
            "replacement_run_id": control.replacement_run_id if control else None,
            "replacement_state": replacement_state,
            "replacement_error": control.replacement_error if control else "",
            "replacement_sample_ids": (control.replacement_sample_ids or []) if control else [],
            "is_replacement": bool(control and control.replacement_of_run_id),
            "result_history": (
                [
                    {
                        "run_id": entry.get("run_id"),
                        "input_hash": entry.get("input_hash"),
                        "status": entry.get("status"),
                        "replaced_at": entry.get("replaced_at"),
                    }
                    for entry in (control.result_history or [])
                ]
                if control
                else []
            ),
            "source_links": [
                {
                    "sample_id": link.sample_id,
                    "source_code": link.source_code,
                    "source_status": (source_for_link(db, link).status
                                      if source_for_link(db, link) else "unknown"),
                }
                for link in db.query(RunSourceLink).filter_by(run_id=run.id).order_by(RunSourceLink.id)
            ],
        }
    )
    return result


# ---------------------------------------------------------------------------
# Revocation and replacement registration
# ---------------------------------------------------------------------------
def revoke_source(db: Session, sample_id: int, source_id: int, reason: str, user):
    source = (
        db.query(SpectrumSource)
        .filter_by(id=source_id)
        .with_for_update()
        .first()
    )
    if not source or source.sample_id != sample_id:
        raise HTTPException(404, "测谱来源不存在")
    if source.status == SourceStatus.REVOKED:
        raise HTTPException(409, "该测谱已经撤销，不能重复撤销")
    sample = db.query(MixtureSample).filter_by(id=sample_id).with_for_update().first()
    ensure_all_links(db)
    source.status = SourceStatus.REVOKED
    source.reason = reason
    source.revoked_at = now()
    run_ids = affected_run_ids(db, source)
    detail = (
        f"样本 {sample.code} 的测谱 {source.code} 因质检污染撤销"
        f"（{reason}）；旧谱与旧结果保留查阅，结果停止发布与导出"
    )
    replacement_ids = set()
    for run_id in run_ids:
        control = control_for(db, run_id, create=True, lock=True)
        run = db.query(UnmixRun).filter_by(id=run_id).with_for_update().first()
        if not run:
            continue
        control.publish_state = PublishState.REVOKED
        control.blocked_reason = detail
        control.updated_at = now()
        if control.replacement_of_run_id:
            # Affected run is itself a replacement: it will be recomputed in
            # place, never superseded by a second replacement study.
            replacement_ids.add(run.id)
            continue
        run.updated_at = now()
        if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
            # Any in-flight computation must not surface as a result: the worker
            # re-checks this gate in the transaction that would commit, and the
            # job token/claim check rejects the late write.
            run.status = RunStatus.BLOCKED
            run.error = detail
            run.result = {}
            run.job_token = ""
    log(db, user.email, "source.revoke", source.code)
    db.commit()
    _trigger_replacements(set(run_ids) | replacement_ids, user.email)
    return serialize_source(source), run_ids


def register_replacement(db: Session, sample_id: int, observed, reason: str, user):
    sample = (
        db.query(MixtureSample)
        .filter_by(id=sample_id)
        .with_for_update()
        .first()
    )
    if not sample:
        raise HTTPException(404, "样本不存在")
    ensure_sample_sources(db)
    sources = (
        db.query(SpectrumSource)
        .filter_by(sample_id=sample_id)
        .order_by(SpectrumSource.id)
        .all()
    )
    if any(s.status == SourceStatus.ACTIVE for s in sources):
        raise HTTPException(409, "当前仍有有效测谱，请先撤销受污染的测谱再登记替代谱")
    revoked = [s for s in sources if s.status == SourceStatus.REVOKED]
    if not revoked:
        raise HTTPException(409, "该样本没有已撤销的测谱，无需登记替代谱")
    library = db.get(SpectralLibrary, sample.library_id)
    values = _validate_spectrum(observed, library)
    source = SpectrumSource(
        code=_next_source_code(db, sample_id, sample.code),
        library_id=sample.library_id,
        sample_id=sample_id,
        kind=SourceKind.REPLACEMENT,
        status=SourceStatus.ACTIVE,
        observed=values,
        reason=reason,
        supersedes_id=revoked[-1].id,
        owner=user.name,
    )
    db.add(source)
    db.flush()
    log(db, user.email, "source.replace", source.code)
    db.commit()
    # Registering a spectrum must not resurrect a study whose replacement was
    # already recorded as failed: the explicit retry endpoint owns that.
    _trigger_replacements(None, user.email)
    return serialize_source(source)


def _validate_spectrum(observed, library):
    try:
        values = [float(v) for v in (observed or [])]
    except (TypeError, ValueError):
        raise HTTPException(422, "替代谱必须由有限数字组成")
    expected = len(library.wavelengths) if library else 0
    if expected and len(values) != expected:
        raise HTTPException(422, f"替代谱需要 {expected} 个波段数值")
    if not 2 <= len(values) <= 64:
        raise HTTPException(422, "替代谱需要 2–64 个波段数值")
    if not all(math.isfinite(v) and abs(v) <= 1000 for v in values):
        raise HTTPException(422, "替代谱包含非有限或超范围数值")
    return values


def _trigger_replacements(run_ids, actor):
    from .replacement_service import ensure_replacements

    ensure_replacements(run_ids, actor)


# ---------------------------------------------------------------------------
# Impact report
# ---------------------------------------------------------------------------
def impact(db: Session, library_id=None):
    ensure_all_links(db)
    db.commit()
    sample_query = db.query(MixtureSample)
    if library_id:
        sample_query = sample_query.filter_by(library_id=library_id)
    samples = sample_query.order_by(MixtureSample.code).all()
    rows = []
    affected_runs = set()
    revoked_total = 0
    for sample in samples:
        sources = (
            db.query(SpectrumSource)
            .filter_by(sample_id=sample.id)
            .order_by(SpectrumSource.id)
            .all()
        )
        revoked = [s for s in sources if s.status == SourceStatus.REVOKED]
        if not revoked:
            continue
        revoked_total += len(revoked)
        run_ids = set()
        for source in revoked:
            run_ids.update(affected_run_ids(db, source))
        runs = []
        for run_id in sorted(run_ids):
            affected_runs.add(run_id)
            run = db.get(UnmixRun, run_id)
            if run:
                runs.append(serialize_run(db, run))
        active = active_source(db, sample.id)
        rows.append(
            {
                "sample_id": sample.id,
                "code": sample.code,
                "name": sample.name,
                "library_id": sample.library_id,
                "sources": [serialize_source(s) for s in sources],
                "revoked": True,
                "active_source": serialize_source(active) if active else None,
                "affected_runs": runs,
            }
        )
    controls = db.query(RunControl).all()
    replacement_runs = {
        run.id: run
        for run in db.query(UnmixRun)
        .filter(UnmixRun.id.in_([c.replacement_run_id for c in controls if c.replacement_run_id]))
        .all()
    }

    def replacement_status(control):
        run = replacement_runs.get(control.replacement_run_id)
        return run.status if run else None

    summary = {
        "revoked_sources": revoked_total,
        "affected_runs": len(affected_runs),
        "replacements_pending": sum(
            1
            for c in controls
            if c.publish_state == PublishState.REVOKED
            and c.replacement_run_id is None
        ),
        "replacements_running": sum(
            1
            for c in controls
            if c.publish_state == PublishState.REVOKED
            and replacement_status(c) in (RunStatus.QUEUED, RunStatus.RUNNING)
        ),
        "replacements_completed": sum(
            1
            for c in controls
            if c.publish_state == PublishState.REVOKED
            and replacement_status(c) == RunStatus.COMPLETED
        ),
        "replacements_blocked": sum(
            1
            for c in controls
            if c.publish_state == PublishState.REVOKED
            and c.replacement_state == RunStatus.BLOCKED
        ),
        "replacements_failed": sum(
            1
            for c in controls
            if c.publish_state == PublishState.REVOKED
            and c.replacement_state == RunStatus.FAILED
        ),
    }
    return {"samples": rows, "summary": summary}
