"""Single-process worker with at-least-once, idempotent job handling.

Concurrency and interruption rules:

* A job is claimed with a conditional UPDATE (``status = queued``); a second
  delivery of the same job cannot execute it twice.
* A revoked/blocked study is never computed into a publishable result.  The
  gate is re-checked in the transaction that would commit, and the commit is a
  compare-and-swap on the claim, so a revocation racing with completion always
  wins and a late worker can never overwrite the blocked state.
* A replacement study is created at most once (unique constraint plus row
  lock); restart recovery rebuilds the queue from database truth, re-enqueues
  unfinished studies, and reconciles revoked originals that still owe a
  replacement.
"""
import time
import traceback
import uuid
from pathlib import Path

from sqlalchemy import update

from .config.database import Base, SessionLocal, engine
from .constants.status import PublishState, RunStatus
from .models import RunControl, RunSourceLink, UnmixRun
from .services.analysis_service import analyze_unmix
from .services.audit_service import log
from .services.job_queue import JOB_KEY, dequeue, enqueue, queue
from .services.replacement_service import archive_result, ensure_replacements
from .services.source_service import control_for, publication_reasons, source_for_link

ARTIFACTS = Path("/artifacts")
RESULT_PREFIX = "result:"


def _load_reuse(db, run):
    """Archived per-sample results reused verbatim by a replacement study.

    The replacement snapshot marks which samples are archived (via
    ``reused_from_run_id``).  Archived rows may come from the original study or
    from an earlier, still-valid replacement round (``reuse_archive``); each row
    is first checked against the frozen link so a polluted archive is rejected.
    """
    control = control_for(db, run.id)
    if not control or not control.replacement_of_run_id:
        return {}
    sources = {str(k): v for k, v in (control.reuse_archive or {}).items()}
    original = db.get(UnmixRun, control.replacement_of_run_id)
    if original and original.status == RunStatus.COMPLETED and original.result:
        for row in original.result.get("samples", []):
            sources.setdefault(
                str(row.get("sample_id")),
                {"run_id": original.id, "source_id": None, "row": row},
            )
    reuse = {}
    for sample in (run.input_snapshot or {}).get("samples", []):
        source_run_id = sample.get("reused_from_run_id")
        if not source_run_id:
            continue
        entry = sources.get(str(sample.get("id")))
        if not entry:
            raise ValueError(f"归档结果缺少样本 {sample.get('code')}，无法原样复用")
        link = (
            db.query(RunSourceLink)
            .filter_by(run_id=run.id, sample_id=sample["id"])
            .first()
        )
        linked = source_for_link(db, link) if link else None
        if not linked or linked.status != "active" or linked.id != sample.get("source_id"):
            raise ValueError(f"样本 {sample.get('code')} 的复用来源已失效，拒绝拼接研究结果")
        row = entry["row"]
        if list(row.get("observed", [])) != list(sample["observed"]):
            raise ValueError(f"样本 {sample.get('code')} 的归档观测谱不一致，拒绝复用")
        reuse[sample["id"]] = {**row, "reused_from_run_id": entry["run_id"]}
    return reuse


