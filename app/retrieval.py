from __future__ import annotations

import asyncio
import re
import time
import unicodedata

from elasticsearch import AsyncElasticsearch
from openai import AsyncOpenAI

from app.models import SearchFilters, SourceCard

# Reciprocal Rank Fusion constant (standard value from Cormack et al.)
RRF_K = 60
# How many hits each of kNN and BM25 contributes to the fusion
FUSION_DEPTH = 50

# Exact-name fields a member filter matches against: speeches, ministers, questioners
_PERSON_FIELDS = (
    "speaker.keyword",
    "speakers.keyword",
    "answering_person.keyword",
    "asking_persons.keyword",
)

_SOURCE_FIELDS = [
    "text", "date", "house", "heading",
    "answering_person", "answering_role", "department_role",
    "questions", "question_numbers",
    "chunk_type", "table_caption", "chunk_index",
    "speaker", "speaker_role", "section_type", "heading_path", "committee",
]


async def embed_query(openai_client: AsyncOpenAI, query: str) -> list[float]:
    # Truncate to 8191 chars (safe proxy for token limit on short queries)
    truncated = query[:8191]
    response = await openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=[truncated],
    )
    return response.data[0].embedding


def _build_filter(filters: SearchFilters) -> list[dict]:
    clauses: list[dict] = []
    if filters.house:
        clauses.append({"term": {"house": filters.house}})
    if filters.year_from or filters.year_to:
        year_range: dict = {}
        if filters.year_from:
            year_range["gte"] = filters.year_from
        if filters.year_to:
            year_range["lte"] = filters.year_to
        clauses.append({"range": {"year": year_range}})
    if filters.member:
        clauses.append({"bool": {
            "should": [{"term": {f: filters.member}} for f in _PERSON_FIELDS],
            "minimum_should_match": 1,
        }})
    return clauses


def _bm25_query(query: str, *, all_terms: bool = False) -> dict:
    """
    all_terms=True requires every query term in one field. Used when results are
    sorted by date, where score can't push loose single-word matches down.
    """
    return {
        "multi_match": {
            "query": query,
            "fields": [
                "text", "heading^2", "heading_path", "questions",
                "speaker^3", "answering_person^3", "asking_persons^2",
            ],
            "operator": "and" if all_terms else "or",
        }
    }


def _hit_to_source_card(hit: dict, score: float) -> SourceCard:
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
        score=round(score, 4),
        table_caption=src.get("table_caption"),
        speaker=src.get("speaker") or "",
        speaker_role=src.get("speaker_role") or "",
        section_type=src.get("section_type") or "",
        heading_path=src.get("heading_path") or "",
        committee=src.get("committee") or "",
    )


async def _hybrid_search(
    es: AsyncElasticsearch,
    openai_client: AsyncOpenAI,
    query: str,
    *,
    index: str,
    k: int,
    num_candidates: int,
    clauses: list[dict],
) -> list[SourceCard]:
    """kNN and BM25 run concurrently, fused with Reciprocal Rank Fusion."""
    query_vector = await embed_query(openai_client, query)

    knn_block: dict = {
        "field": "embedding",
        "query_vector": query_vector,
        "k": FUSION_DEPTH,
        "num_candidates": max(num_candidates, FUSION_DEPTH),
    }
    if clauses:
        knn_block["filter"] = {"bool": {"filter": clauses}}

    # Words like "Minister" or "said" would otherwise match speaker names and
    # speech prefixes ("THE MINISTER FOR LABOUR: ...") rather than the topic
    keywords = topic_terms(query, None) or query

    knn_resp, bm25_resp = await asyncio.gather(
        es.search(index=index, knn=knn_block, source=_SOURCE_FIELDS, size=FUSION_DEPTH),
        es.search(
            index=index,
            query={"bool": {"must": [_bm25_query(keywords)], "filter": clauses}},
            source=_SOURCE_FIELDS,
            size=FUSION_DEPTH,
        ),
    )

    fused: dict[str, float] = {}
    hits: dict[str, dict] = {}
    for resp in (knn_resp, bm25_resp):
        for rank, hit in enumerate(resp["hits"]["hits"], start=1):
            fused[hit["_id"]] = fused.get(hit["_id"], 0.0) + 1.0 / (RRF_K + rank)
            hits.setdefault(hit["_id"], hit)

    # Normalise so a document ranked first by both searches scores 1.0
    best_possible = 2.0 / (RRF_K + 1)
    ranked = sorted(fused, key=fused.get, reverse=True)[:k]
    return [_hit_to_source_card(hits[i], fused[i] / best_possible) for i in ranked]


