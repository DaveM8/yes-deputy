from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


# Size limits bound what one request can cost on the server's LLM keys.
# An assistant answer is capped at 1,500 tokens (about 7,500 characters).
MAX_MESSAGE_CHARS = 4_000
MAX_HISTORY_CHARS = 10_000
MAX_HISTORY_MESSAGES = 24


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=MAX_HISTORY_CHARS)


class SearchFilters(BaseModel):
    house: str | None = Field(default=None, max_length=50)
    year_from: int | None = None
    year_to: int | None = None
    member: str | None = Field(default=None, max_length=200)  # exact name: speaker, minister or questioner
    sort: Literal["relevance", "date"] = "relevance"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    history: list[ChatMessage] = Field(default_factory=list, max_length=MAX_HISTORY_MESSAGES)
    model_key: str = Field(default="deepseek-flash", max_length=50)
    api_key: str | None = Field(default=None, max_length=500)  # BYOK — never stored server-side
    filters: SearchFilters = Field(default_factory=SearchFilters)


class SourceCard(BaseModel):
    chunk_id: str
    chunk_type: Literal["answer_text", "table", "speech", "bill_text", "act_text", "bill_memo"]
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
    # Speech chunks (debates, committees); empty for written/oral answers
    speaker: str = ""
    speaker_role: str = ""
    section_type: str = ""
    heading_path: str = ""
    committee: str = ""
    # Bills and Acts (chunk_type bill_text / act_text / bill_memo)
    version_label: str = ""
    bill_status: str = ""
    sponsors: list[str] = []
    pdf_url: str = ""
    page: int | None = None
