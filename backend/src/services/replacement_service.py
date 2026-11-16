"""Generate at most one replacement study per contaminated original study.

The replacement keeps the original parameters (library, fixed endmembers,
sigma, repeats, root seed and sample order).  Only samples whose active
observation changed are recomputed; every other sample's per-replicate
composition is copied verbatim from a still-valid archive row.  When the
original never completed there is no archive, so all samples are computed from
their currently active observations.

Idempotency: ``run_controls.replacement_of_run_id`` is unique and is set under a
row lock, and the whole creation runs inside a savepoint.  Concurrent revokes,
worker recovery and retries therefore cannot create a second replacement; later
revocations of the same study are folded into that one replacement in place.
"""
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from ..constants.status import PublishState, RunStatus
from ..models import RunControl, RunSourceLink, SpectrumSource, UnmixRun
from ..utils.clock import now
from . import job_queue
from .audit_service import log
from .source_service import active_source, affected_sample_ids, ensure_all_links, link_run_sources


def ensure_replacements(run_ids=None, actor="system"):
    """Idempotently reconcile replacement studies.  Safe to call repeatedly."""
    from ..config.database import SessionLocal

    to_queue = []
    with SessionLocal() as db:
        ids = run_ids
        if ids is None:
            ids = [
                control.run_id
                for control in db.query(RunControl)
                .filter(RunControl.publish_state == PublishState.REVOKED)
                .all()
            ]
        for run_id in sorted(set(ids or [])):
            replacement = _create_or_refresh(db, run_id, actor)
            if replacement and replacement.status == RunStatus.QUEUED:
                to_queue.append(replacement.id)
        db.commit()
    for run_id in sorted(set(to_queue)):
        try:
            job_queue.enqueue(run_id)
        except Exception:
            _mark_queue_failure(run_id)
    return sorted(set(to_queue))


def _mark_queue_failure(run_id):
    from ..config.database import SessionLocal

    with SessionLocal() as db:
        run = db.get(UnmixRun, run_id)
        control = db.query(RunControl).filter_by(run_id=run_id).first()
        if run and run.status == RunStatus.QUEUED:
            run.status = RunStatus.FAILED
            run.error = "队列不可用，替代研究未能排队；请稍后重试"
        if control:
            control.replacement_state = RunStatus.FAILED
            control.replacement_error = run.error if run else "队列不可用"
            control.updated_at = now()
        db.commit()


