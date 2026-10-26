import asyncio
import logging
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
import aiohttp

# --- Config ---
MAX_CONCURRENT = 50        # concurrent connections, tune up/down
RETRY_ATTEMPTS = 3         # retries per failed file
RETRY_DELAY    = 5         # seconds between retries
REQUEST_TIMEOUT = 30       # seconds per request
OUTPUT_DIR = Path("questions_xml")
LOG_FILE   = "download.log"
FAILED_FILE = "failed_urls.txt"   # rerunnable - feed back in as input
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


def url_to_local_path(url: str, base_dir: Path) -> Path:
    """
    Mirror the URL path structure locally.
    https://data.oireachtas.ie/akn/ie/debateRecord/dail/2023-12-13/writtens/mul@/dbsect_173.xml
    -> oireachtas_xml/akn/ie/debateRecord/dail/2023-12-13/writtens/mul@/dbsect_173.xml
    """
    parsed = urlparse(url)
    # strip leading /
    relative = parsed.path.lstrip('/')
    return base_dir / relative


def load_urls(path: str) -> list[str]:
    with open(path, 'r', encoding='utf-8') as f:
        urls = [line.strip() for line in f if line.strip() and line.startswith('http')]
    return urls


def already_downloaded(urls: list[str], base_dir: Path) -> tuple[list[str], int]:
    """Skip URLs where the local file already exists and is non-empty."""
    pending = []
    skipped = 0
    for url in urls:
        local = url_to_local_path(url, base_dir)
        if local.exists() and local.stat().st_size > 0:
            skipped += 1
        else:
            pending.append(url)
    return pending, skipped


async def download_one(
    session: aiohttp.ClientSession,
    url: str,
    base_dir: Path,
    semaphore: asyncio.Semaphore,
    failed: list[str]
) -> bool:
    local_path = url_to_local_path(url, base_dir)
    local_path.parent.mkdir(parents=True, exist_ok=True)

    async with semaphore:
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        async with aiofiles.open(local_path, 'wb') as f:
                            await f.write(content)
                        return True
                    elif resp.status == 404:
                        log.warning(f"404 NOT FOUND: {url}")
                        failed.append(url)
                        return False
                    else:
                        log.warning(f"HTTP {resp.status} attempt {attempt}/{RETRY_ATTEMPTS}: {url}")

            except asyncio.TimeoutError:
                log.warning(f"Timeout attempt {attempt}/{RETRY_ATTEMPTS}: {url}")
            except aiohttp.ClientError as e:
                log.warning(f"Client error attempt {attempt}/{RETRY_ATTEMPTS}: {url} — {e}")

            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY * attempt)  # backoff

        log.error(f"FAILED after {RETRY_ATTEMPTS} attempts: {url}")
        failed.append(url)
        return False


async def download_all(urls: list[str], base_dir: Path):
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    failed: list[str] = []
    completed = 0
    total = len(urls)
    start = time.time()

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT, limit_per_host=MAX_CONCURRENT)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            download_one(session, url, base_dir, semaphore, failed)
            for url in urls
        ]

        for coro in asyncio.as_completed(tasks):
            result = await coro
            completed += 1
            if completed % 1000 == 0:
                elapsed = time.time() - start
                rate = completed / elapsed
                remaining = (total - completed) / rate if rate > 0 else 0
                log.info(
                    f"Progress: {completed}/{total} "
                    f"({100*completed/total:.1f}%) "
                    f"rate={rate:.1f}/s "
                    f"ETA={remaining/60:.1f}min "
                    f"failed={len(failed)}"
                )

    return failed


def main():
    if len(sys.argv) < 2:
        print("Usage: python download_oireachtas.py <urls_file.txt> [output_dir]")
        sys.exit(1)

    urls_file = sys.argv[1]
    base_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else OUTPUT_DIR

    log.info(f"Loading URLs from {urls_file}")
    urls = load_urls(urls_file)
    log.info(f"Loaded {len(urls):,} URLs")

    urls, skipped = already_downloaded(urls, base_dir)
    log.info(f"Skipping {skipped:,} already downloaded, {len(urls):,} remaining")

    if not urls:
        log.info("Nothing to download.")
        return

    failed = asyncio.run(download_all(urls, base_dir))

    # Write failed URLs for rerun
    if failed:
        with open(FAILED_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(failed))
        log.info(f"Wrote {len(failed):,} failed URLs to {FAILED_FILE}")
        log.info(f"Rerun with: python download_oireachtas.py {FAILED_FILE}")
    else:
        log.info("All files downloaded successfully.")


if __name__ == '__main__':
    main()
