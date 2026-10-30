#!/usr/bin/env python3
"""
Remove superseded bill chunks from the bills index.

chunk_bills.py indexes one document per bill (the Act, or the latest version)
plus its memo, and each document's chunks have their own record_id. When a bill
moves on to a new version, the new version's chunks get a new record_id and the
old ones would stay in the index. This deletes every chunk whose record_id isn't
in the current chunk file.

Run after embed_and_index.py has indexed the current chunk file. Elasticsearch
credentials come from ES_HOST / ES_USER / ES_PASS (as in .env).

Usage
-----
    python prune_bills.py --chunks bills_chunks.jsonl --index oireachtas-bills-v1 --dry-run
    python prune_bills.py --chunks bills_chunks.jsonl --index oireachtas-bills-v1
"""

import argparse
import json
import logging
import os
import sys

from elasticsearch import Elasticsearch

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)


def indexed_record_ids(es: Elasticsearch, index: str) -> set[str]:
    ids: set[str] = set()
    after = None
    while True:
        comp = {"size": 5000, "sources": [{"r": {"terms": {"field": "record_id"}}}]}
        if after:
            comp["after"] = after
        resp = es.search(index=index, size=0, aggs={"ids": {"composite": comp}})
        agg = resp["aggregations"]["ids"]
        ids.update(b["key"]["r"] for b in agg["buckets"])
        after = agg.get("after_key")
        if not agg["buckets"] or not after:
            return ids


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chunks", default="bills_chunks.jsonl")
    parser.add_argument("--index", default="oireachtas-bills-v1")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    current: set[str] = set()
    with open(args.chunks, encoding="utf-8") as f:
        for line in f:
            current.add(json.loads(line)["record_id"])
    if not current:
        sys.exit("Chunk file is empty; refusing to prune everything")

    es_kwargs = {"hosts": [os.environ.get("ES_HOST", "http://localhost:9200")], "request_timeout": 60}
    if os.environ.get("ES_USER") and os.environ.get("ES_PASS"):
        es_kwargs["basic_auth"] = (os.environ["ES_USER"], os.environ["ES_PASS"])
    es = Elasticsearch(**es_kwargs)

    stale = sorted(indexed_record_ids(es, args.index) - current)
    log.info(f"{len(current)} current documents, {len(stale)} superseded in {args.index}")
    if args.dry_run or not stale:
        return
    deleted = 0
    for i in range(0, len(stale), 1000):
        resp = es.delete_by_query(index=args.index, refresh=True,
                                  query={"terms": {"record_id": stale[i:i + 1000]}})
        deleted += resp.get("deleted", 0)
    log.info(f"Deleted {deleted} chunks")


if __name__ == "__main__":
    main()
