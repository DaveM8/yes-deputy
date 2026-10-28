# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**Yes Deputy** — a knowledge-discovery tool over the Irish Oireachtas record: everything said in the Dáil, Seanad and committees since 1919 (debates, Leaders' Questions, statements, committee hearings), plus parliamentary questions and ministerial written/oral answers.

**October 2026 change of scope:** the project began as a written-answers (Q&A) tool. It now indexes every speech, and `/api/chat` is expected to answer general questions over the whole record. The April notes below on search UX still apply, but "LLM/RAG is a future add-on" is no longer the priority order.

### Product direction (as of April 2026)

The primary product is a **human-first research tool** — a specialist search interface closer to Westlaw than ChatGPT. Users (journalists, researchers, lobbyists, academics) search the Oireachtas record using keyword and semantic search, read results in context, and compose and save their own research answers in a built-in workspace.

**LLM/RAG is a future add-on, not the core.** The existing LLM chat (`/api/chat`) is retained but deprioritised. The current focus is on polishing the search + annotation interface.

#### Why this ordering
- Search is genuinely useful standalone — no dependency on RAG quality to deliver value
- Every human answer saved passively accumulates a high-quality eval/training dataset
- When LLM answers are added later they can immediately be benchmarked against the human baseline

#### Target features (current sprint)
- Hybrid search (keyword + semantic) over Oireachtas debates
- Faceted filtering — house, date range, member, question type
- Rich result display with metadata (member name, date, house, question type)
- Deferral-aware ranking — surface substantive answers, suppress procedural deferrals
- Inline source display alongside user's writing area (no context switching)
- Research/answer workspace — text area for composing answers using retrieved results
- Auto-save of all composed answers to database
- Keyboard-first UX — tab navigation, minimal friction

**Implementation status:** the only query path is `/api/chat`. Its retrieval (`app/retrieval.py`) is hybrid kNN + BM25 (see "Request flow" below). `SearchFilters` supports `house`, `year_from`/`year_to`, `member` and `sort`. There is no standalone search UI and no table for saved answers in SQLite yet.

#### LLM integration — future phase
- Add as optional enhancement once the human research tool is solid
- Display LLM answer alongside (not instead of) human research workspace
- Two-pass iterative retrieval pipeline is the candidate architecture
- Immediately testable against accumulated human answer pairs when added

Source data is fetched from `data.oireachtas.ie` as AKN (Akoma Ntoso) XML files.

## Environment

Python 3.12 virtualenv at `.venv/`. Activate with:
```bash
source .venv/bin/activate
```

`requirements.txt` covers the core packages. Install into the venv with:
```bash
.venv/bin/pip install -r requirements.txt
```

Additional analysis packages (`polars`, `pandas`, `ipython`) are installed directly into `.venv` as needed.

Interactive analysis is done in IPython (configured via `.dir-locals.el` for Emacs). Run scripts directly with the venv Python:
```bash
.venv/bin/python stats.py
```

## Running the web server

```bash
bash start.sh
# or directly:
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2
```

Requires a `.env` file (or environment variables) — see `app/config.py` for the full list. Required variables: `OPENAI_API_KEY`, `JWT_SECRET`. Optional but typical: `ES_HOST`, `ES_INDEX`, `ANTHROPIC_API_KEY`, `DB_PATH`. `app/config.py` has its own minimal `.env` loader (no python-dotenv); real environment variables take precedence over `.env` values.

**Production** (see `SETUP.md`): the live site (yesdeputy.com) runs behind nginx as the systemd service `yesdeputy`, from this directory. After code changes: `sudo systemctl restart yesdeputy`; logs via `sudo journalctl -u yesdeputy -f`.

## Nightly incremental pipeline

`run_pipeline.sh` is the end-to-end update job (cron, 02:00 daily; every step idempotent; logs to `pipeline_logs/`, keeps last 30). It takes `pipeline.lock` with `flock` and skips if the lock is held. Run long manual backfills as `flock pipeline.lock <command>` so the two never overlap.

