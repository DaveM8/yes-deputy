from __future__ import annotations

from elasticsearch import AsyncElasticsearch
from openai import AsyncOpenAI

from app.models import SearchFilters, SourceCard


async def embed_query(openai_client: AsyncOpenAI, query: str) -> list[float]:
    # Truncate to 8191 chars (safe proxy for token limit on short queries)
    truncated = query[:8191]
    response = await openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=[truncated],
    )
    return response.data[0].embedding


def _build_filter(filters: SearchFilters) -> dict | None:
    must_clauses = []
    if filters.house:
        must_clauses.append({"term": {"house.keyword": filters.house}})
    if filters.year_from or filters.year_to:
        year_range: dict = {}
        if filters.year_from:
            year_range["gte"] = filters.year_from
        if filters.year_to:
            year_range["lte"] = filters.year_to
        must_clauses.append({"range": {"year": year_range}})
    if not must_clauses:
        return None
    return {"bool": {"must": must_clauses}}


def _hit_to_source_card(hit: dict) -> SourceCard:
    src = hit["_source"]
    text = src.get("text", "")
    return SourceCard(
        chunk_id=hit["_id"],
        chunk_type=src.get("chunk_type", "answer_text"),
        date=src.get("date", ""),
        house=src.get("house", ""),
        heading=src.get("heading", ""),
        answering_person=src.get("answering_person", ""),
        answering_role=src.get("answering_role", ""),
        department_role=src.get("department_role", ""),
        questions=src.get("questions") or [],
        question_numbers=src.get("question_numbers") or [],
        excerpt=text[:300],
        text=text,
        score=round(hit.get("_score", 0.0), 4),
        table_caption=src.get("table_caption"),
    )


async def retrieve(
    es: AsyncElasticsearch,
    openai_client: AsyncOpenAI,
    query: str,
    *,
    index: str = "oireachtas-chunks",
    k: int = 8,
    num_candidates: int = 100,
    filters: SearchFilters | None = None,
) -> list[SourceCard]:
    query_vector = await embed_query(openai_client, query)

    knn_block: dict = {
        "field": "embedding",
        "query_vector": query_vector,
        "k": k,
        "num_candidates": num_candidates,
    }
    if filters:
        es_filter = _build_filter(filters)
        if es_filter:
            knn_block["filter"] = es_filter

    response = await es.search(
        index=index,
        knn=knn_block,
        source=[
            "text", "date", "house", "heading",
            "answering_person", "answering_role", "department_role",
            "questions", "question_numbers",
            "chunk_type", "table_caption", "chunk_index",
        ],
        size=k,
    )
    return [_hit_to_source_card(h) for h in response["hits"]["hits"]]