def _create_or_refresh(db, original_id, actor):
    control = (
        db.query(RunControl)
        .filter_by(run_id=original_id)
        .with_for_update()
        .first()
    )
    if not control or control.publish_state != PublishState.REVOKED:
        return None
    if control.replacement_of_run_id:
        # This control belongs to a replacement study that became contaminated
        # later.  Refresh that single replacement in place; a replacement of a
        # replacement must never be created.
        return _refresh_existing_replacement(db, control, actor)
    original = db.query(UnmixRun).filter_by(id=original_id).first()
    if not original or original.status == RunStatus.FAILED:
        return None
    ensure_all_links(db)

    if control.replacement_run_id:
        replacement = db.get(UnmixRun, control.replacement_run_id)
        if not replacement:
            control.replacement_run_id = None
            control.replacement_state = "none"
            return None
        replacement = (
            db.query(UnmixRun)
            .filter_by(id=replacement.id)
            .with_for_update()
            .first()
        )
        if not replacement:
            return None
        replacement_control = db.query(RunControl).filter_by(run_id=replacement.id).first()
        if replacement.status in (RunStatus.QUEUED, RunStatus.RUNNING):
            # Not finished yet: fold any newly revoked samples into the same
            # replacement instead of creating another one.
            _refresh_revoked(db, original, control, replacement, replacement_control)
            return replacement
        if (
            replacement_control
            and replacement_control.publish_state == PublishState.REVOKED
        ) or replacement.status == RunStatus.BLOCKED:
            # A later revocation contaminated this replacement too, or it was
            # parked because the replacement spectrum was still missing.
            # Recompute it in place (never a second replacement study), reusing
            # every still-valid archive row.
            _refresh_revoked(db, original, control, replacement, replacement_control)
            return replacement
        return replacement

    affected = affected_sample_ids(db, original_id)
    if not affected:
        if control.replacement_state not in (RunStatus.FAILED, RunStatus.BLOCKED):
            control.replacement_state = "none"
            control.replacement_error = "未找到受影响样本的来源关系"
            control.updated_at = now()
        return None

    archive = _seed_archive(db, original, affected)
    snapshot, problem = _build_snapshot(db, original, affected, archive)
    if problem:
        control.replacement_state = RunStatus.BLOCKED
        control.replacement_error = problem
        control.updated_at = now()
        return None

    savepoint = db.begin_nested()
    try:
        replacement = UnmixRun(
            name=_name(original),
            library_id=original.library_id,
            sample_ids=list(original.sample_ids),
            noise_sigma=original.noise_sigma,
            repeats=original.repeats,
            seed=original.seed,
            input_snapshot=snapshot,
            status=RunStatus.QUEUED,
            owner=original.owner,
        )
        db.add(replacement)
        db.flush()
        link_run_sources(db, replacement)
        db.add(
            RunControl(
                run_id=replacement.id,
                publish_state=PublishState.ACTIVE,
                replacement_of_run_id=original.id,
                replacement_state=RunStatus.QUEUED,
                replacement_sample_ids=affected,
                reuse_archive=archive,
            )
        )
        db.flush()
        control = db.query(RunControl).filter_by(run_id=original_id).with_for_update().first()
        control.replacement_run_id = replacement.id
        control.replacement_state = RunStatus.QUEUED
        control.replacement_error = ""
        control.updated_at = now()
        log(db, actor, "run.replacement", f"{replacement.id}<-{original.id}")
        db.flush()
        savepoint.commit()
        return replacement
    except IntegrityError:
        savepoint.rollback()
        # Another concurrent caller won the unique constraint; adopt its row.
        control = db.query(RunControl).filter_by(run_id=original_id).first()
        if control and control.replacement_run_id:
            return db.get(UnmixRun, control.replacement_run_id)
        return None


def _refresh_existing_replacement(db, replacement_control, actor):
    """A replacement whose own sources were later revoked.

    The replacement is recomputed in place with the union of all affected
    samples; its own polluted result is discarded, the archived original still
    backs every unaffected sample, and no second replacement study is created.
    """
    replacement = db.get(UnmixRun, replacement_control.run_id)
    if not replacement:
        return None
    original_control = (
        db.query(RunControl)
        .filter_by(replacement_run_id=replacement.id)
        .with_for_update()
        .first()
    )
    if not original_control:
        return None
    original = db.get(UnmixRun, replacement_control.replacement_of_run_id)
    if not original or original.status == RunStatus.FAILED:
        return None
    if replacement.status == RunStatus.FAILED and not _affected_grew(
        db, original, replacement, original_control
    ):
        # A computational failure is surfaced as-is; only an explicit retry or
        # a new revocation may re-run it.
        return replacement
    ensure_all_links(db)
    _refresh_revoked(
        db, original, original_control, replacement, replacement_control
    )
    return replacement


def _affected_grew(db, original, replacement, control):
    pending = _pending_samples(db, original, control)
    return not pending <= set(control.replacement_sample_ids or [])


def _pending_samples(db, original, replacement_control):
    """Samples that must be (re)computed in the next replacement round.

    A sample is pending when no archive row from this replacement's own history
    is still backed by its currently active observation.  Using that criterion
    (instead of "every sample ever revoked") is what lets round 3 reuse the
    valid rows produced by rounds 1 and 2 while recomputing only what changed.
    """
    archived = _valid_archive(db, replacement_control)
    pending = set()
    for sample in (original.input_snapshot or {}).get("samples", []):
        sample_id = sample.get("id")
        source = active_source(db, sample_id)
        entry = archived.get(str(sample_id))
        if not source or not entry or entry.get("source_id") != source.id:
            pending.add(sample_id)
    return pending


