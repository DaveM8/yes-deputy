from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class SearchFilters(BaseModel):
    house: str | None = None
    year_from: int | None = None
    year_to: int | None = None


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = Field(default_factory=list)
    model_key: str = "claude-haiku"
    api_key: str | None = None  # BYOK — never stored server-side
    filters: SearchFilters = Field(default_factory=SearchFilters)


class SourceCard(BaseModel):
    chunk_id: str
    chunk_type: Literal["answer_text", "table"]
    date: str
    house: str
    heading: str
    answering_person: str
    answering_role: str
    department_role: str
    questions: list[str]
    question_numbers: list[int]
    excerpt: str          # first 300 chars — for card display only
    text: str             # full chunk text — injected into system prompt
    score: float          # cosine similarity score
    table_caption: str | None = None
