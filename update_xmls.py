#!/usr/bin/env python3
"""
Incrementally download Oireachtas XML files for new Dáil sitting days.

Downloads two document types per sitting day:
  - writtens  → written answers  (questions_xml/.../writtens/mul@/main.xml)
  - debate    → oral questions + full day debate record
                (questions_xml/.../debate/mul@/main.xml)

Strategy
--------
  1. Scan questions_xml/ to find the latest date already downloaded.
  2. Query api.oireachtas.ie for Dáil debate dates after that date up to today.
  3. For each date and each doc type, fetch the main.xml from data.oireachtas.ie.
     (Individual dbsect_N.xml files return 403; main.xml contains all sections.)
  4. Save mirroring the URL path under questions_xml/.

The existing xml_to_parquet.py pipeline handles main.xml files automatically —
it uses rglob('*.xml') and findall('.//{*}debateSection') which work on any file
that contains debate sections, regardless of how many.

Usage
-----
    python update_xmls.py
    python update_xmls.py --from-date 2026-02-20   # override auto-detected date
    python update_xmls.py --xml-dir questions_xml --dry-run
    python update_xmls.py --doc-types writtens      # written answers only
    python update_xmls.py --doc-types debate        # oral questions only
    python update_xmls.py --doc-types writtens debate  # both (default)

Note: 404s on writtens are normal (not every sitting day has written answers
published). 404s on debate are rare but handled gracefully.
"""

import asyncio
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

API_BASE        = "https://api.oireachtas.ie/v1"
DATA_BASE       = "https://data.oireachtas.ie"
REQUEST_TIMEOUT = 60        # seconds — main.xml files can be large
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 5         # seconds, multiplied by attempt number
LOG_FILE        = "update_xmls.log"

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
# STEP 1 — FIND LATEST DOWNLOADED DATE
# ─────────────────────────────────────────────

DATE_RE = re.compile(r'/(\d{4}-\d{2}-\d{2})/')

def find_latest_date(xml_dir: Path, chamber: str) -> date | None:
    """
    Scan the given chamber's subtree for the most recent date embedded in any file path.
    Directory structure: .../debateRecord/{chamber}/YYYY-MM-DD/writtens/...
    Scoped per chamber so one chamber's downloads don't advance another's start date.
    """
    latest = None
    for path in (xml_dir / "akn" / "ie" / "debateRecord" / chamber).rglob('*.xml'):
        m = DATE_RE.search(str(path))
        if m:
            d = date.fromisoformat(m.group(1))
            if latest is None or d > latest:
                latest = d
    return latest


# ─────────────────────────────────────────────
# STEP 2 — DISCOVER NEW SITTING DATES
# ─────────────────────────────────────────────

async def fetch_sitting_dates(
    session: aiohttp.ClientSession,
    after: date,
    up_to: date,
    chamber: str = "dail",
) -> list[date]:
    """
    Query the Oireachtas API for all sitting days for the given chamber in (after, up_to].
    Returns a sorted list of dates that have debate records.
    """
    dates: list[date] = []
    skip = 0
    limit = 50

    # API returns up to `limit` records per page; paginate until exhausted
    while True:
        url = (
            f"{API_BASE}/debates"
            f"?chamber_type=house&chamber={chamber}"
            f"&date_start={after + timedelta(days=1)}"
            f"&date_end={up_to}"
            f"&limit={limit}&skip={skip}"
        )
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    log.error(f"API returned {resp.status} for {url}")
                    break
                data = await resp.json()
        except Exception as e:
            log.error(f"API request failed: {e}")
            break

        results = data.get("results", [])
        for r in results:
            dr = r.get("debateRecord", {})
            date_str = dr.get("date", "")
            if date_str:
                dates.append(date.fromisoformat(date_str))

        count = data["head"]["counts"]["resultCount"]
        skip += len(results)
        if skip >= count or not results:
            break

    dates.sort()
    log.info(f"Found {len(dates)} {chamber} sitting day(s) between "
             f"{after + timedelta(days=1)} and {up_to}")
    return dates


# ─────────────────────────────────────────────
# STEP 3 — DOWNLOAD main.xml FOR EACH DATE
# ─────────────────────────────────────────────

# doc_type values:
#   "writtens" → written parliamentary answers
#   "debate"   → oral questions + full daily debate record

def main_xml_url(sitting_date: date, doc_type: str, chamber: str = "dail") -> str:
    return (
        f"{DATA_BASE}/akn/ie/debateRecord/{chamber}"
        f"/{sitting_date}/{doc_type}/mul@/main.xml"
    )


