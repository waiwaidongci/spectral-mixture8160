from fastapi import HTTPException

from ..constants.status import SourceStatus
from ..models import MixtureSample, SpectralLibrary, SpectrumSource
from ..utils.serialize import record
from .audit_service import log
from .source_service import active_source, ensure_sample_sources, serialize_source


def list_samples(db, lid):
    if not db.get(SpectralLibrary, lid):
        raise HTTPException(404, "光谱库不存在")
    ensure_sample_sources(db)
    db.commit()
    rows = []
    for sample in (
        db.query(MixtureSample).filter_by(library_id=lid).order_by(MixtureSample.code)
    ):
        data = record(sample)
        sources = (
            db.query(SpectrumSource)
            .filter_by(sample_id=sample.id)
            .order_by(SpectrumSource.id)
            .all()
        )
        current = active_source(db, sample.id)
        revoked = [s for s in sources if s.status == SourceStatus.REVOKED]
        data.update(
            {
                "active_source_id": current.id if current else None,
                "active_source_code": current.code if current else "",
                "source_count": len(sources),
                "revoked_source_count": len(revoked),
                "sources_revoked": bool(revoked) and current is None,
                "sources": [serialize_source(s) for s in sources],
            }
        )
        rows.append(data)
    return rows


def annotate_sample(db, sid, data, user):
    row = db.get(MixtureSample, sid)
    if not row:
        raise HTTPException(404, "样本不存在")
    row.note = data.note
    log(db, user.email, "sample.annotate", row.code)
    db.commit()
    return record(row)
