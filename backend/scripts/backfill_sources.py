"""Idempotent lineage backfill for databases created before source tracking.

Safe to run repeatedly: it only adds one measured source for samples that have
none and one link for (run, sample) pairs that have none.  It never rewrites an
existing observation, revocation, run result or review.

It also binds legacy approvals (made before approval-hash binding existed) to
the archived result hash they reviewed, so a later recomputation cannot ride on
an old approval.
"""
from sqlalchemy import inspect, text

from src.config.database import Base, engine, SessionLocal
from src.models import ReplicationNote, UnmixRun
from src.services.replacement_service import ensure_replacements
from src.services.source_service import control_for, ensure_all_links

# New scalar columns on pre-existing tables, with the DDL used by both MySQL
# and SQLite.  (The reviewed MySQL migration is migrations/002_source_control.sql;
# this keeps `create_all` from silently leaving an older database unusable.)
ADDED_COLUMNS = {
    "unmix_runs": {
        "job_token": "VARCHAR(64) NOT NULL DEFAULT ''",
        "updated_at": "VARCHAR(64) NOT NULL DEFAULT ''",
    },
    "replication_notes": {"approved_hash": "VARCHAR(64) NOT NULL DEFAULT ''"},
}


def ensure_columns():
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, columns in ADDED_COLUMNS.items():
            if table not in tables:
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    )


def backfill():
    Base.metadata.create_all(engine)
    ensure_columns()
    with SessionLocal() as db:
        ensure_all_links(db)
        for run in db.query(UnmixRun).all():
            control_for(db, run.id, create=True)
        db.commit()
    with SessionLocal() as db:
        for note in db.query(ReplicationNote).filter_by(status="approved").all():
            if note.approved_hash:
                continue
            run = db.get(UnmixRun, note.run_id)
            if run and run.result:
                note.approved_hash = run.result.get("input_hash", "")
        db.commit()
    # Only creates missing replacements; revoked studies that already have one
    # are left untouched.
    ensure_replacements(None, "migration.backfill")


if __name__ == "__main__":
    backfill()
