#!/usr/bin/env python3
"""
Embed chunks from chunks.jsonl using OpenAI text-embedding-3-small,
then bulk-index into Elasticsearch with a dense_vector field for kNN search.

Features
--------
- Idempotent: skips chunks whose _id already exists in the index
- Batched OpenAI API calls (EMBED_BATCH_SIZE texts per request)
- Batched Elasticsearch bulk indexing
- Automatic exponential-backoff retry on rate-limit / transient errors
- Progress logged every PROGRESS_EVERY chunks

Usage
-----
    python embed_and_index.py \
        --chunks chunks.jsonl \
        --es-host http://localhost:9200 \
        --es-user elastic \
        --es-pass SECRET \
        --openai-key sk-... \
        --index oireachtas-chunks

    # Create/recreate the index mapping only, don't index docs:
    python embed_and_index.py ... --setup-only

    # Resume after interruption (skips already-indexed IDs automatically):
    python embed_and_index.py ... --index oireachtas-chunks
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch.helpers import BulkIndexError, bulk
import tiktoken
from openai import OpenAI, RateLimitError, APIStatusError

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

EMBED_BATCH_SIZE = 200        # texts per OpenAI API call
MAX_TOKENS       = 8191       # text-embedding-3-small hard limit (8192), leave 1 for safety
_TOKENIZER       = tiktoken.get_encoding("cl100k_base")  # tokenizer used by text-embedding-3-small
ES_BATCH_SIZE    = 400        # docs per Elasticsearch bulk request
PROGRESS_EVERY   = 10_000    # log progress every N chunks processed
MODEL            = "text-embedding-3-small"
DIMS             = 1536
LOG_FILE         = "embed_and_index.log"
MAX_RETRIES      = 6
RETRY_BASE_DELAY = 2.0        # seconds; doubles each attempt (2,4,8,16,32,64)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# ELASTICSEARCH INDEX MAPPING
# ─────────────────────────────────────────────

INDEX_MAPPING = {
    "settings": {
        "number_of_shards":   1,
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "irish_english": {
                    "type":      "standard",
                    "stopwords": "_english_"
                }
            }
        }
    },
    "mappings": {
        # Vectors are indexed but not stored again in _source: that roughly halves
        # per-doc storage. Consequence: _reindex can't copy embeddings out of an
        # index built with this mapping — keep the chunk files to re-embed.
        "_source": {"excludes": ["embedding"]},
        "properties": {
            # ── Chunk identity ─────────────────────────────
            "id": {
                "type": "keyword"
            },
            "record_id": {
                "type": "keyword"
            },
            "chunk_index": {
                "type": "integer"
            },
            "chunk_type": {
                "type": "keyword"       # "answer_text" | "table" | "speech"
            },
            # ── Debate metadata ────────────────────────────
            "date": {
                "type":   "date",
                "format": "yyyy-MM-dd"
            },
            "year": {
                "type": "integer"
            },
            "house": {
                "type": "keyword"       # "Dáil" | "Seanad" | "Committee"
            },
            "committee": {
                "type": "keyword"       # committee code from the path; "" for Dáil/Seanad
            },
            "doc_type": {
                "type": "keyword"       # "debate" | "writtens"
            },
            "section_type": {
                "type": "keyword"       # debateSection name: debate, questions, motion, ...
            },
            "heading_path": {
                "type":   "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 1024}}
            },
            "heading": {
                "type":   "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}
            },
            "section_id": {
                "type": "keyword"
            },
            "department_role": {
                "type":   "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}
            },
            "answering_person": {
                "type":   "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}
            },
            "answering_role": {
                "type":   "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}
            },
            "asking_persons": {
                "type":   "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}
            },
            # ── Speech metadata (chunk_type "speech") ──────
            "speaker": {
                "type":   "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}
            },
            "speaker_role": {
                "type":   "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}
            },
            "speakers": {
                "type":   "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}
            },
            "question_numbers": {
                "type": "integer"
            },
            "questions": {
                "type":     "text",
                "analyzer": "irish_english"
            },
            # ── Chunk content ──────────────────────────────
            "text": {
                "type":     "text",
                "analyzer": "irish_english"
            },
            "table_caption": {
                "type":     "text",
                "analyzer": "irish_english"
            },
            "table_headers": {
                "type": "keyword"
            },
            # ── Vector ────────────────────────────────────
            "embedding": {
                "type":       "dense_vector",
                "dims":       DIMS,
                "index":      True,
                "similarity": "cosine"
            }
        }
    }
}


# ─────────────────────────────────────────────
# ELASTICSEARCH HELPERS
# ─────────────────────────────────────────────

def create_index(es: Elasticsearch, index_name: str, recreate: bool = False):
    exists = es.indices.exists(index=index_name)
    if exists:
        if recreate:
            log.info(f"Deleting existing index: {index_name}")
            es.indices.delete(index=index_name)
        else:
            log.info(f"Index already exists: {index_name} (use --recreate to rebuild)")
            return
    log.info(f"Creating index: {index_name}")
    es.indices.create(index=index_name, body=INDEX_MAPPING)
    log.info("Index created successfully.")


def load_existing_ids(es: Elasticsearch, index_name: str) -> set[str]:
    """
    Scroll through the index to collect all existing chunk IDs.
    Used to skip already-embedded chunks on resume.
    """
    log.info(f"Loading existing document IDs from {index_name} ...")
    existing: set[str] = set()
    try:
        resp = es.search(
            index=index_name,
            body={"query": {"match_all": {}}, "_source": False, "size": 10_000},
            scroll="2m",
        )
        scroll_id = resp["_scroll_id"]
        hits = resp["hits"]["hits"]
        while hits:
            for h in hits:
                existing.add(h["_id"])
            resp = es.scroll(scroll_id=scroll_id, scroll="2m")
            scroll_id = resp["_scroll_id"]
            hits = resp["hits"]["hits"]
        es.clear_scroll(scroll_id=scroll_id)
    except Exception as e:
        log.warning(f"Could not load existing IDs (index may be empty): {e}")
    log.info(f"Found {len(existing):,} already-indexed chunks.")
    return existing


def flush_to_es(es: Elasticsearch, docs: list[dict], index_name: str) -> int:
    """Bulk-index docs. Returns number of failures."""
    actions = [
        {"_index": index_name, "_id": d["id"], "_source": d}
        for d in docs
    ]
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            success, failed = bulk(
                es, actions,
                raise_on_error=False,
                raise_on_exception=False,
            )
            if failed:
                log.warning(f"  {len(failed)} docs failed in ES bulk request")
                for item in failed[:3]:
                    log.warning(f"  {json.dumps(item)[:200]}")
            return len(failed)
        except BulkIndexError as e:
            log.warning(f"  BulkIndexError attempt {attempt}/{MAX_RETRIES}: {e}")
        except Exception as e:
            log.warning(f"  ES batch error attempt {attempt}/{MAX_RETRIES}: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BASE_DELAY * attempt)
    log.error(f"  ES batch failed after {MAX_RETRIES} attempts, skipping {len(docs)} docs")
    return len(docs)


# ─────────────────────────────────────────────
# OPENAI EMBEDDING
# ─────────────────────────────────────────────

def _truncate_to_tokens(text: str) -> str:
    tokens = _TOKENIZER.encode(text)
    if len(tokens) <= MAX_TOKENS:
        return text
    return _TOKENIZER.decode(tokens[:MAX_TOKENS])


def embed_texts(client: OpenAI, texts: list[str]) -> list[list[float]]:
    """
    Embed a batch of texts. Retries on rate-limit with exponential backoff.
    Returns list of embedding vectors (same order as input).
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.embeddings.create(model=MODEL, input=texts)
            # API returns embeddings sorted by index — sort to be safe
            items = sorted(resp.data, key=lambda x: x.index)
            return [item.embedding for item in items]
        except RateLimitError as e:
            # Also a 429, but retrying can't help: the account has no credit
            if getattr(e, "code", None) == "insufficient_quota":
                raise RuntimeError(
                    "OpenAI account has no credit remaining. Add credit, then rerun: "
                    "already-indexed chunks are skipped."
                ) from e
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            log.warning(f"  Rate limit (attempt {attempt}/{MAX_RETRIES}), sleeping {delay:.0f}s: {e}")
            time.sleep(delay)
        except APIStatusError as e:
            if e.status_code >= 500:
                delay = RETRY_BASE_DELAY * attempt
                log.warning(f"  OpenAI server error {e.status_code} (attempt {attempt}/{MAX_RETRIES}), "
                            f"sleeping {delay:.0f}s")
                time.sleep(delay)
            else:
                raise
    raise RuntimeError(f"OpenAI embedding failed after {MAX_RETRIES} attempts")


