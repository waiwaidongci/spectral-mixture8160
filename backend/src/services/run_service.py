import csv
import io

import numpy as np
from fastapi import HTTPException

from ..constants.status import PublishState, RunStatus
from ..models import ReplicationNote, SpectralLibrary, UnmixRun
from .audit_service import log
from .job_queue import enqueue
from .source_service import (
    build_snapshot,
    control_for,
    ensure_all_links,
    ensure_sample_sources,
    link_run_sources,
    publication_reasons,
    serialize_run,
)
from .unmix_algorithm import matrix_diagnostics


def list_runs(db):
    ensure_all_links(db)
    db.commit()
    return [serialize_run(db, r) for r in db.query(UnmixRun).order_by(UnmixRun.id.desc())]


def get_run(db, rid):
    row = db.get(UnmixRun, rid)
    if not row:
        raise HTTPException(404, "解混运行不存在")
    ensure_all_links(db)
    db.commit()
    return row


def create_run(db, data, user):
    ensure_sample_sources(db)
    library = db.get(SpectralLibrary, data.library_id)
    if not library:
        raise HTTPException(404, "光谱库不存在")
    if len(set(data.sample_ids)) != len(data.sample_ids) or len(data.sample_ids) * data.repeats > 480:
        raise HTTPException(422, "样本不可重复，样本数×重复数不得超过480")
    snapshot = build_snapshot(db, library, data.sample_ids)
    try:
        _, d = matrix_diagnostics(np.array([e["spectrum"] for e in snapshot["endmembers"]]).T)
        if not d["supported"]:
            raise ValueError(d["reason"])
    except ValueError as e:
        raise HTTPException(422, str(e))
    row = UnmixRun(**data.model_dump(), input_snapshot=snapshot, owner=user.name)
    db.add(row)
    db.flush()
    link_run_sources(db, row)
    control_for(db, row.id, create=True)
    log(db, user.email, "unmix.enqueue", row.id)
    db.commit()
    try:
        enqueue(row.id)
    except Exception:
        row.status = RunStatus.FAILED
        row.error = "队列不可用，请重提"
        db.commit()
        raise HTTPException(503, row.error)
    return serialize_run(db, row)


def export_run(db, rid):
    row = get_run(db, rid)
    control = control_for(db, rid)
    if control and control.publish_state == PublishState.REVOKED:
        raise HTTPException(403, "来源测谱已撤销，该结果停止导出；请等待替代研究完成")
    if publication_reasons(db, row):
        raise HTTPException(403, "来源测谱已撤销，该结果停止导出；请等待替代研究完成")
    if row.status != RunStatus.COMPLETED or not row.result:
        raise HTTPException(403, "仅获批完成研究可导出")
    if not db.query(ReplicationNote).filter_by(run_id=rid, status="approved").first():
        raise HTTPException(403, "仅获批完成研究可导出")
    # An approval is bound to the result hash it reviewed; a recomputed result
    # needs a fresh approval before its numbers may leave the system.
    current_hash = (row.result or {}).get("input_hash", "")
    approved = (
        db.query(ReplicationNote)
        .filter_by(run_id=rid, status="approved", approved_hash=current_hash)
        .first()
    )
    if not approved:
        raise HTTPException(403, "当前结果尚未获得与之一致的批准，请重新审阅后再导出")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(
        [
            "sample",
            "endmember",
            "weight",
            "conditional_q025",
            "conditional_q975",
            "rss",
            "rmse",
            "noise_sigma",
            "repeats",
            "root_seed",
            "input_hash",
            "sample_source",
        ]
    )
    for s in row.result["samples"]:
        for i, e in enumerate(row.result["endmembers"]):
            w.writerow(
                [
                    s["code"],
                    e["code"],
                    s["point_fit"]["weights"][i],
                    s["lower"][i],
                    s["upper"][i],
                    s["point_fit"]["rss"],
                    s["point_fit"]["rmse"],
                    row.noise_sigma,
                    row.repeats,
                    row.seed,
                    row.result["input_hash"],
                    s.get("source_code", ""),
                ]
            )
    return out.getvalue()
