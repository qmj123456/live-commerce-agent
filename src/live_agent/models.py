import time
from typing import Literal

from pydantic import BaseModel, Field


class Comment(BaseModel):
    id: str
    user: str
    text: str
    kind: str = ""
    observed_at: float = Field(default_factory=time.time)


class Decision(BaseModel):
    action: Literal["reply", "bump", "escalate", "ignore"] = "escalate"
    text: str = Field(default="", max_length=160)
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class Candidate(BaseModel):
    id: str
    comment: Comment
    decision: Decision
    product_id: str = ""
    created_at: float = Field(default_factory=time.time)
    status: str = "pending"
    approved: bool = False
