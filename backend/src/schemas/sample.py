from pydantic import BaseModel, Field

from .source import SourceOutput


class SampleOutput(BaseModel):
    id: int
    code: str
    library_id: int
    name: str
    observed: list[float]
    note: str
    active_source_id: int | None = None
    active_source_code: str = ""
    source_count: int = 0
    revoked_source_count: int = 0
    sources_revoked: bool = False
    sources: list[SourceOutput] = []


class SampleAnnotation(BaseModel):
    note: str = Field(min_length=8, max_length=1500)