Steps:
1. `update_xmls.py --chamber dail`, then `--chamber seanad`
2. `update_committees.py` — committee debates, saved under `questions_xml/akn/ie/debateRecord/{committee_code}/{date}/debate/mul@/main.xml`
3. `chunk_for_rag.py --from-date <today−14d> --out chunks_pipeline.jsonl` (14-day overlap catches late-published documents)
4. `embed_and_index.py` into `ES_INDEX`, credentials sourced from `.env`

`chunk_for_rag.py` derives `house` from AKN FRBRWork metadata, so chunks carry `"Dáil"`, `"Seanad"` or `"Committee"`.

## Pipeline stages

### Stage 0 — URL list
`xml_urls.txt` contains all XML URLs (one per line) to download from `data.oireachtas.ie`.

### Stage 1 — Download XMLs

**Incremental updates (recommended):**
```bash
# Download both written answers and oral questions for new sitting days since last run:
python update_xmls.py

# Historical backfill — fetch everything since a specific date:
python update_xmls.py --from-date 2000-01-01

# Seanad instead of Dáil (default --chamber dail); preview without saving:
python update_xmls.py --chamber seanad --dry-run

# Written answers only, or oral questions only:
python update_xmls.py --doc-types writtens
python update_xmls.py --doc-types debate
```
`update_xmls.py` queries the Oireachtas API for sitting days, then downloads:
- `questions_xml/akn/ie/debateRecord/dail/{date}/writtens/mul@/main.xml` — written answers
- `questions_xml/akn/ie/debateRecord/dail/{date}/debate/mul@/main.xml` — oral questions + full debate record

