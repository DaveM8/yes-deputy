#!/bin/bash
# Full incremental update pipeline for Yes Deputy.
# Downloads new Oireachtas XML, chunks, embeds, and indexes into Elasticsearch.
# Safe to run repeatedly — every step is idempotent.

set -euo pipefail

cd "$(dirname "$0")"

PYTHON=".venv/bin/python"
LOG_DIR="pipeline_logs"
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
LOG_FILE="$LOG_DIR/pipeline_$TIMESTAMP.log"
CHUNK_FILE="chunks_pipeline.jsonl"
# Overlap by 14 days to catch any late-published documents
FROM_DATE=$(date -d "14 days ago" +"%Y-%m-%d" 2>/dev/null || date -v-14d +"%Y-%m-%d")

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "=== Yes Deputy pipeline starting ==="
log "Chunk from-date: $FROM_DATE"

# ── Step 1: Download new XMLs ──────────────────────────────────────────────
log "--- Step 1: Downloading Dáil debates ---"
$PYTHON update_xmls.py --chamber dail 2>&1 | tee -a "$LOG_FILE"

log "--- Step 1b: Downloading Seanad debates ---"
$PYTHON update_xmls.py --chamber seanad 2>&1 | tee -a "$LOG_FILE"

log "--- Step 1c: Downloading committee debates ---"
$PYTHON update_committees.py 2>&1 | tee -a "$LOG_FILE"

# ── Step 2: Chunk new XML files ────────────────────────────────────────────
log "--- Step 2: Chunking (from $FROM_DATE) ---"
$PYTHON chunk_for_rag.py \
    --xml-dir questions_xml \
    --out "$CHUNK_FILE" \
    --workers 8 \
    --max-chars 1500 \
    --from-date "$FROM_DATE" \
    2>&1 | tee -a "$LOG_FILE"

# ── Step 3: Embed and index ────────────────────────────────────────────────
log "--- Step 3: Embedding and indexing ---"

# Load credentials from .env
set -a
# shellcheck disable=SC1091
source .env
set +a

$PYTHON embed_and_index.py \
    --chunks "$CHUNK_FILE" \
    --es-host "${ES_HOST:-http://localhost:9200}" \
    ${ES_USER:+--es-user "$ES_USER"} \
    ${ES_PASS:+--es-pass "$ES_PASS"} \
    --openai-key "$OPENAI_API_KEY" \
    --index "${ES_INDEX:-oireachtas-chunks}" \
    2>&1 | tee -a "$LOG_FILE"

log "=== Pipeline complete ==="

# Keep only the last 30 logs
ls -t "$LOG_DIR"/pipeline_*.log 2>/dev/null | tail -n +31 | xargs rm -f --
