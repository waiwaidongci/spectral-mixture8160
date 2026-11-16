from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..config.database import get_db
from ..controllers.dependencies import current_user, roles
from ..constants.roles import Role
from ..schemas.source import ReplacementInput, RevokeInput, SourceOutput
from ..services.source_service import (
    impact,
    list_sources,
    register_replacement,
    revoke_source,
)

router = APIRouter(tags=["sources"])


@router.get("/samples/{sid}/sources", response_model=list[SourceOutput])
def sources(sid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    return list_sources(db, sid)


@router.post("/samples/{sid}/sources/{source_id}/revoke", response_model=SourceOutput)
def revoke(
    sid: int,
    source_id: int,
    data: RevokeInput,
    db: Session = Depends(get_db),
    user=Depends(roles(Role.ADMIN, Role.ANALYST)),
):
    source, _ = revoke_source(db, sid, source_id, data.reason, user)
    return source


@router.post("/samples/{sid}/sources/replacement", response_model=SourceOutput, status_code=201)
def replacement(
    sid: int,
    data: ReplacementInput,
    db: Session = Depends(get_db),
    user=Depends(roles(Role.ADMIN, Role.ANALYST)),
):
    return register_replacement(db, sid, data.observed, data.reason, user)


@router.get("/source-impact")
def source_impact(
    library_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    return impact(db, library_id)
