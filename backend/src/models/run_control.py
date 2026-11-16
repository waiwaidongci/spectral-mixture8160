from sqlalchemy import String, Text, JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from ..config.database import Base
from ..utils.clock import now


class RunControl(Base):
    """Publication gate and replacement bookkeeping for one unmix run."""

    __tablename__ = "run_controls"
    __table_args__ = (
        UniqueConstraint("run_id", name="run_control_run_unique"),
        UniqueConstraint("replacement_of_run_id", name="run_control_one_replacement"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("unmix_runs.id"))
    publish_state: Mapped[str] = mapped_column(String(24), default="active")
    blocked_reason: Mapped[str] = mapped_column(Text, default="")
    # For a replacement run: which original run it supersedes.
    replacement_of_run_id: Mapped[int | None] = mapped_column(ForeignKey("unmix_runs.id"), nullable=True, default=None)
    # For the original run: the single replacement study generated for it.
    replacement_run_id: Mapped[int | None] = mapped_column(ForeignKey("unmix_runs.id"), nullable=True, default=None)
    replacement_state: Mapped[str] = mapped_column(String(24), default="none")
    replacement_error: Mapped[str] = mapped_column(Text, default="")
    # Affected sample ids recomputed by this replacement run; the other samples
    # are copied from the archived original result verbatim.
    replacement_sample_ids: Mapped[list] = mapped_column(JSON, default=list)
    # Stable archived rows available for reuse by later refresh rounds, keyed by
    # string sample id: {"run_id", "source_id", "row"}.  A row is only kept
    # while the observation it was computed from is still the active source, so
    # a polluted archive can never be reused.
    reuse_archive: Mapped[dict] = mapped_column(JSON, default=dict)
    # Results replaced in place by a later refresh round stay queryable here:
    # [{"run_id", "input_hash", "status", "replaced_at", "result"}].
    result_history: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String(64), default=now)
    updated_at: Mapped[str] = mapped_column(String(64), default=now)
