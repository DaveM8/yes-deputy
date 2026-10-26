#!/usr/bin/env python3
"""
Index Oireachtas written answers Parquet file into Elasticsearch.

Usage:
    python index_elasticsearch.py \
        --parquet /home/dave/gov/members_questions/written_answers.parquet \
        --es-host http://localhost:9200 \
        --index oireachtas-written-answers
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import pyarrow.parquet as pq
from elasticsearch import Elasticsearch
from elasticsearch.helpers import BulkIndexError, bulk

# --- Config ---
BATCH_SIZE  = 500
MAX_RETRIES = 3
RETRY_DELAY = 5
LOG_FILE    = "elasticsearch_index.log"
# --------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# INDEX MAPPING
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
        "properties": {
            "id": {
                "type": "keyword"
            },
            "date": {
                "type":   "date",
                "format": "yyyy-MM-dd"
            },
            "year": {
                "type": "integer"
            },
            "house": {
                "type": "keyword"
            },
            "heading": {
                "type":   "text",
                "fields": {
                    "keyword": {"type": "keyword", "ignore_above": 512}
                }
            },
            "section_id": {
                "type": "keyword"
            },
            "department_role": {
                "type":   "text",
                "fields": {
                    "keyword": {"type": "keyword", "ignore_above": 512}
                }
            },
            "answering_person": {
                "type":   "text",
                "fields": {
                    "keyword": {"type": "keyword", "ignore_above": 256}
                }
            },
            "answering_role": {
                "type":   "text",
                "fields": {
                    "keyword": {"type": "keyword", "ignore_above": 512}
                }
            },
            "question_numbers": {
                "type": "integer"
            },
            "questions": {
                "type": "text",
                "analyzer": "irish_english"
            },
                    "asking_person": {
                        "type":   "text",
                        "fields": {
                            "keyword": {"type": "keyword", "ignore_above": 256}
                        }
                    },
                    "question_clean": {
                        "type":     "text",
                        "analyzer": "irish_english"
                    }
                }
            },
            "answer_clean": {
                "type":     "text",
                "analyzer": "irish_english"
            }
        }
    }
}


# ─────────────────────────────────────────────
# PARQUET -> DOCUMENTS
# ─────────────────────────────────────────────

def row_to_doc(row: dict, index_name: str) -> dict:
    questions = [str(q) for q in (row.get('questions') or []) if q]
        

    question_numbers = [int(n) for n in (row.get('question_numbers') or []) if n is not None]

    doc = {
        'id':               row.get('id', ''),
        'date':             row.get('date', ''),
        'year':             int(row['year']) if row.get('year') is not None else None,
        'house':            row.get('house', ''),
        'heading':          row.get('heading', ''),
        'section_id':       row.get('section_id', ''),
        'department_role':  row.get('department_role', ''),
        'answering_person': row.get('answering_person', ''),
        'answering_role':   row.get('answering_role', ''),
        'question_numbers': question_numbers,
        'questions':        questions,
        'answer_clean':     row.get('answer_clean', ''),
    }

    return {
        '_index':  index_name,
        '_id':     doc['id'],
        '_source': doc,
    }


def parquet_batches(parquet_path: Path, batch_size: int):
    pf = pq.ParquetFile(parquet_path)
    for batch in pf.iter_batches(batch_size=batch_size):
        rows = batch.to_pydict()
        n = len(rows['id'])
        for i in range(n):
            yield {col: rows[col][i] for col in rows}


# ─────────────────────────────────────────────
# ELASTICSEARCH
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
    log.info("Index created.")


def count_existing(es: Elasticsearch, index_name: str) -> int:
    try:
        result = es.count(index=index_name)
        return result['count']
    except Exception:
        return 0


def flush_batch(es: Elasticsearch, docs: list[dict]) -> int:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            success, failed = bulk(
                es,
                docs,
                raise_on_error=False,
                raise_on_exception=False,
            )
            if failed:
                log.warning(f"  {len(failed)} docs failed in batch")
                for item in failed[:3]:
                    log.warning(f"  Failed doc: {json.dumps(item)[:200]}")
            return len(failed)
        except BulkIndexError as e:
            log.warning(f"  BulkIndexError attempt {attempt}/{MAX_RETRIES}: {e}")
        except Exception as e:
            log.warning(f"  Batch error attempt {attempt}/{MAX_RETRIES}: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY * attempt)

    log.error(f"  Batch failed after {MAX_RETRIES} attempts, skipping {len(docs)} docs")
    return len(docs)


def index_documents(es: Elasticsearch, parquet_path: Path, index_name: str, batch_size: int):
    pf         = pq.ParquetFile(parquet_path)
    total_rows = pf.metadata.num_rows
    log.info(f"Parquet contains {total_rows:,} rows")
    log.info(f"Index currently has {count_existing(es, index_name):,} docs")

    completed  = 0
    errors     = 0
    start      = time.time()
    batch_docs = []

    for row in parquet_batches(parquet_path, batch_size=1000):
        batch_docs.append(row_to_doc(row, index_name))

        if len(batch_docs) >= batch_size:
            errors    += flush_batch(es, batch_docs)
            completed += len(batch_docs)
            batch_docs = []

            elapsed   = time.time() - start
            rate      = completed / elapsed if elapsed > 0 else 0
            remaining = (total_rows - completed) / rate if rate > 0 else 0
            log.info(
                f"Indexed: {completed:,}/{total_rows:,} "
                f"({100*completed/total_rows:.1f}%) "
                f"rate={rate:.1f}/s "
                f"ETA={remaining/60:.1f}min "
                f"errors={errors}"
            )

    # Flush remainder
    if batch_docs:
        errors    += flush_batch(es, batch_docs)
        completed += len(batch_docs)

    elapsed     = time.time() - start
    final_count = count_existing(es, index_name)
    log.info(f"Indexing complete in {elapsed:.1f}s")
    log.info(f"Total processed: {completed:,} documents, {errors} errors")
    log.info(f"Index now contains: {final_count:,} documents")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Index Oireachtas Parquet into Elasticsearch')
    parser.add_argument('--parquet',    required=True, help='Path to written_answers.parquet')
    parser.add_argument('--es-host',    default='http://localhost:9200', help='Elasticsearch host')
    parser.add_argument('--index',      default='oireachtas-written-answers', help='Index name')
    parser.add_argument('--batch-size', type=int, default=BATCH_SIZE, help='Docs per bulk request')
    parser.add_argument('--recreate',   action='store_true', help='Delete and recreate index')
    parser.add_argument('--es-user',    default=None, help='Elasticsearch username')
    parser.add_argument('--es-pass',    default=None, help='Elasticsearch password')
    args = parser.parse_args()

    parquet_path = Path(args.parquet)
    if not parquet_path.exists():
        log.error(f"Parquet file not found: {parquet_path}")
        sys.exit(1)

    # Build ES client
    es_kwargs = {
        'hosts':           [args.es_host],
        'request_timeout': 30,
    }
    if args.es_user and args.es_pass:
        es_kwargs['basic_auth'] = (args.es_user, args.es_pass)

    es = Elasticsearch(
        hosts=[args.es_host],
        request_timeout=30,
        http_compress=True,
    )

    # Use info() rather than ping() - ping fails on HTTP 400 even when ES is healthy
    try:
        info = es.info()
        log.info(f"Connected to Elasticsearch at {args.es_host}")
        log.info(f"Cluster: {info['cluster_name']}, version: {info['version']['number']}")
    except Exception as e:
        log.error(f"Cannot connect to Elasticsearch at {args.es_host}: {e}")
        sys.exit(1)

    create_index(es, args.index, recreate=args.recreate)
    index_documents(es, parquet_path, args.index, args.batch_size)


if __name__ == '__main__':
    main()
