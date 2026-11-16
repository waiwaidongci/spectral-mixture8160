from fastapi import HTTPException

from ..constants.status import RunStatus
from ..models import ReplicationNote, UnmixRun
from ..utils.serialize import record
from .audit_service import log
from .source_service import publication_reasons


def list_notes(db):
    return [record(n) for n in db.query(ReplicationNote).order_by(ReplicationNote.id.desc())]


def create_note(db, data, user):
    run = db.get(UnmixRun, data.run_id)
    if not run or run.status != RunStatus.COMPLETED:
        raise HTTPException(409, "只能对已完成解混研究提交复配建议")
    if publication_reasons(db, run):
        raise HTTPException(409, "该研究的来源测谱已撤销，请等待替代研究完成后再提交复配建议")
    row = ReplicationNote(**data.model_dump(), owner=user.name, status="submitted")
    db.add(row)
    db.flush()
    log(db, user.email, "replication.submit", row.id)
    db.commit()
    return record(row)


def review_note(db, nid, data, user):
    row = db.get(ReplicationNote, nid)
    if not row:
        raise HTTPException(404, "复配建议不存在")
    if row.status != "submitted":
        raise HTTPException(409, "已审阅建议不可重复审批")
    run = db.get(UnmixRun, row.run_id)
    if data.status == "approved":
        if not run or run.status != RunStatus.COMPLETED or not run.result:
            raise HTTPException(409, "该研究没有可批准的已完成结果")
        if publication_reasons(db, run):
            raise HTTPException(409, "该研究的来源测谱已撤销，不能批准；请审阅替代研究")
        row.approved_hash = (run.result or {}).get("input_hash", "")
    row.status = data.status
    row.reviewer = user.name
    row.review_note = data.review_note
    log(db, user.email, "replication." + data.status, row.id)
    db.commit()
    return record(row)
