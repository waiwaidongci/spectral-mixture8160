from sqlalchemy import String, Text, JSON, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from ..config.database import Base
from ..utils.clock import now


class SpectrumSource(Base):
    """A measured (or replacement) spectral observation for one mixture sample.

    Revocation never deletes the row: the original spectrum stays queryable and
    runs keep their source link, which is what lets the system find every study
    that relied on a contaminated measurement.
    """

    __tablename__ = "spectrum_sources"
    __table_args__ = (
        Index("source_sample_status_idx", "sample_id", "status"),
        Index("source_code_idx", "code"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(160), unique=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("spectral_libraries.id"))
    sample_id: Mapped[int] = mapped_column(ForeignKey("mixture_samples.id"))
    kind: Mapped[str] = mapped_column(String(24), default="measured")
    status: Mapped[str] = mapped_column(String(24), default="active")
    observed: Mapped[list] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text, default="")
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("spectrum_sources.id"), nullable=True, default=None)
    owner: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[str] = mapped_column(String(64), default=now)
    revoked_at: Mapped[str] = mapped_column(String(64), default="")