def local_path(sitting_date: date, xml_dir: Path, doc_type: str, chamber: str = "dail") -> Path:
    return (
        xml_dir
        / "akn" / "ie" / "debateRecord" / chamber
        / str(sitting_date) / doc_type / "mul@"
        / "main.xml"
    )


async def download_one(
    session: aiohttp.ClientSession,
    sitting_date: date,
    xml_dir: Path,
    dry_run: bool,
    doc_type: str = "writtens",
    chamber: str = "dail",
) -> tuple[date, str, bool, str | None, Path | None]:
    """
    Download main.xml for sitting_date and doc_type.
    Returns (date, doc_type, success, error_message, saved_path).
    saved_path is set only when a file was freshly downloaded (not pre-existing).
    """
    url  = main_xml_url(sitting_date, doc_type, chamber)
    dest = local_path(sitting_date, xml_dir, doc_type, chamber)

    if dest.exists() and dest.stat().st_size > 0:
        log.info(f"  {sitting_date} [{doc_type}] — already downloaded, skipping")
        return sitting_date, doc_type, True, None, None  # None = not newly downloaded

    if dry_run:
        log.info(f"  {sitting_date} [{doc_type}] — DRY RUN would fetch: {url}")
        return sitting_date, doc_type, True, None, None

    dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            async with session.get(
                url,
                # No total timeout: it would include time spent queued for one of the
                # connector's 5 slots, so large backfills timed out before being sent.
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=30, sock_read=REQUEST_TIMEOUT
                ),
            ) as resp:

                if resp.status == 200:
                    content = await resp.read()
                    dest.write_bytes(content)
                    size_kb = len(content) / 1024
                    log.info(f"  {sitting_date} [{doc_type}] — saved {size_kb:.0f} KB → {dest}")
                    return sitting_date, doc_type, True, None, dest  # dest = freshly saved

                elif resp.status == 404:
                    # writtens 404s are normal (not every day has written answers)
                    # debate 404s are rare but handled the same way
                    log.info(f"  {sitting_date} [{doc_type}] — 404 (not published)")
                    return sitting_date, doc_type, False, "404", None

                elif resp.status == 403:
                    log.warning(f"  {sitting_date} [{doc_type}] — 403 forbidden: {url}")
                    return sitting_date, doc_type, False, "403", None

                else:
                    log.warning(
                        f"  {sitting_date} [{doc_type}] — HTTP {resp.status} "
                        f"attempt {attempt}/{RETRY_ATTEMPTS}"
                    )

        except asyncio.TimeoutError:
            log.warning(f"  {sitting_date} [{doc_type}] — timeout attempt {attempt}/{RETRY_ATTEMPTS}")
        except aiohttp.ClientError as e:
            log.warning(f"  {sitting_date} [{doc_type}] — client error attempt {attempt}/{RETRY_ATTEMPTS}: {e}")

        if attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_DELAY * attempt)

    msg = f"failed after {RETRY_ATTEMPTS} attempts"
    log.error(f"  {sitting_date} [{doc_type}] — {msg}: {url}")
    return sitting_date, doc_type, False, msg, None


async def download_all(
    dates: list[date],
    xml_dir: Path,
    dry_run: bool,
    doc_types: list[str],
    chamber: str = "dail",
) -> tuple[list[date], list[date], list[dict]]:
    """
    Download main.xml for each (date, doc_type) combination.
    Returns (succeeded_dates, failed_dates, new_files).
    new_files is a list of dicts describing freshly downloaded files (for the manifest).
    A date appears in failed only if at least one doc_type fails with a non-404 error.
    """
    succeeded_set: set[date] = set()
    failed_set: set[date] = set()
    new_files: list[dict] = []

    downloaded_at = datetime.now(timezone.utc).isoformat()

    connector = aiohttp.TCPConnector(limit=5)  # conservative — large files
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            download_one(session, d, xml_dir, dry_run, dt, chamber)
            for d in dates
            for dt in doc_types
        ]
        for coro in asyncio.as_completed(tasks):
            d, dt, ok, err, saved_path = await coro
            if ok or err == "404":  # 404 = not published, not a real failure
                succeeded_set.add(d)
            else:
                failed_set.add(d)
            if saved_path is not None:
                new_files.append({
                    "downloaded_at": downloaded_at,
                    "date": str(d),
                    "doc_type": dt,
                    "path": str(saved_path),
                })

    # A date counts as failed only if it never succeeded for any type
    true_failed = sorted(failed_set - succeeded_set)
    return sorted(succeeded_set), true_failed, new_files


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

