from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..config.database import get_db
from ..controllers.dependencies import current_user, roles
from ..constants.roles import Role
from ..schemas.run import RunInput
from ..services.run_service import create_run, export_run, get_run, list_runs
from ..services.source_service import serialize_run

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("")
def index(db: Session = Depends(get_db), user=Depends(current_user)):
    return list_runs(db)


@router.get("/{rid}")
def show(rid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    return serialize_run(db, get_run(db, rid))


@router.post("", status_code=202)
def create(
    data: RunInput,
    db: Session = Depends(get_db),
    user=Depends(roles(Role.ADMIN, Role.ANALYST)),
):
    return create_run(db, data, user)


@router.post("/{rid}/replacement/retry")
def retry_replacement(
    rid: int,
    db: Session = Depends(get_db),
    user=Depends(roles(Role.ADMIN, Role.ANALYST)),
):
    from ..services.replacement_service import retry_replacement as retry

    return retry(db, rid, user)


@router.get("/{rid}/export")
def export(rid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    return Response(
        content="﻿" + export_run(db, rid),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="spectral-run-{rid}.csv"'},
    )
