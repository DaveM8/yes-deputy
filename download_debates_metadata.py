#!/usr/bin/env python3
"""
Download metadata for all Dáil (or Seanad) debates from the Oireachtas API.

Saves one JSON record per sitting day to a JSONL file. Each line is the full
debateRecord object from the API, with a top-level `date` field added for
convenience.

Idempotent  — already-fetched dates are skipped on re-run.
Resumable   — interrupted runs can be safely continued.
Incremental — without --from-date, picks up from the latest date already saved.

Usage
-----
    # Full historical download (Dáil, 1919 → today):
    python download_debates_metadata.py --from-date 1919-01-01

    # Incremental update (auto-detects latest saved date):
    python download_debates_metadata.py

    # Seanad instead of Dáil:
    python download_debates_metadata.py --chamber seanad --output seanad_debates.jsonl

    # Preview without writing:
    python download_debates_metadata.py --dry-run

Output fields per line (debateRecord keys)
------------------------------------------
    date, debateType, uri, lastUpdated, house, chamber,
    counts (debateSectionCount, questionCount, billCount, …),
    formats (xml, pdf, writtens_pdf),
    debateSections (list of section metadata including speakers, debateType, xmlUri)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import aiohttp

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

API_BASE        = "https://api.oireachtas.ie/v1"
PAGE_SIZE       = 50          # max the API supports
REQUEST_TIMEOUT = 30          # seconds per request
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 5           # seconds, multiplied by attempt number
LOG_FILE        = "download_debates_metadata.log"
DEFAULT_OUTPUT  = "debates_metadata.jsonl"
EARLIEST_DATE   = "1919-01-01"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# LOAD EXISTING OUTPUT — for idempotency
# ─────────────────────────────────────────────

def load_existing_dates(output_path: Path) -> set[str]:
    """
    Read an existing JSONL output file and return the set of dates already saved.
    Returns an empty set if the file doesn't exist.
    """
    if not output_path.exists():
        return set()
    dates: set[str] = set()
    with output_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                d = record.get("date")
                if d:
                    dates.add(d)
            except json.JSONDecodeError:
                log.warning(f"Skipping malformed line in {output_path}")
    return dates


def latest_saved_date(output_path: Path) -> date | None:
    """Return the most recent date in the existing output file, or None."""
    dates = load_existing_dates(output_path)
    if not dates:
        return None
    return date.fromisoformat(max(dates))


# ─────────────────────────────────────────────
# FETCH FROM API
# ─────────────────────────────────────────────

async def fetch_page(
    session: aiohttp.ClientSession,
    chamber: str,
    date_start: date,
    date_end: date,
    skip: int,
) -> dict:
    """Fetch one page of debate records. Returns the full API response dict."""
    url = (
        f"{API_BASE}/debates"
        f"?chamber_type=house"
        f"&chamber={chamber}"
        f"&date_start={date_start}"
        f"&date_end={date_end}"
        f"&limit={PAGE_SIZE}"
        f"&skip={skip}"
    )
    headers = {"accept": "application/json"}

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                log.warning(f"HTTP {resp.status} attempt {attempt}/{RETRY_ATTEMPTS}: {url}")
        except asyncio.TimeoutError:
            log.warning(f"Timeout attempt {attempt}/{RETRY_ATTEMPTS}: {url}")
        except aiohttp.ClientError as e:
            log.warning(f"Client error attempt {attempt}/{RETRY_ATTEMPTS}: {e}")

        if attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_DELAY * attempt)

    raise RuntimeError(f"Failed to fetch page after {RETRY_ATTEMPTS} attempts: {url}")


async def fetch_all_records(
    session: aiohttp.ClientSession,
    chamber: str,
    date_start: date,
    date_end: date,
) -> list[dict]:
    """
    Paginate through the API and return all debate records in [date_start, date_end].
    Each record has 'date' added as a top-level key for convenience.
    """
    records: list[dict] = []
    skip = 0
    total: int | None = None

    while True:
        data = await fetch_page(session, chamber, date_start, date_end, skip)

        if total is None:
            total = data["head"]["counts"]["resultCount"]
            log.info(f"  Total records in range: {total:,}")

        batch = data.get("results", [])
        if not batch:
            break

        for result in batch:
            dr = result.get("debateRecord", {})
            # Add date at top level (also present inside debateRecord)
            dr["date"] = dr.get("date") or result.get("contextDate", "")
            records.append(dr)

        skip += len(batch)
        log.info(f"  Fetched {skip:,} / {total:,}")

        if skip >= total:
            break

        # Polite pause between pages
        await asyncio.sleep(0.2)

    return records


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

async def run(
    chamber: str,
    date_start: date,
    date_end: date,
    output_path: Path,
    dry_run: bool,
) -> tuple[int, int]:
    """
    Fetch all debate records in [date_start, date_end], skip already-saved dates,
    and append new records to output_path.
    Returns (new_count, skipped_count).
    """
    existing_dates = load_existing_dates(output_path)
    log.info(f"Already saved: {len(existing_dates):,} dates in {output_path}")

    connector = aiohttp.TCPConnector(limit=3)
    async with aiohttp.ClientSession(connector=connector) as session:
        log.info(f"Fetching {chamber} debates: {date_start} → {date_end}")
        records = await fetch_all_records(session, chamber, date_start, date_end)

    new_records = [r for r in records if r.get("date") not in existing_dates]
    skipped = len(records) - len(new_records)

    log.info(f"Fetched {len(records):,} records — {len(new_records):,} new, {skipped:,} already saved")

    if dry_run:
        log.info("DRY RUN — nothing written")
        if new_records:
            sample = new_records[0]
            log.info(f"  Sample new record: date={sample.get('date')} sections={sample.get('counts', {}).get('debateSectionCount')}")
        return len(new_records), skipped

    if new_records:
        # Sort by date before writing so the file stays in chronological order
        new_records.sort(key=lambda r: r.get("date", ""))
        with output_path.open("a", encoding="utf-8") as f:
            for record in new_records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info(f"Appended {len(new_records):,} records to {output_path}")

    return len(new_records), skipped


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Download Oireachtas debate metadata from the API to JSONL"
    )
    parser.add_argument(
        "--chamber",
        default="dail",
        choices=("dail", "seanad"),
        help="Chamber to fetch (default: dail)",
    )
    parser.add_argument(
        "--from-date",
        default=None,
        help=(
            "Fetch records from this date YYYY-MM-DD. "
            "Default: auto-detect from latest date in output file, "
            f"or {EARLIEST_DATE} if output file does not exist."
        ),
    )
    parser.add_argument(
        "--to-date",
        default=str(date.today()),
        help="Fetch records up to and including this date (default: today)",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output JSONL file (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch from API but do not write to disk",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    date_end    = date.fromisoformat(args.to_date)

    # Determine start date
    if args.from_date:
        date_start = date.fromisoformat(args.from_date)
        log.info(f"Using --from-date: {date_start}")
    else:
        latest = latest_saved_date(output_path)
        if latest is not None:
            # Overlap by one day to catch any same-day updates
            date_start = latest - timedelta(days=1)
            log.info(f"Resuming from latest saved date: {latest} (fetching from {date_start})")
        else:
            date_start = date.fromisoformat(EARLIEST_DATE)
            log.info(f"No existing output — starting from {date_start}")

    if date_start > date_end:
        log.info("Already up to date — nothing to fetch.")
        return

    if args.dry_run:
        log.info("DRY RUN — no files will be written")

    new_count, skipped = asyncio.run(
        run(
            chamber=args.chamber,
            date_start=date_start,
            date_end=date_end,
            output_path=output_path,
            dry_run=args.dry_run,
        )
    )

    log.info("\nDone.")
    log.info(f"  New records written : {new_count:,}")
    log.info(f"  Already saved       : {skipped:,}")
    if new_count > 0 and not args.dry_run:
        log.info(f"  Output file         : {output_path}")


if __name__ == "__main__":
    main()