The `debate` main.xml contains all question types (Priority Questions, Other Questions, Leaders' Questions, Topical Issues) alongside general debates. 404 on `writtens` is normal (not every sitting day publishes written answers).

**Bulk download from URL list (legacy):**
```bash
python download_xmls.py xml_urls.txt [output_dir]
# Default output: questions_xml/
# Failed URLs written to failed_urls.txt for reruns:
python download_xmls.py failed_urls.txt
```
`xml_urls.txt` contains the original set of individual written-answer section URLs. Downloads are idempotent — already-downloaded files are skipped. Uses 50 concurrent connections with exponential backoff retry.

### Stage 2 — XML → Parquet (two-stage pipeline)
```bash
python xml_to_parquet.py \
    --xml-dir questions_xml \
    --json-dir questions_json \
    --parquet-out written_answers.parquet

# Run only stage 1 (XML→JSON) or stage 2 (JSON→Parquet):
python xml_to_parquet.py ... --stage 1
python xml_to_parquet.py ... --stage 2
```
Stage 1 is parallelised with `ProcessPoolExecutor` (default 8 workers). Both stages are idempotent — already-converted files are skipped.

### Stage 3 — Index into Elasticsearch (legacy, whole-section index)
```bash
python index_elasticsearch.py \
    --parquet written_answers.parquet \
    --es-host http://localhost:9200 \
    --index oireachtas-written-answers

# Rebuild index from scratch:
python index_elasticsearch.py ... --recreate

# With authentication:
python index_elasticsearch.py ... --es-user USER --es-pass PASS
```

This creates the `oireachtas-written-answers` index (one document = one debate section). The web app uses a different, chunk-based index — see Stages 4 and 5.

### Stage 4 — Chunk for RAG
```bash
python chunk_for_rag.py \
    --xml-dir questions_xml \
    --out chunks.jsonl \
    --workers 8 \
    --max-chars 1500
```
Each `debateSection` (at any nesting depth) goes down one of two paths:
- **Has direct `<question>` children** (written answers, oral PQs): `answer_text` chunks of the answer prose up to `--max-chars`, plus `table` chunks (one per HTML table, preceding prose prepended).
- **No `<question>`** (debates, Leaders' Questions, statements, committees, Seanad): `speech` chunks via `chunk_speeches()`. That's one chunk per speech (split over `--max-chars`), with text prefixed `"Speaker (role): "`. Speeches under `MIN_SPEECH_CHARS` (200) are merged into exchange chunks. Tables inside speeches become `table` chunks that also carry speaker fields.

Speech chunk IDs include the chamber or committee code (`speech|{source_key}|{eId}`), because different committees sitting on one date reuse `dbsect_N` eIds. Q&A chunk IDs are unchanged from the original scheme; don't change either formula, or everything gets re-embedded.

`--from-date` restricts to XML files on/after a date. The output file is **overwritten** unless `--append` is passed. Deduplication happens downstream: `embed_and_index.py` skips chunk IDs already in the index, including duplicate lines within one file.

### Stage 5 — Embed and index chunks (the index the web app queries)
```bash
python embed_and_index.py \
    --chunks chunks.jsonl \
    --es-host http://localhost:9200 \
    --openai-key sk-... \
    --index oireachtas-chunks

# Create/recreate the index mapping only:
python embed_and_index.py ... --setup-only
```
Embeds with `text-embedding-3-small` (1536 dims) and bulk-indexes into Elasticsearch with a `dense_vector` field (`int8_hnsw`) for kNN search. Idempotent — skips already-indexed chunk IDs.

**Indices:** the app and the nightly pipeline use `ES_INDEX=oireachtas-search` (set in `.env`), an alias pointing to `oireachtas-chunks-v2`. The v2 mapping (`INDEX_MAPPING`) adds speech fields and **excludes `embedding` from `_source`** to halve storage. Consequence: v2's vectors can't be copied out with `_reindex`; a mapping change means re-embedding from the chunk files. The original `oireachtas-chunks` index (Q&A only, vectors in `_source`) is kept as a rollback target until v2 is verified. Person-name fields have `.keyword` sub-fields for exact filtering.

### Analysis
`stats.py` is an exploratory Polars script that reads `written_answers.parquet` directly. Run it as a script or paste sections into IPython.

## Data model

Each record in the Parquet/Elasticsearch index represents one **debate section** (a Minister answering one or more grouped questions):

| Field | Type | Description |
|---|---|---|
| `id` | string | SHA256 of `date\|house\|section_id` |
| `date` | string | YYYY-MM-DD |
| `year` | int16 | |
| `house` | string | "Dáil" or "Seanad" |
| `heading` | string | Topic heading of the section |
| `section_id` | string | AKN `eId` attribute |
| `department_role` | string | Role/department the question was directed to |
| `answering_person` | string | Minister's name |
| `answering_role` | string | Minister's role title |
| `question_numbers` | list[int] | PQ reference numbers |
| `questions` | list[string] | Cleaned question texts (deduplicated in `stats.py`) |
| `answer_clean` | string | Answer text with housekeeping phrases stripped |

**Chunk index documents** (`oireachtas-chunks-v2`) carry the Q&A fields above per chunk (`answering_person`, `asking_persons`, `questions`, …) and add `chunk_type` (`answer_text` | `table` | `speech`), `committee` (committee code, empty for Dáil/Seanad), `doc_type` (`debate` | `writtens`), `section_type` (AKN section `name`: debate, questions, motion, statement, topical, …) and `heading_path` (nested headings joined with ` > `). Speech chunks also have `speaker`, `speaker_role` and `speakers` (everyone in a merged exchange). `house` is `Dáil`, `Seanad` or `Committee`.

## XML format notes

Source files use the **Akoma Ntoso (AKN)** namespace `http://docs.oasis-open.org/legaldocml/ns/akn/3.0/CSD13`. The parser uses `{*}` wildcard namespace matching for robustness (`root.findall('.//{*}debateSection')`), except where the `NS` dict is explicitly passed.

Question text extraction strips:
- The questioner's name and PQ number prefix from the `<p>` element
- Trailing `[NNNN/YY]` reference tags
- The boilerplate phrase "and if he/she will make a statement on the matter."

Answer text extraction strips "I propose to take Questions Nos. ..." housekeeping lines.

## Web application architecture

The FastAPI app lives in `app/`. It serves a single-page frontend from `static/` and exposes a JSON/SSE API.

### Request flow for `/api/chat`
1. JWT auth (`app/auth.py`) — Bearer token, 30-day expiry, bcrypt passwords
2. Rate-limit check — `FREE_QUERIES_PER_DAY` per user per day, stored in SQLite (`app/database.py`)
3. Filter inference (`_infer_filters` in `app/routers/chat.py`) fills in filters the user didn't set. A full person name found in the question sets `member`, matched by `MemberIndex` (accent-insensitive, multi-word names only, name list refreshed hourly from the index). Recency words ("last", "latest", "recently") set `sort="date"`. A single named chamber sets `house`.
4. Retrieval (`app/retrieval.py`). Default: kNN and BM25 run concurrently and are fused with Reciprocal Rank Fusion, done in Python rather than ES's `rrf` retriever. Card `score` is normalised so rank 1 in both searches = 1.0. With `sort="date"`: a filtered query, newest first, where the remaining topic words (`topic_terms()`) must all match. `member` matches `speaker`/`speakers`/`answering_person`/`asking_persons` `.keyword` fields.
5. Source cards streamed immediately as an SSE `sources` event (before LLM starts)
6. LLM streaming (`app/llm/`) — each token yielded as an SSE `token` event
7. `done` event with remaining quota sent on completion

### LLM provider abstraction (`app/llm/`)
- `base.py` defines the `LLMProvider` ABC with `stream_chat()`
- `claude.py` and `openai_provider.py` are concrete implementations
- `registry.py` maps string keys (`"claude-haiku"`, `"claude-sonnet"`, `"gpt-4o-mini"`, `"gpt-4o"`) to `ModelInfo` + provider singletons
- BYOK: callers can pass their own API key in `ChatRequest.api_key`; it is never stored

### Authentication model
- New users register → `is_approved=0`; admin must approve before they can use the app
- `is_admin=1` users access `/api/admin/*` routes and the `/admin` page
- `app/email_service.py` sends registration notifications to `ADMIN_EMAIL` via SMTP

### SQLite database (`data/users.db` by default)
Two tables: `users` (auth + approval) and `query_log` (daily per-user count). `init_db()` is idempotent and handles the `is_approved` column migration for databases created before approval was added.

### Static frontend (`static/`)
Plain HTML/JS, no build step. `index.html` + `app.js` are the main research interface (SSE consumer for `/api/chat`); `auth.html` is login/register; `admin.html` is the user management panel (inline JS).

### SSE event protocol (`/api/chat`)
The chat endpoint streams Server-Sent Events in this order:

| Event | Payload | Notes |
|---|---|---|
| `sources` | `SourceCard[]` (without `text`) | Sent before LLM starts |
| `token` | `{"text": "..."}` | One per LLM token |
| `done` | `{"finish_reason": "stop", "queries_remaining": N}` | `queries_remaining` absent for BYOK |
| `error` | `{"message": "...", "code"?: "quota_exceeded"}` | Replaces `done` on failure |

### Admin CLI (`manage.py`)
```bash
# List all users (pending shown as PENDING in Approved column):
.venv/bin/python manage.py users

# Approve a pending user:
.venv/bin/python manage.py approve user@example.com

# Create the first admin account (bootstrapping):
.venv/bin/python manage.py create-admin admin@example.com password123
```
The web UI (`/admin`) uses the same approve/delete operations via `/api/admin/*` REST routes.

## Tests

There is no test suite. There are no `test_*.py` files in the project.

## Other scripts and repo hygiene

- `download_debates_metadata.py` — per-sitting-day debate metadata from the Oireachtas API → JSONL (`debates_metadata.jsonl`)
- `scan_anomalies.py` — scans the XML corpus for unhandled elements inside `<speech>`/`<question>` (writes `anomaly_report.*`); run it before changing the parser or chunker
- `generate_year_samples.py` — renders one random Dáil sitting per year to HTML in `year_samples/` for eyeballing format drift

The working tree contains many untracked Emacs artefacts (`#file#`, `.#file`, `file~`), plus large data files and logs. Ignore them and don't commit them.

## Key known data facts

- Total rows: ~681,424 debate sections
- Total questions: ~838,816 (sections can group multiple questions)
- Mean question length: ~220 chars; median: ~193 chars
- Mean answer length: ~1,287 chars; median: ~991 chars
- `parse_xml.py` is an earlier standalone parser — `xml_to_parquet.py` is the production version with the full pipeline
