#!/usr/bin/env python3
"""
Incrementally download Oireachtas XML files for committee debate sittings.

Strategy
--------
  1. Scan questions_xml/ to find the latest committee date already downloaded.
  2. Query api.oireachtas.ie for all committee debates after that date.
  3. For each (committee_code, date) pair, download debate/mul@/main.xml.

The Oireachtas API does not support per-committee filtering — it returns all
committees together when queried with chamber_type=committee. We paginate
through the full result set and dispatch downloads from there.

XML URL pattern:
  https://data.oireachtas.ie/akn/ie/debateRecord/{committee_code}/{date}/debate/mul@/main.xml

Local path:
  questions_xml/akn/ie/debateRecord/{committee_code}/{date}/debate/mul@/main.xml

Usage
-----
    python update_committees.py
    python update_committees.py --from-date 2026-04-01
    python update_committees.py --dry-run
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
REQUEST_TIMEOUT = 60
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 5
LOG_FILE        = "update_committees.log"
DATE_RE         = re.compile(r'/(\d{4}-\d{2}-\d{2})/')

# Known plenary chamber names — exclude these from committee scan
PLENARY_CHAMBERS = {"dail", "seanad"}

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

def find_latest_committee_date(xml_dir: Path) -> date | None:
    """
    Scan questions_xml/akn/ie/debateRecord/ for subdirectories that are
    committee codes (not dail/seanad) and return the most recent date found.
    """
    record_dir = xml_dir / "akn" / "ie" / "debateRecord"
    if not record_dir.exists():
        return None

    latest = None
    for chamber_dir in record_dir.iterdir():
        if not chamber_dir.is_dir():
            continue
        if chamber_dir.name.lower() in PLENARY_CHAMBERS:
            continue  # skip dail/ and seanad/
        for path in chamber_dir.rglob("*.xml"):
            m = DATE_RE.search(str(path))
            if m:
                d = date.fromisoformat(m.group(1))
                if latest is None or d > latest:
                    latest = d
    return latest


# ─────────────────────────────────────────────
# STEP 2 — FETCH ALL COMMITTEE DEBATES IN RANGE
# ─────────────────────────────────────────────

async def fetch_committee_sittings(
    session: aiohttp.ClientSession,
    after: date,
    up_to: date,
) -> list[dict]:
    """
    Return a list of {committee_code, date, xml_uri} dicts for all committee
    debates in (after, up_to].
    """
    sittings: list[dict] = []
    skip  = 0
    limit = 50

    while True:
        url = (
            f"{API_BASE}/debates"
            f"?chamber_type=committee"
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
            dr   = r.get("debateRecord", {})
            house = dr.get("house", {})
            code  = house.get("committeeCode")
            d_str = dr.get("date", "")
            if code and d_str:
                sittings.append({
                    "committee_code": code,
                    "date": date.fromisoformat(d_str),
                })

        total = data["head"]["counts"]["resultCount"]
        skip += len(results)
        log.info(f"  Fetched {min(skip, total):,} / {total:,} committee sittings")
        if skip >= total or not results:
            break

    log.info(f"Found {len(sittings)} committee sittings between "
             f"{after + timedelta(days=1)} and {up_to}")
    return sittings


# ─────────────────────────────────────────────
# STEP 3 — DOWNLOAD main.xml
# ─────────────────────────────────────────────

def main_xml_url(committee_code: str, sitting_date: date) -> str:
    return (
        f"{DATA_BASE}/akn/ie/debateRecord/{committee_code}"
        f"/{sitting_date}/debate/mul@/main.xml"
    )


def local_path(xml_dir: Path, committee_code: str, sitting_date: date) -> Path:
    return (
        xml_dir
        / "akn" / "ie" / "debateRecord" / committee_code
        / str(sitting_date) / "debate" / "mul@"
        / "main.xml"
    )


async def download_one(
    session: aiohttp.ClientSession,
    xml_dir: Path,
    committee_code: str,
    sitting_date: date,
    dry_run: bool,
) -> tuple[bool, str | None, Path | None]:
    """
    Download one committee main.xml. Returns (success, error, saved_path).
    saved_path is None when the file was skipped or dry-run.
    """
    url  = main_xml_url(committee_code, sitting_date)
    dest = local_path(xml_dir, committee_code, sitting_date)

    if dest.exists() and dest.stat().st_size > 0:
        log.info(f"  [{committee_code}] {sitting_date} — already downloaded, skipping")
        return True, None, None

    if dry_run:
        log.info(f"  [{committee_code}] {sitting_date} — DRY RUN would fetch: {url}")
        return True, None, None

    dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    dest.write_bytes(content)
                    log.info(f"  [{committee_code}] {sitting_date} — saved {len(content)//1024} KB → {dest}")
                    return True, None, dest
                elif resp.status == 404:
                    log.info(f"  [{committee_code}] {sitting_date} — 404 (not published)")
                    return False, "404", None
                else:
                    log.warning(
                        f"  [{committee_code}] {sitting_date} — "
                        f"HTTP {resp.status} attempt {attempt}/{RETRY_ATTEMPTS}"
                    )
        except asyncio.TimeoutError:
            log.warning(f"  [{committee_code}] {sitting_date} — timeout attempt {attempt}")
        except aiohttp.ClientError as e:
            log.warning(f"  [{committee_code}] {sitting_date} — error attempt {attempt}: {e}")

        if attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_DELAY * attempt)

    log.error(f"  [{committee_code}] {sitting_date} — failed after {RETRY_ATTEMPTS} attempts")
    return False, "failed", None


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Download new Oireachtas XML files for committee debates"
    )
    parser.add_argument(
        "--xml-dir",
        default="questions_xml",
        help="Root directory of existing XML files (default: questions_xml)",
    )
    parser.add_argument(
        "--from-date",
        default=None,
        help=(
            "Download dates after this date YYYY-MM-DD. "
            "Default: auto-detect from latest committee XML already in xml-dir, "
            "or 2000-01-01 if none found."
        ),
    )
    parser.add_argument(
        "--to-date",
        default=str(date.today()),
        help="Download up to and including this date (default: today)",
    )
    parser.add_argument(
        "--manifest",
        default="download_manifest.jsonl",
        help="JSONL file to append new download records to (default: download_manifest.jsonl)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be downloaded without saving anything",
    )
    args = parser.parse_args()

    xml_dir = Path(args.xml_dir)
    up_to   = date.fromisoformat(args.to_date)

    if args.from_date:
        after = date.fromisoformat(args.from_date)
        log.info(f"Using --from-date: downloading dates after {after}")
    else:
        latest = find_latest_committee_date(xml_dir)
        if latest is not None:
            after = latest
            log.info(f"Latest committee date in {xml_dir}: {latest}")
        else:
            after = date(2000, 1, 1)
            log.info(f"No existing committee files found — starting from {after}")

    log.info(f"Checking for new committee sittings: {after + timedelta(days=1)} → {up_to}")

    if after >= up_to:
        log.info("Already up to date — nothing to download.")
        return

    if args.dry_run:
        log.info("DRY RUN — no files will be saved")

    async def run():
        connector = aiohttp.TCPConnector(limit=5)
        async with aiohttp.ClientSession(connector=connector) as session:
            sittings = await fetch_committee_sittings(session, after=after, up_to=up_to)

        if not sittings:
            log.info("No new committee sittings found.")
            return

        log.info(f"Downloading {len(sittings)} sittings across "
                 f"{len({s['committee_code'] for s in sittings})} committees…")

        total_new    = 0
        total_errors = 0
        downloaded_at = datetime.now(timezone.utc).isoformat()
        new_manifest_entries: list[dict] = []

        connector2 = aiohttp.TCPConnector(limit=5)
        async with aiohttp.ClientSession(connector=connector2) as session:
            for sitting in sorted(sittings, key=lambda s: (s["date"], s["committee_code"])):
                ok, err, saved = await download_one(
                    session, xml_dir,
                    sitting["committee_code"], sitting["date"],
                    args.dry_run,
                )
                if saved:
                    total_new += 1
                    new_manifest_entries.append({
                        "downloaded_at": downloaded_at,
                        "date": str(sitting["date"]),
                        "doc_type": "debate",
                        "committee_code": sitting["committee_code"],
                        "path": str(saved),
                    })
                elif err not in (None, "404"):
                    total_errors += 1

        log.info(f"\nDone.")
        log.info(f"  New files saved : {total_new}")
        log.info(f"  Errors          : {total_errors}")

        if new_manifest_entries and not args.dry_run:
            manifest_path = Path(args.manifest)
            with manifest_path.open("a", encoding="utf-8") as mf:
                for entry in new_manifest_entries:
                    mf.write(json.dumps(entry) + "\n")
            log.info(f"  Manifest updated: {manifest_path} (+{len(new_manifest_entries)} entries)")

    asyncio.run(run())


if __name__ == "__main__":
    main()
