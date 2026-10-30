#!/usr/bin/env python3
"""
Download bill metadata and PDFs from the Oireachtas API.

Bills, Acts, explanatory memoranda and amendment lists are published as PDF
only (no XML), so this is separate from update_xmls.py.

Strategy
--------
  1. Fetch every bill from api.oireachtas.ie/v1/legislation (1,000 per page,
     about 7 requests) and write bills_metadata.jsonl, one bill per line.
     The whole list is refetched each run: it's small, and bills change status.
  2. Download every English or bilingual PDF the bills reference (versions,
     related docs, amendment lists), skipping files already on disk.

Local path mirrors the URL:
  https://data.oireachtas.ie/ie/oireachtas/bill/2026/102/eng/memo/b10226d-memo.pdf
  -> bills_pdf/ie/oireachtas/bill/2026/102/eng/memo/b10226d-memo.pdf

Usage
-----
    python update_bills.py
    python update_bills.py --out-dir /data/members_questions
    python update_bills.py --limit 200       # first N bills only (trial run)
    python update_bills.py --metadata-only
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

API_URL         = "https://api.oireachtas.ie/v1/legislation"
# Without bill_status the API filters by status, so list every one
BILL_STATUSES   = "Current,Withdrawn,Enacted,Rejected,Defeated,Lapsed"
PAGE_SIZE       = 1000      # the API's maximum
MAX_CONCURRENT  = 8
REQUEST_TIMEOUT = 120
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 5
WANTED_LANGS    = {"eng", "mul", None}   # amendment lists carry no lang
LOG_FILE        = "update_bills.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)


PDF_HOST        = "data.oireachtas.ie"


def pdf_local_path(url: str, pdf_dir: Path) -> Path:
    """
    Local path mirroring the URL. URLs come from the API's JSON, so only
    https://data.oireachtas.ie/... PDFs are accepted, and the path must stay
    inside pdf_dir.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != PDF_HOST or not parsed.path.endswith(".pdf"):
        raise ValueError(f"Unexpected PDF URL: {url!r}")
    local = (pdf_dir / parsed.path.lstrip('/')).resolve()
    if ".." in Path(parsed.path).parts or not local.is_relative_to(pdf_dir.resolve()):
        raise ValueError(f"PDF URL escapes {pdf_dir}: {url!r}")
    return local


def bill_documents(bill: dict) -> list[dict]:
    """Every document a bill references, flattened, as {kind, ...doc}."""
    docs = []
    for kind in ("versions", "relatedDocs", "amendmentLists"):
        for wrapper in bill.get(kind) or []:
            doc = next(iter(wrapper.values()))
            docs.append({"kind": kind, **doc})
    return docs


def pdf_urls(bill: dict) -> list[str]:
    urls = []
    for doc in bill_documents(bill):
        if doc.get("lang") not in WANTED_LANGS:
            continue
        uri = ((doc.get("formats") or {}).get("pdf") or {}).get("uri")
        if not uri:
            continue
        try:
            pdf_local_path(uri, Path("bills_pdf"))
        except ValueError as e:
            log.warning(f"Skipping {e}")
            continue
        urls.append(uri)
    return urls


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict) -> dict:
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                if resp.status == 200:
                    return await resp.json()
                log.warning(f"HTTP {resp.status} attempt {attempt}/{RETRY_ATTEMPTS}: {url} {params}")
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            log.warning(f"Error attempt {attempt}/{RETRY_ATTEMPTS}: {url} {params} — {e}")
        if attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_DELAY * attempt)
    raise RuntimeError(f"Failed to fetch {url} {params}")


async def fetch_all_bills(session: aiohttp.ClientSession) -> list[dict]:
    bills: list[dict] = []
    skip = 0
    while True:
        data = await fetch_json(session, API_URL, {
            "bill_status": BILL_STATUSES, "limit": PAGE_SIZE, "skip": skip,
        })
        page = [r["bill"] for r in data.get("results", [])]
        bills.extend(page)
        total = data["head"]["counts"]["billCount"]
        log.info(f"Fetched {len(bills)}/{total} bills")
        if not page or len(bills) >= total:
            return bills
        skip += PAGE_SIZE


async def download_one(
    session: aiohttp.ClientSession,
    url: str,
    pdf_dir: Path,
    semaphore: asyncio.Semaphore,
    failed: list[str],
) -> bool:
    local_path = pdf_local_path(url, pdf_dir)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    part = local_path.with_suffix(local_path.suffix + ".part")

    async with semaphore:
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                    if resp.status == 200:
                        part.write_bytes(await resp.read())
                        part.rename(local_path)   # never leave a half-written PDF
                        return True
                    if resp.status == 404:
                        log.warning(f"404 NOT FOUND: {url}")
                        failed.append(url)
                        return False
                    log.warning(f"HTTP {resp.status} attempt {attempt}/{RETRY_ATTEMPTS}: {url}")
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                log.warning(f"Error attempt {attempt}/{RETRY_ATTEMPTS}: {url} — {e}")
            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY * attempt)

        log.error(f"FAILED after {RETRY_ATTEMPTS} attempts: {url}")
        failed.append(url)
        return False


async def run(out_dir: Path, limit: int | None, metadata_only: bool) -> int:
    pdf_dir = out_dir / "bills_pdf"
    meta_path = out_dir / "bills_metadata.jsonl"
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT)

    async with aiohttp.ClientSession(connector=connector) as session:
        bills = await fetch_all_bills(session)
        # Newest first from the API; keep that order so --limit tries recent bills
        if limit:
            bills = bills[:limit]

        tmp = meta_path.with_suffix(".jsonl.part")
        with open(tmp, "w", encoding="utf-8") as f:
            for bill in bills:
                f.write(json.dumps(bill, ensure_ascii=False) + "\n")
        tmp.rename(meta_path)
        log.info(f"Wrote {len(bills)} bills to {meta_path}")
        if metadata_only:
            return 0

        urls = list(dict.fromkeys(u for b in bills for u in pdf_urls(b)))
        pending = [u for u in urls if not pdf_local_path(u, pdf_dir).exists()]
        log.info(f"{len(urls)} PDFs referenced, {len(urls) - len(pending)} on disk, "
                 f"{len(pending)} to download")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        failed: list[str] = []
        start = time.time()
        tasks = [download_one(session, u, pdf_dir, semaphore, failed) for u in pending]
        for done, coro in enumerate(asyncio.as_completed(tasks), 1):
            await coro
            if done % 500 == 0:
                rate = done / (time.time() - start)
                log.info(f"Progress: {done}/{len(pending)} rate={rate:.1f}/s "
                         f"ETA={(len(pending) - done) / rate / 60:.1f}min failed={len(failed)}")

    log.info(f"Done: {len(pending) - len(failed)} downloaded, {len(failed)} failed")
    if failed:
        (out_dir / "bills_failed_urls.txt").write_text("\n".join(failed) + "\n")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, default=Path("."),
                        help="Where bills_metadata.jsonl and bills_pdf/ go (default: .)")
    parser.add_argument("--limit", type=int, help="Only the first N bills (newest first)")
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.out_dir, args.limit, args.metadata_only)))


if __name__ == "__main__":
    main()