def process(job_value):
    run_id = int(job_value)
    token = uuid.uuid4().hex
    with SessionLocal() as db:
        claimed = db.execute(
            update(UnmixRun)
            .where(UnmixRun.id == run_id, UnmixRun.status == RunStatus.QUEUED)
            .values(status=RunStatus.RUNNING, job_token=token, updated_at=_now())
        ).rowcount
        db.commit()
        if not claimed:
            return
        run = db.get(UnmixRun, run_id)
        control = control_for(db, run_id)
        if control and control.publish_state == PublishState.REVOKED:
            _block(db, run, control, control.blocked_reason or "来源测谱已撤销")
            return
        if publication_reasons(db, run):
            _block(db, run, control, "；".join(publication_reasons(db, run)))
            return
        _sync_replacement_state(db, run.id, RunStatus.RUNNING)
        db.commit()

        try:
            reuse = _load_reuse(db, run)
            result = analyze_unmix(
                run.input_snapshot, run.noise_sigma, run.repeats, run.seed, reuse=reuse
            )
        except Exception as e:
            traceback.print_exc()
            db.rollback()
            run = db.get(UnmixRun, run_id)
            control = control_for(db, run_id)
            if control and control.publish_state == PublishState.REVOKED:
                _block(db, run, control, control.blocked_reason or "来源测谱已撤销")
            else:
                run.status = RunStatus.FAILED
                run.error = str(e)
                run.updated_at = _now()
                _sync_replacement_state(db, run.id, RunStatus.FAILED, str(e))
                log(db, "worker", "unmix.failed", run.id)
                db.commit()
            return

        # Gate is evaluated in the same transaction as the result commit.
        db.refresh(run)
        control = control_for(db, run_id)
        reasons = publication_reasons(db, run)
        if (control and control.publish_state == PublishState.REVOKED) or reasons:
            _block(
                db,
                run,
                control,
                (control.blocked_reason if control else "")
                or "；".join(reasons)
                or "来源测谱已撤销",
            )
            return
        # Final write is a compare-and-swap: it only lands if this worker still
        # owns a running claim.  A revocation that flipped the run to blocked
        # (or a competing delivery) makes the WHERE clause match nothing, so a
        # late worker can never resurrect a polluted result.
        updated = (
            db.query(UnmixRun)
            .filter(
                UnmixRun.id == run_id,
                UnmixRun.status == RunStatus.RUNNING,
                UnmixRun.job_token == token,
            )
            .update(
                {
                    UnmixRun.result: result,
                    UnmixRun.status: RunStatus.COMPLETED,
                    UnmixRun.error: "",
                    UnmixRun.updated_at: _now(),
                },
                synchronize_session=False,
            )
        )
        if not updated:
            db.rollback()
            run = db.get(UnmixRun, run_id)
            control = control_for(db, run_id)
            if control and control.publish_state == PublishState.REVOKED:
                _block(db, run, control, control.blocked_reason or "来源测谱已撤销")
            return
        db.expire_all()
        run = db.get(UnmixRun, run_id)
        _sync_replacement_state(db, run_id, RunStatus.COMPLETED)
        archive_result(db, run)
        log(db, "worker", "unmix.completed", run_id)
        db.commit()


def _block(db, run, control, reason):
    if control:
        control.publish_state = PublishState.REVOKED
        control.blocked_reason = reason
        control.updated_at = _now()
        if run:
            _sync_replacement_state(db, run.id, RunStatus.BLOCKED, reason)
    if run:
        run.status = RunStatus.BLOCKED
        run.error = reason
        run.result = {}
        run.job_token = ""
        run.updated_at = _now()
        log(db, "worker", "unmix.blocked", run.id)
    db.commit()


def _sync_replacement_state(db, run_id, state, error=""):
    control = db.query(RunControl).filter_by(run_id=run_id).first()
    if control and control.replacement_of_run_id:
        control.replacement_state = state
        control.replacement_error = error
        control.updated_at = _now()


def _now():
    from .utils.clock import now

    return now()


def recover():
    """Re-enqueue unfinished studies after a restart; never duplicate results.

    Queue entries are rebuilt from database truth, so a job that was delivered
    but never committed is retried, and a job whose result was already committed
    is not enqueued again.
    """
    ids = set()
    with SessionLocal() as db:
        pending = (
            db.query(UnmixRun)
            .filter(UnmixRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
            .order_by(UnmixRun.id)
            .all()
        )
        for run in pending:
            control = control_for(db, run.id)
            if control and control.publish_state == PublishState.REVOKED:
                run.status = RunStatus.BLOCKED
                run.error = control.blocked_reason or "来源测谱已撤销"
                run.result = {}
                continue
            if run.status == RunStatus.RUNNING:
                run.status = RunStatus.QUEUED
                run.job_token = ""
            ids.add(run.id)
        db.commit()
    for run_id in sorted(ids):
        enqueue(run_id)
    # Originals revoked while the worker was down still get exactly one
    # replacement; already-created replacements are skipped by the lock check.
    replaced = ensure_replacements(None, "worker.recover")
    for run_id in replaced:
        ids.add(run_id)
    return sorted(ids)


def main():
    Base.metadata.create_all(engine)
    ARTIFACTS.mkdir(exist_ok=True)
    # A restarted worker must not replay stale queue entries: clear them and
    # rebuild from database truth, which also de-duplicates retries.
    try:
        queue.delete(JOB_KEY)
    except Exception:
        traceback.print_exc()
    recover()
    while True:
        ARTIFACTS.joinpath("heartbeat").write_text(str(time.time()))
        job = dequeue(timeout=2)
        if not job:
            continue
        try:
            process(job[1])
        except Exception:
            traceback.print_exc()
            # The claim may not have been committed; put the id back so the
            # study is not silently dropped.  Set semantics make this a no-op
            # if a recovery pass already re-added it.
            try:
                enqueue(job[1])
            except Exception:
                traceback.print_exc()


if __name__ == "__main__":
    main()
