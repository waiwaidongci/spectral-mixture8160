from sqlalchemy import String, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from ..config.database import Base
from ..utils.clock import now


class RunSourceLink(Base):
    """The source relationship between a study and the measured spectra it used.

    A link is frozen when the run is enqueued.  Older studies can have their
    links backfilled later; every publish/export check consults these links so
    a backfilled study is controlled exactly like a newly created one.
    """

    __tablename__ = "run_source_links"
    __table_args__ = (
        UniqueConstraint("run_id", "sample_id", name="run_sample_source_unique"),
        Index("run_source_run_idx", "run_id"),
        Index("run_source_source_idx", "source_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("unmix_runs.id"))
    sample_id: Mapped[int] = mapped_column(ForeignKey("mixture_samples.id"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("spectrum_sources.id"), nullable=True, default=None)
    source_code: Mapped[str] = mapped_column(String(160), default="")
    relation: Mapped[str] = mapped_column(String(24), default="snapshot")
    created_at: Mapped[str] = mapped_column(String(64), default=now)