def _refresh_revoked(db, original, control, replacement, replacement_control):
    """Recompute an already-produced replacement in place after a later revoke.

    The replacement run keeps its id, name and parameters; only its snapshot,
    result and publication gate are reset.  Rows from its own valid archive are
    reused verbatim; only samples whose active observation changed are
    recomputed.
    """
    pending = _pending_samples(db, original, replacement_control)
    if replacement.status == RunStatus.RUNNING:
        # A worker already owns this job; its own publication gate will stop the
        # stale snapshot.  The next reconcile pass refreshes it in place.
        return
    if not pending:
        # Nothing the replacement computed has been invalidated.
        return
    if replacement.status == RunStatus.COMPLETED and replacement.result:
        history = list(replacement_control.result_history or [])
        history.append(
            {
                "run_id": replacement.id,
                "input_hash": replacement.result.get("input_hash", ""),
                "status": replacement.status,
                "replaced_at": now(),
                "result": replacement.result,
            }
        )
        replacement_control.result_history = history
    replacement.status = RunStatus.QUEUED
    replacement.error = ""
    replacement.result = {}
    replacement.job_token = ""
    replacement_control.publish_state = PublishState.ACTIVE
    replacement_control.blocked_reason = ""
    replacement_control.updated_at = now()
    control.replacement_state = RunStatus.QUEUED
    control.replacement_error = ""
    control.updated_at = now()
    _apply_refresh(db, original, control, replacement_control, replacement, pending)
    log(db, "system", "run.replacement.refresh", replacement.id)


def _apply_refresh(db, original, control, replacement_control, replacement, affected):
    # Take the run row lock so a concurrent worker claim (which flips the row to
    # running with a new job token) is visible before we rewrite its snapshot.
    replacement = (
        db.query(UnmixRun).filter_by(id=replacement.id).with_for_update().first()
    )
    if not replacement:
        return
    if replacement.status == RunStatus.RUNNING:
        # A worker already owns this job; its own publication gate will stop the
        # stale snapshot.  The next reconcile pass refreshes it in place.
        return
    archive = _valid_archive(db, replacement_control)
    snapshot, problem = _build_snapshot(db, original, affected, archive)
    if problem:
        control.replacement_state = RunStatus.BLOCKED
        control.replacement_error = problem
        control.updated_at = now()
        replacement.status = RunStatus.BLOCKED
        replacement.error = problem
        replacement.result = {}
        replacement.job_token = ""
        return
    replacement.input_snapshot = snapshot
    replacement.status = RunStatus.QUEUED
    replacement.error = ""
    replacement.job_token = ""
    control.replacement_state = RunStatus.QUEUED
    control.replacement_error = ""
    control.replacement_sample_ids = sorted(affected)
    control.updated_at = now()
    replacement.updated_at = now()
    for link in db.query(RunSourceLink).filter_by(run_id=replacement.id):
        db.delete(link)
    db.flush()
    link_run_sources(db, replacement)


def _seed_archive(db, original, affected):
    """Rows of the original archive that may be reused by a replacement.

    Only samples whose archived observation is still the active source qualify.
    """
    if original.status != RunStatus.COMPLETED or not original.result:
        return {}
    archive = {}
    for row in original.result.get("samples", []):
        sample_id = row.get("sample_id")
        if sample_id in affected:
            continue
        source = _archived_source(db, original, sample_id)
        active = active_source(db, sample_id)
        if source and active and source.id == active.id:
            archive[str(sample_id)] = {
                "run_id": original.id,
                "source_id": source.id,
                "row": row,
            }
    return archive


def _valid_archive(db, replacement_control):
    """Archive rows carried forward across replacement refresh rounds."""
    archive = {}
    for key, entry in (replacement_control.reuse_archive or {}).items():
        source = db.get(SpectrumSource, entry.get("source_id"))
        active = active_source(db, int(key))
        if source and active and source.id == active.id and source.status == "active":
            archive[key] = entry
    return archive