async def _recent_search(
    es: AsyncElasticsearch,
    query: str,
    *,
    index: str,
    k: int,
    clauses: list[dict],
    member: str | None,
) -> list[SourceCard]:
    """Newest matching chunks first. The query's topic words (if any) must match."""
    topic = topic_terms(query, member)
    bool_query: dict = {"filter": clauses}
    if topic:
        bool_query["must"] = [_bm25_query(topic, all_terms=True)]
    resp = await es.search(
        index=index,
        query={"bool": bool_query},
        sort=[{"date": "desc"}, {"chunk_index": "asc"}, "_score"],
        source=_SOURCE_FIELDS,
        size=k,
    )
    return [_hit_to_source_card(h, 1.0) for h in resp["hits"]["hits"]]


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
    filters = filters or SearchFilters()
    clauses = _build_filter(filters)
    if filters.sort == "date":
        return await _recent_search(
            es, query, index=index, k=k, clauses=clauses, member=filters.member,
        )
    return await _hybrid_search(
        es, openai_client, query,
        index=index, k=k, num_candidates=num_candidates, clauses=clauses,
    )


# ─────────────────────────────────────────────
# Query understanding: member names and recency
# ─────────────────────────────────────────────

_RECENCY_RE = re.compile(r"\b(last|latest|most recent(ly)?|recently|newest)\b", re.IGNORECASE)

# Words that describe the act of speaking or the question itself, not its topic.
# Removed before requiring a topic match in date-sorted searches.
_NON_TOPIC_WORDS = {
    "a", "about", "after", "an", "and", "any", "are", "as", "at", "before", "by", "can",
    "date", "day", "did", "do", "does", "for", "from", "has", "have", "he", "her", "his",
    "how", "i", "in", "is", "it", "last", "latest", "me", "mention", "mentioned", "most",
    "newest", "of", "on", "or", "recent", "recently", "said", "say", "says", "she", "show",
    "speak", "speaks", "spoke", "spoken", "statement", "statements", "talk", "talked", "tell", "that",
    "the", "their", "them", "they", "this", "time", "to", "was", "were", "what", "when",
    "where", "which", "who", "with", "anything", "something",
    "dail", "seanad", "oireachtas", "house", "chamber", "committee",
    "deputy", "senator", "minister", "taoiseach", "td",
}


def _fold(text: str) -> str:
    """Lower-case and strip accents so 'Micheal' matches 'Micheál'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


_WORD_RE = re.compile(r"[\w'’-]+")


def wants_recent(query: str) -> bool:
    return bool(_RECENCY_RE.search(query))


def topic_terms(query: str, member: str | None) -> str:
    """The query minus the member's name and non-topic words, e.g. 'housing'."""
    member_words = set(_WORD_RE.findall(_fold(member))) if member else set()
    words = [
        w for w in _WORD_RE.findall(query)
        if _fold(w) not in _NON_TOPIC_WORDS and _fold(w) not in member_words
    ]
    return " ".join(words)


class MemberIndex:
    """
    Distinct person names in the index, for spotting a full name in a query.

    Names are loaded with terms aggregations and refreshed at most once per
    `ttl` seconds, so people added by the nightly pipeline appear without a restart.
    Only multi-word names are matched, so a bare surname ("Martin") never
    triggers a filter.
    """

    def __init__(self, ttl: float = 3600):
        self.ttl = ttl
        self._loaded_at = 0.0
        self._by_folded: dict[str, str] = {}
        self._max_words = 1

    async def refresh(self, es: AsyncElasticsearch, index: str) -> None:
        aggs = {
            f: {"terms": {"field": f, "size": 65000}}
            for f in ("speaker.keyword", "answering_person.keyword", "asking_persons.keyword")
        }
        resp = await es.search(index=index, size=0, aggs=aggs)
        by_folded: dict[str, str] = {}
        for agg in resp["aggregations"].values():
            for bucket in agg["buckets"]:
                name = bucket["key"].strip()
                if len(name.split()) >= 2:
                    by_folded.setdefault(" ".join(_WORD_RE.findall(_fold(name))), name)
        self._by_folded = by_folded
        self._max_words = max((len(n.split()) for n in by_folded), default=1)
        self._loaded_at = time.monotonic()

    async def ensure_fresh(self, es: AsyncElasticsearch, index: str) -> None:
        if time.monotonic() - self._loaded_at > self.ttl:
            await self.refresh(es, index)

    def find(self, query: str) -> str | None:
        """Longest full name appearing as consecutive words in the query."""
        words = _WORD_RE.findall(_fold(query))
        for n in range(min(self._max_words, len(words)), 1, -1):
            for i in range(len(words) - n + 1):
                name = self._by_folded.get(" ".join(words[i:i + n]))
                if name:
                    return name
        return None