VALID_DOC_TYPES = ("writtens", "debate")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Download new Oireachtas XML files (written answers and/or oral questions)"
    )
    parser.add_argument(
        "--xml-dir",
        default="questions_xml",
        help="Root directory of existing XML files (default: questions_xml)",
    )
    parser.add_argument(
        "--from-date",
        default=None,
        help="Download dates after this date YYYY-MM-DD (default: auto-detect from xml-dir)",
    )
    parser.add_argument(
        "--to-date",
        default=str(date.today()),
        help="Download up to and including this date (default: today)",
    )
    parser.add_argument(
        "--doc-types",
        nargs="+",
        default=list(VALID_DOC_TYPES),
        choices=VALID_DOC_TYPES,
        metavar="TYPE",
        help=(
            "Document types to download: 'writtens' (written answers) and/or "
            "'debate' (oral questions + full debate record). "
            "Default: both. Example: --doc-types writtens debate"
        ),
    )
    parser.add_argument(
        "--manifest",
        default="download_manifest.jsonl",
        help=(
            "Path to append newly downloaded file records (JSONL format). "
            "Used by downstream pipelines to process only new files. "
            "Default: download_manifest.jsonl. Use --manifest /dev/null to disable."
        ),
    )
    parser.add_argument(
        "--chamber",
        default="dail",
        choices=("dail", "seanad"),
        help="Chamber to fetch: 'dail' (default) or 'seanad'",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be downloaded without saving anything",
    )
    args = parser.parse_args()

    xml_dir   = Path(args.xml_dir)
    up_to     = date.fromisoformat(args.to_date)
    doc_types = args.doc_types
    chamber   = args.chamber

    log.info(f"Chamber: {chamber}")
    log.info(f"Document types: {doc_types}")

    # Determine start date
    if args.from_date:
        after = date.fromisoformat(args.from_date)
        log.info(f"Using --from-date: downloading dates after {after}")
    else:
        latest = find_latest_date(xml_dir, chamber)
        if latest is None:
            log.error(f"No {chamber} XML files found in {xml_dir}. Use --from-date to set a start date.")
            sys.exit(1)
        after = latest
        log.info(f"Latest {chamber} date in {xml_dir}: {latest}")

    log.info(f"Checking for new sitting days: {after + timedelta(days=1)} → {up_to}")

    if after >= up_to:
        log.info("Already up to date — nothing to download.")
        return

    if args.dry_run:
        log.info("DRY RUN — no files will be saved")

    # Discover sitting dates then download — all in one event loop
    async def run():
        connector = aiohttp.TCPConnector(limit=5)
        async with aiohttp.ClientSession(connector=connector) as session:
            dates = await fetch_sitting_dates(session, after=after, up_to=up_to, chamber=chamber)

        if not dates:
            log.info("No new sitting days found.")
            return [], [], []

        log.info(f"Dates to download: {[str(d) for d in dates]}")
        return await download_all(dates, xml_dir, args.dry_run, doc_types, chamber)

    succeeded, failed, new_files = asyncio.run(run())

    log.info(f"\nDone.")
    log.info(f"  Dates downloaded : {len(succeeded)}")
    log.info(f"  New files saved  : {len(new_files)}")
    log.info(f"  Dates failed     : {len(failed)}")
    if failed:
        log.warning(f"  Failed dates: {[str(d) for d in failed]}")
        log.info("  Re-run the script to retry failed dates.")
    else:
        log.info("  All dates downloaded successfully.")

    # Write manifest of newly downloaded files for downstream pipelines
    if new_files and not args.dry_run:
        manifest_path = Path(args.manifest)
        with manifest_path.open("a", encoding="utf-8") as mf:
            for entry in sorted(new_files, key=lambda e: (e["date"], e["doc_type"])):
                mf.write(json.dumps(entry) + "\n")
        log.info(f"  Manifest updated : {manifest_path} (+{len(new_files)} entries)")

    if not args.dry_run and succeeded:
        log.info(
            f"\nNext step — re-run the pipeline to parse new files:\n"
            f"  python xml_to_parquet.py \\\n"
            f"      --xml-dir {xml_dir} \\\n"
            f"      --json-dir questions_json \\\n"
            f"      --parquet-out written_answers.parquet\n"
            f"\nThen re-chunk:\n"
            f"  python chunk_for_rag.py --xml-dir {xml_dir} --out chunks.jsonl"
        )


if __name__ == "__main__":
    main()
