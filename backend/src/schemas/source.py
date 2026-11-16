from pydantic import BaseModel, Field


class RevokeInput(BaseModel):
    reason: str = Field(min_length=8, max_length=1000)


class ReplacementInput(BaseModel):
    observed: list[float] = Field(min_length=2, max_length=64)
    reason: str = Field(min_length=8, max_length=1000)


class SourceOutput(BaseModel):
    id: int
    code: str
    library_id: int
    sample_id: int
    kind: str
    status: str
    active: bool
    observed: list[float]
    reason: str
    supersedes_id: int | None
    owner: str
    created_at: str
    revoked_at: str