# ─────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────

def run(
    chunks_path: Path,
    es: Elasticsearch,
    openai_client: OpenAI,
    index_name: str,
    existing_ids: set[str],
):
    total_lines    = 0
    skipped        = 0
    embedded       = 0
    es_errors      = 0

    embed_queue: list[dict]  = []   # chunks waiting to be embedded
    es_queue:    list[dict]  = []   # chunks with embeddings, waiting ES flush

    start = time.time()

    def flush_embed_queue():
        nonlocal embedded, es_errors
        if not embed_queue:
            return
        texts  = [_truncate_to_tokens(c["text"]) for c in embed_queue]
        vectors = embed_texts(openai_client, texts)
        for chunk, vec in zip(embed_queue, vectors):
            chunk["embedding"] = vec
            es_queue.append(chunk)
        embedded += len(embed_queue)
        embed_queue.clear()
        flush_es_queue()

    def flush_es_queue():
        nonlocal es_errors
        if len(es_queue) >= ES_BATCH_SIZE:
            batch = es_queue[:ES_BATCH_SIZE]
            del es_queue[:ES_BATCH_SIZE]
            es_errors += flush_to_es(es, batch, index_name)

    def flush_all():
        nonlocal es_errors
        flush_embed_queue()
        if es_queue:
            es_errors += flush_to_es(es, es_queue, index_name)
            es_queue.clear()

    with open(chunks_path, "r", encoding="utf-8") as f:
        for line in f:
            total_lines += 1
            line = line.strip()
            if not line:
                continue

            try:
                chunk = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning(f"Bad JSON on line {total_lines}: {e}")
                continue

            if chunk["id"] in existing_ids:
                skipped += 1
                continue

            embed_queue.append(chunk)
            existing_ids.add(chunk["id"])  # skip duplicate lines later in the same file

            if len(embed_queue) >= EMBED_BATCH_SIZE:
                flush_embed_queue()

            if total_lines % PROGRESS_EVERY == 0:
                elapsed = time.time() - start
                rate    = (embedded + skipped) / elapsed if elapsed else 0
                log.info(
                    f"  Line {total_lines:,} | embedded={embedded:,} "
                    f"skipped={skipped:,} es_errors={es_errors} "
                    f"rate={rate:.0f}/s"
                )

    flush_all()

    elapsed = time.time() - start
    log.info(f"\nDone in {elapsed/60:.1f} min")
    log.info(f"  Lines read  : {total_lines:,}")
    log.info(f"  Skipped     : {skipped:,} (already indexed)")
    log.info(f"  Embedded    : {embedded:,}")
    log.info(f"  ES errors   : {es_errors}")

    try:
        final = es.count(index=index_name)["count"]
        log.info(f"  Index total : {final:,} documents")
    except Exception:
        pass


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Embed chunks.jsonl and index into Elasticsearch with dense_vector"
    )
    parser.add_argument("--chunks",      default="chunks.jsonl",
                        help="Path to chunks JSONL file (default: chunks.jsonl)")
    parser.add_argument("--es-host",     default="http://localhost:9200",
                        help="Elasticsearch host (default: http://localhost:9200)")
    parser.add_argument("--es-user",     default=None, help="Elasticsearch username")
    parser.add_argument("--es-pass",     default=None, help="Elasticsearch password")
    parser.add_argument("--openai-key",  default=None,
                        help="OpenAI API key (default: $OPENAI_API_KEY env var)")
    parser.add_argument("--index",       default="oireachtas-chunks",
                        help="Elasticsearch index name (default: oireachtas-chunks)")
    parser.add_argument("--recreate",    action="store_true",
                        help="Delete and recreate the index before indexing")
    parser.add_argument("--setup-only",  action="store_true",
                        help="Create the index mapping only, then exit")
    args = parser.parse_args()

    chunks_path = Path(args.chunks)
    if not args.setup_only and not chunks_path.exists():
        log.error(f"Chunks file not found: {chunks_path}")
        sys.exit(1)

    # ── Elasticsearch client ───────────────────
    es_kwargs = {
        "hosts":            [args.es_host],
        "request_timeout":  30,
        "http_compress":    True,
    }
    if args.es_user and args.es_pass:
        es_kwargs["basic_auth"] = (args.es_user, args.es_pass)
    es = Elasticsearch(**es_kwargs)

    try:
        info = es.info()
        log.info(f"Connected to Elasticsearch: cluster={info['cluster_name']}, "
                 f"version={info['version']['number']}")
    except Exception as e:
        log.error(f"Cannot connect to Elasticsearch at {args.es_host}: {e}")
        sys.exit(1)

    create_index(es, args.index, recreate=args.recreate)

    if args.setup_only:
        log.info("--setup-only: index created, exiting.")
        return

    # ── OpenAI client ──────────────────────────
    import os
    api_key = args.openai_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.error("OpenAI API key required: use --openai-key or set $OPENAI_API_KEY")
        sys.exit(1)
    openai_client = OpenAI(api_key=api_key)

    # ── Load existing IDs for resume ───────────
    existing_ids = load_existing_ids(es, args.index)

    # ── Run ────────────────────────────────────
    log.info(f"Starting embedding + indexing of {chunks_path}")
    log.info(f"  Model         : {MODEL} ({DIMS} dims)")
    log.info(f"  Embed batch   : {EMBED_BATCH_SIZE}")
    log.info(f"  ES batch      : {ES_BATCH_SIZE}")
    log.info(f"  Target index  : {args.index}")

    run(chunks_path, es, openai_client, args.index, existing_ids)


if __name__ == "__main__":
    main()
