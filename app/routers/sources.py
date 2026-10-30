"""
Full text of one source chunk, with the chunks around it, for the
"Read full text" page (static/source.html).
"""
from __future__ import annotations

import hashlib
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.auth import get_current_user

router = APIRouter()

_CHUNK_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_SECTION_NUM_RE = re.compile(r"dbsect_(\d+)$")
_MAX_CONTEXT = 20

_FIELDS = [
    "text", "date", "house", "committee", "heading", "heading_path",
    "section_id", "section_type", "chunk_type", "chunk_index", "record_id",
    "speaker", "speaker_role", "answering_person", "answering_role",
    "department_role", "questions", "question_numbers", "table_caption",
    "version_label", "bill_status", "sponsors", "pdf_url", "page", "bill_uri",
]


def _chunk_id(record_id: str, chunk_index: int) -> str:
    """Same formula as chunk_for_rag.make_chunk_id (not imported: it pulls in lxml)."""
    return hashlib.sha256(f"{record_id}|{chunk_index}".encode()).hexdigest()[:16]


def _oireachtas_url(src: dict) -> str | None:
    """Link to the section on oireachtas.ie, or None if it can't be built."""
    bill_uri = src.get("bill_uri") or ""
    if bill_uri.startswith("https://data.oireachtas.ie/ie/oireachtas/bill/"):
        year_no = bill_uri.rstrip("/").rsplit("/", 2)[-2:]
        return f"https://www.oireachtas.ie/en/bills/bill/{year_no[0]}/{year_no[1]}/"
    date = src.get("date")
    if not date:
        return None
    numbers = src.get("question_numbers") or []
    if numbers:
        return f"https://www.oireachtas.ie/en/debates/question/{date}/{numbers[0]}/"
    chamber = src.get("committee") or {"Dáil": "dail", "Seanad": "seanad"}.get(src.get("house", ""))
    if not chamber:
        return None
    m = _SECTION_NUM_RE.search(src.get("section_id") or "")
    if m:
        return f"https://www.oireachtas.ie/en/debates/debate/{chamber}/{date}/{m.group(1)}/"
    return f"https://www.oireachtas.ie/en/debates/debate/{chamber}/{date}/"


def _public(chunk_id: str, src: dict) -> dict:
    out = {k: src.get(k) for k in _FIELDS if k != "record_id"}
    out["chunk_id"] = chunk_id
    return out


@router.get("/api/sources/{chunk_id}")
async def get_source(
    chunk_id: str,
    request: Request,
    _user: Annotated[dict, Depends(get_current_user)],
    before: Annotated[int, Query(ge=0, le=_MAX_CONTEXT)] = 3,
    after: Annotated[int, Query(ge=0, le=_MAX_CONTEXT)] = 3,
) -> dict:
    if not _CHUNK_ID_RE.match(chunk_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    es = request.app.state.es
    index = request.app.state.settings.es_index

    resp = await es.search(
        index=index, query={"ids": {"values": [chunk_id]}}, source=_FIELDS, size=1,
    )
    hits = resp["hits"]["hits"]
    if not hits:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    src = hits[0]["_source"]

    # Neighbouring chunks of the same section, by recomputing their IDs
    before_chunks: list[dict] = []
    after_chunks: list[dict] = []
    record_id, idx = src.get("record_id"), src.get("chunk_index")
    wanted = []
    if record_id and isinstance(idx, int):
        wanted = [i for i in range(idx - before, idx + after + 1) if i >= 0 and i != idx]
    if wanted:
        ids = {_chunk_id(record_id, i): i for i in wanted}
        # A search rather than mget: mget refuses an alias that spans several
        # indices (debates and bills)
        resp = await es.search(
            index=index, query={"ids": {"values": list(ids)}}, source=_FIELDS, size=len(ids),
        )
        found = sorted(
            (ids[h["_id"]], h["_id"], h["_source"]) for h in resp["hits"]["hits"]
        )
        before_chunks = [_public(cid, s) for i, cid, s in found if i < idx]
        after_chunks = [_public(cid, s) for i, cid, s in found if i > idx]

    return {
        "chunk": _public(chunk_id, src),
        "before": before_chunks,
        "after": after_chunks,
        "oireachtas_url": _oireachtas_url(src),
    }