def archive_result(db, run):
    """Store a finished replacement's rows as reuse sources for later rounds.

    Only rows computed from the observation that is still active qualify; a row
    computed from a later-revoked spectrum is dropped so it can never be reused.
    """
    control = db.query(RunControl).filter_by(run_id=run.id).first()
    if not control or not control.replacement_of_run_id or not run.result:
        return
    archive = dict(control.reuse_archive or {})
    for row in run.result.get("samples", []):
        if row.get("reused_from_run_id"):
            continue
        sample_id = row.get("sample_id")
        source = active_source(db, sample_id)
        if not source:
            continue
        archive[str(sample_id)] = {
            "run_id": run.id,
            "source_id": source.id,
            "row": row,
        }
    control.reuse_archive = archive
    control.updated_at = now()


def _build_snapshot(db, original, affected, archive):
    original_samples = {
        s.get("id"): dict(s) for s in (original.input_snapshot or {}).get("samples", [])
    }
    missing = []
    samples = []
    for sample_id in sorted(original_samples):
        row = original_samples[sample_id]
        entry = archive.get(str(sample_id)) if sample_id not in affected else None
        if entry:
            archived_row = entry["row"]
            source = db.get(SpectrumSource, entry["source_id"])
            # The archived numbers belong to the archived observation, which may
            # be a replacement spectrum registered after the original snapshot.
            row["observed"] = list(
                source.observed if source else archived_row.get("observed", row["observed"])
            )
            row["source_id"] = entry["source_id"]
            row["source_code"] = source.code if source else archived_row.get("source_code", "")
            row["source_kind"] = source.kind if source else row.get("source_kind", "")
            row["reused_from_run_id"] = entry["run_id"]
        else:
            source = active_source(db, sample_id)
            if not source:
                missing.append(row.get("code", str(sample_id)))
                continue
            row["observed"] = list(source.observed)
            row["source_id"] = source.id
            row["source_code"] = source.code
            row["source_kind"] = source.kind
            row["replaces_revoked_source"] = sample_id in affected
            row.pop("reused_from_run_id", None)
        samples.append(row)
    if missing:
        return None, (
            "受影响样本尚未登记替代谱：" + "、".join(sorted(missing))
        )
    return {
        "library": (original.input_snapshot or {}).get("library"),
        "endmembers": (original.input_snapshot or {}).get("endmembers", []),
        "samples": samples,
    }, None


def _archived_source(db, original, sample_id):
    """The observation the original archive was actually computed from."""
    link = (
        db.query(RunSourceLink)
        .filter_by(run_id=original.id, sample_id=sample_id)
        .first()
    )
    if not link:
        return None
    if link.source_id:
        return db.get(SpectrumSource, link.source_id)
    if link.source_code:
        return db.query(SpectrumSource).filter_by(code=link.source_code).first()
    return None


def _name(original):
    name = original.name + " · 替代"
    return name if len(name) <= 120 else name[:119] + "…"


def retry_replacement(db, run_id, user):
    """Retry without generating a second replacement study."""
    control = db.query(RunControl).filter_by(run_id=run_id).with_for_update().first()
    if not control or control.publish_state != PublishState.REVOKED:
        raise HTTPException(404, "该研究没有待处理的替代任务")
    replacement = db.get(UnmixRun, control.replacement_run_id) if control.replacement_run_id else None
    if replacement:
        if replacement.status not in (RunStatus.FAILED, RunStatus.BLOCKED):
            raise HTTPException(409, "替代研究已存在，不能重复生成")
        replacement.status = RunStatus.QUEUED
        replacement.error = ""
        replacement.updated_at = now()
        control.replacement_state = RunStatus.QUEUED
        control.replacement_error = ""
        control.updated_at = now()
        log(db, user.email, "run.replacement.retry", replacement.id)
        db.commit()
        try:
            job_queue.enqueue(replacement.id)
        except Exception:
            _mark_queue_failure(replacement.id)
            raise HTTPException(503, "队列不可用，请稍后重试")
        return {"created": [], "requeued": [replacement.id]}
    control.replacement_state = "none"
    control.replacement_error = ""
    db.commit()
    created = ensure_replacements([run_id], user.email)
    return {"created": created, "requeued": []}
