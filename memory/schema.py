import uuid
from datetime import datetime
from pydantic import BaseModel, Field

VALID_KINDS = frozenset({
    "preference", "project", "hardware", "software",
    "workflow", "constraint", "avoid", "general",
})


class Memory(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: str
    topic: str
    content: str
    confidence: float = 0.8
    source: str = "extractor"
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    last_seen_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    embedding: list[float] | None = None
