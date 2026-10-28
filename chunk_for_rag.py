#!/usr/bin/env python3
"""
Chunk Oireachtas debate and written-answer XML files into RAG-ready JSONL.

Sections with <question> children (written answers, oral PQs) produce:
  - answer_text : answer prose grouped up to --max-chars
  - table       : one chunk per embedded table, with preceding prose prepended as context

All other sections (debates, Leaders' Questions, statements, committee hearings) produce:
  - speech      : one chunk per speech, split over --max-chars, prefixed "Speaker (role): ".
                  Consecutive short speeches (interjections) are merged into one exchange chunk.
  - table       : tables inside speeches, as above

Output is newline-delimited JSON (JSONL), one chunk per line.
Each chunk is self-contained — it carries the full metadata needed for citation.

Usage:
    python chunk_for_rag.py \\
        --xml-dir  questions_xml \\
        --out      chunks.jsonl \\
        --workers  8 \\
        --max-chars 1500

Incremental:
    --from-date limits processing to files on/after a date. The output file is
    overwritten unless --append is given; embed_and_index.py skips chunk IDs that
    are already indexed, so re-chunking overlapping ranges is safe.
"""

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from lxml import etree

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

NS            = {'akn': 'http://docs.oasis-open.org/legaldocml/ns/akn/3.0/CSD13'}
XML_WORKERS   = 8
DEFAULT_MAX_CHARS = 1500
MIN_SPEECH_CHARS  = 200   # shorter speeches are merged into an exchange chunk
LOG_FILE      = "chunker.log"

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
# XML HELPERS
# ─────────────────────────────────────────────

def localname(tag: str) -> str:
    return tag.split('}', 1)[1] if tag.startswith('{') else tag


def get_text(elem) -> str:
    return ' '.join(''.join(elem.itertext()).split())


def para_to_text(p_elem) -> str:
    """
    Flatten a <p> to plain text, substituting <img> with a fixed description.
    Handles all inline elements (<b>, <i>, <u>, <a>, <sup>, etc.) transparently
    via itertext — except <img> which has no text content.
    """
    parts = []
    if p_elem.text:
        parts.append(p_elem.text)
    for child in p_elem:
        if not isinstance(child.tag, str):
            continue
        tag = localname(child.tag)
        if tag == 'img':
            parts.append('[Organisation chart — not available as text]')
        else:
            parts.extend(child.itertext())
        if child.tail:
            parts.append(child.tail)
    return ' '.join(' '.join(parts).split())


BOILERPLATE = re.compile(r'^I propose to take Questions? Nos?\.', re.IGNORECASE)


def is_boilerplate(text: str) -> bool:
    return bool(BOILERPLATE.match(text))


# ─────────────────────────────────────────────
# TABLE EXTRACTION
# ─────────────────────────────────────────────

def extract_table(table_elem) -> dict:
    """
    Parse a <table> element into a structured dict:
      caption : str (empty string if absent or generic)
      headers : list[str]  — first header row
      rows    : list[list[str]]  — data rows
    """
    # Caption
    cap_elem = table_elem.find('.//{*}caption')
    caption = get_text(cap_elem).strip() if cap_elem is not None else ''
    if caption.lower() in ('table', 'tables', ''):
        caption = ''

    # Walk <tr> rows in document order
    all_rows: list[list[dict]] = []
    for tr in table_elem.findall('.//{*}tr'):
        cells = []
        for child in tr:
            if not isinstance(child.tag, str):
                continue
            tag = localname(child.tag)
            if tag in ('td', 'th'):
                cells.append({
                    'text': get_text(child),
                    'is_header': tag == 'th',
                })
        if cells:
            all_rows.append(cells)

    if not all_rows:
        return {'caption': caption, 'headers': [], 'rows': []}

    # Determine header row(s).
    # A row is a header row if all its cells are <th>, OR if it's the only row
    # in a single-column span (sub-heading pattern).
    headers: list[str] = []
    data_rows: list[list[str]] = []

    i = 0
    # Consume leading rows that are all-<th> as column headers
    while i < len(all_rows) and all(c['is_header'] for c in all_rows[i]):
        # If this <th> row has only 1 cell and data rows have more, it's a sub-heading
        # — include it as a caption extension rather than a column header
        if len(all_rows[i]) == 1 and i == 0:
            if not caption:
                caption = all_rows[i][0]['text']
            i += 1
            continue
        headers = [c['text'] for c in all_rows[i]]
        i += 1
        break  # Only one column-header row

    # If no <th> headers found, treat the first row as headers
    if not headers and all_rows:
        headers = [c['text'] for c in all_rows[i]]
        i += 1

    # Remaining rows are data
    for row in all_rows[i:]:
        data_rows.append([c['text'] for c in row])

    return {'caption': caption, 'headers': headers, 'rows': data_rows}


def table_to_text(table: dict) -> str:
    """
    Render a structured table dict as natural-language text for embedding.

    Format:
        Table: <caption>
        Columns: Year, Recipients
        Year: 2007; Recipients: 169
        Year: 2008; Recipients: 438
    """
    lines = []

    if table['caption']:
        lines.append(f"Table: {table['caption']}")

    headers = table['headers']
    if headers:
        lines.append(f"Columns: {', '.join(headers)}")

    for row in table['rows']:
        if not any(cell.strip() for cell in row):
            continue  # skip blank rows
        if headers and len(row) == len(headers):
            lines.append('; '.join(f"{h}: {v}" for h, v in zip(headers, row)))
        else:
            lines.append('; '.join(cell for cell in row if cell.strip()))

    return '\n'.join(lines)


# ─────────────────────────────────────────────
# BLOCK EXTRACTION
# ─────────────────────────────────────────────

def extract_blocks(speeches: list) -> list[dict]:
    """
    Walk all speech children in document order across all speeches,
    yielding a sequence of typed blocks:
      {'type': 'text',  'text': str}
      {'type': 'table', 'caption': str, 'headers': [...], 'rows': [...]}

    Boilerplate ("I propose to take Questions...") is dropped.
    <img> elements are replaced with a fixed description inside para_to_text().
    """
    blocks = []
    skip_tags = {'from', 'recordedTime'}

    for speech in speeches:
        for child in speech:
            if not isinstance(child.tag, str):
                continue
            tag = localname(child.tag)

            if tag in skip_tags:
                continue

            elif tag == 'p':
                text = para_to_text(child)
                if text and not is_boilerplate(text):
                    blocks.append({'type': 'text', 'text': text})

            elif tag == 'table':
                table = extract_table(child)
                # Only add tables that have actual data
                if table['headers'] or table['rows']:
                    blocks.append({'type': 'table', **table})

    return blocks


# ─────────────────────────────────────────────
# CHUNKING
# ─────────────────────────────────────────────

def chunk_blocks(blocks: list[dict], max_chars: int) -> list[dict]:
    """
    Convert a sequence of blocks into a list of chunks.

    Rules:
      - Text blocks are grouped greedily up to max_chars, split on block boundaries.
      - Each table becomes its own chunk. The prose paragraph immediately before
        the table is prepended as context (not consumed from the text chunk budget).
      - chunk_index tracks position within the answer for ordering.
    """
    chunks = []
    pending_text: list[str] = []
    pending_chars = 0
    last_text_block = ''  # the most recent prose paragraph, for table context

    def flush_text():
        nonlocal pending_text, pending_chars
        if pending_text:
            chunks.append({
                'chunk_type': 'answer_text',
                'text': '\n\n'.join(pending_text),
            })
            pending_text = []
            pending_chars = 0

    for block in blocks:
        if block['type'] == 'text':
            text = block['text']
            last_text_block = text

            if pending_chars + len(text) > max_chars and pending_text:
                flush_text()

            pending_text.append(text)
            pending_chars += len(text)

        elif block['type'] == 'table':
            # Flush any pending prose first
            flush_text()

            # If the table has no caption, use the preceding prose paragraph as fallback
            if not block.get('caption') and last_text_block:
                block = {**block, 'caption': last_text_block}

            table_text = table_to_text(block)
            if not table_text.strip():
                continue

            # Prepend the last prose paragraph as context (distinct from caption —
            # caption labels the table, context gives the embedding model the
            # surrounding sentence that introduced it)
            context = last_text_block.strip()
            if context:
                full_text = f"{context}\n\n{table_text}"
            else:
                full_text = table_text

            chunks.append({
                'chunk_type': 'table',
                'text': full_text,
                'table_caption': block.get('caption', ''),
                'table_headers': block.get('headers', []),
            })

    flush_text()

    # Add chunk_index
    for i, chunk in enumerate(chunks):
        chunk['chunk_index'] = i

    return chunks


def chunk_speeches(speeches: list, resolve, max_chars: int) -> list[dict]:
    """
    Chunk a section's speeches, keeping each chunk attributable to its speaker(s).

    Rules:
      - Each speech is chunked on its own via chunk_blocks(); every chunk is
        prefixed "Speaker (role): " so the speaker is visible to BM25 and embeddings.
      - Text-only speeches shorter than MIN_SPEECH_CHARS are buffered and merged
        with adjacent short speeches into one exchange chunk (up to max_chars).
      - Each chunk carries speaker (the one contributing most text), speaker_role
        and speakers (everyone in the chunk).
    """
    chunks: list[dict] = []
    exchange: list[tuple[str, str, str]] = []   # (speaker, role, labelled text)
    exchange_chars = 0

    def label(speaker: str, role: str) -> str:
        return f"{speaker} ({role})" if role else speaker

    def flush_exchange():
        nonlocal exchange, exchange_chars
        if exchange:
            weight: dict[str, int] = {}
            for spk, _, text in exchange:
                weight[spk] = weight.get(spk, 0) + len(text)
            main = max(weight, key=weight.get)
            chunks.append({
                'chunk_type':   'speech',
                'text':         '\n\n'.join(text for _, _, text in exchange),
                'speaker':      main,
                'speaker_role': next(r for s, r, _ in exchange if s == main),
                'speakers':     list(dict.fromkeys(s for s, _, _ in exchange if s)),
            })
            exchange = []
            exchange_chars = 0

    for speech in speeches:
        speaker = resolve(speech.get('by'))
        if not speaker:
            from_elem = speech.find('{*}from')
            speaker = get_text(from_elem) if from_elem is not None else ''
        role = resolve(speech.get('as')) if speech.get('as') else ''

        blocks = extract_blocks([speech])
        if not blocks:
            continue
        prefix = f"{label(speaker, role)}: " if speaker else ''

        text_len = sum(len(b['text']) for b in blocks if b['type'] == 'text')
        if text_len < MIN_SPEECH_CHARS and all(b['type'] == 'text' for b in blocks):
            text = prefix + ' '.join(b['text'] for b in blocks)
            if exchange and exchange_chars + len(text) > max_chars:
                flush_exchange()
            exchange.append((speaker, role, text))
            exchange_chars += len(text)
            continue

        flush_exchange()
        for sub in chunk_blocks(blocks, max_chars):
            sub.pop('chunk_index')
            if sub['chunk_type'] == 'answer_text':
                sub['chunk_type'] = 'speech'
                sub['text'] = prefix + sub['text']
            else:  # table: keep the speaker visible without disturbing the table layout
                sub['text'] = f"{prefix.rstrip()}\n{sub['text']}" if prefix else sub['text']
            chunks.append({
                **sub,
                'speaker':      speaker,
                'speaker_role': role,
                'speakers':     [speaker] if speaker else [],
            })

    flush_exchange()

    for i, chunk in enumerate(chunks):
        chunk['chunk_index'] = i
    return chunks


# ─────────────────────────────────────────────
# RECORD ID + METADATA
# ─────────────────────────────────────────────

def make_record_id(date: str, section_id: str, house: str) -> str:
    return hashlib.sha256(f"{date}|{house}|{section_id}".encode()).hexdigest()


def make_chunk_id(record_id: str, chunk_index: int) -> str:
    return hashlib.sha256(f"{record_id}|{chunk_index}".encode()).hexdigest()[:16]


# ─────────────────────────────────────────────
# QUESTION TEXT EXTRACTION
# ─────────────────────────────────────────────

def clean_question_text(elem) -> str:
    bold_elems = (elem.findall('b') or
                  elem.findall('{http://docs.oasis-open.org/legaldocml/ns/akn/3.0/CSD13}b'))
    if bold_elems:
        tail = (bold_elems[-1].tail or '').strip()
        tail = re.sub(r'\s*\[\d+/\d+\]\s*$', '', tail)
        return tail.strip()
    raw = get_text(elem)
    raw = re.sub(r'^\d+\.?\s*', '', raw)
    raw = re.sub(r'\s*\[\d+/\d+\]\s*$', '', raw)
    return raw.strip()


def extract_pq_number(text: str) -> int | None:
    m = re.match(r'^(\d+)\.?\s', text.strip())
    return int(m.group(1)) if m else None


# ─────────────────────────────────────────────
# PER-FILE PROCESSOR
# ─────────────────────────────────────────────

def process_xml(args: tuple) -> tuple[str, list[dict], str | None]:
    """
    Parse one XML file and return (xml_path_str, chunks, error).
    Each chunk is a fully-hydrated dict ready to write as a JSONL line.
    """
    xml_path, max_chars = args
    try:
        parser = etree.XMLParser(encoding='utf-8', recover=True)
        tree   = etree.parse(str(xml_path), parser)
        root   = tree.getroot()
    except Exception as e:
        return str(xml_path), [], str(e)

    # Build eId → showAs reference map
    refs: dict[str, str] = {}
    for elem in root.findall('.//{*}references/{*}*'):
        eid     = elem.get('eId')
        show_as = elem.get('showAs')
        if eid and show_as:
            refs[eid] = show_as

    def resolve(href: str | None) -> str:
        if not href:
            return ''
        return refs.get(href.lstrip('#'), href.lstrip('#'))

    # Date and house from FRBRWork metadata
    date_elem   = root.find('.//{*}FRBRWork/{*}FRBRdate')
    debate_date = date_elem.get('date', '') if date_elem is not None else ''
    year        = int(debate_date[:4]) if debate_date else None

    author_elem  = root.find('.//{*}FRBRWork/{*}FRBRauthor')
    author_href  = author_elem.get('href', '') if author_elem is not None else ''
    if '/committee/' in author_href.lower():
        house = 'Committee'
    elif '/dail/' in author_href.lower():
        house = 'Dáil'
    elif '/seanad/' in author_href.lower():
        house = 'Seanad'
    else:
        house = ''

    source_key, doc_type = _source_from_path(str(xml_path))
    committee = source_key if house == 'Committee' else ''

    def section_heading(section) -> str:
        heading_elem = section.find('akn:heading', NS)
        heading = ''
        if heading_elem is not None:
            heading = (heading_elem.text or '').strip()
            for child in heading_elem:
                tail = (child.tail or '').strip()
                if tail:
                    heading += ' ' + tail
        return heading.strip()

    all_chunks: list[dict] = []

    for section in root.findall('.//{*}debateSection', NS):
        heading = section_heading(section)
        ancestors = [a for a in section.iterancestors() if localname(a.tag) == 'debateSection']
        heading_path = ' > '.join(
            h for h in [section_heading(a) for a in reversed(ancestors)] + [heading] if h
        )
        section_id = section.get('eId', '')
        section_meta = {
            'date':         debate_date,
            'year':         year,
            'house':        house,
            'committee':    committee,
            'doc_type':     doc_type,
            'heading':      heading,
            'heading_path': heading_path,
            'section_id':   section_id,
            'section_type': section.get('name', ''),
        }

        # Questions
        questions: list[dict] = []
        for q in section.findall('akn:question', NS):
            p = q.find('akn:p', NS)
            if p is None:
                continue
            raw    = get_text(p)
            pq_num = extract_pq_number(raw)
            questions.append({
                'number':         pq_num,
                'asking_person':  resolve(q.get('by')),
                'question_clean': clean_question_text(p),
            })

        if not questions:
            # Debate, statements, Leaders' Questions, committee hearing, ...
            speeches = section.findall('akn:speech', NS)
            if not speeches:
                continue
            # source_key in the ID keeps committees sitting on the same date
            # (with the same dbsect_N eIds) apart
            record_id = make_record_id(debate_date, f"speech|{source_key}|{section_id}", house)
            for chunk in chunk_speeches(speeches, resolve, max_chars):
                all_chunks.append({
                    'id':        make_chunk_id(record_id, chunk['chunk_index']),
                    'record_id': record_id,
                    **section_meta,
                    **chunk,
                })
            continue

        # Speeches — skip unanswered sections (<summary> present instead)
        speeches = section.findall('akn:speech', NS)
        if not speeches:
            continue

        answered_by    = resolve(speeches[0].get('by'))
        answering_role = resolve(speeches[0].get('as'))

        roles = list(dict.fromkeys(
            resolve(q.get('to'))
            for q in section.findall('akn:question', NS)
        ))
        department_role = roles[0] if len(roles) == 1 else '; '.join(roles)

        record_id  = make_record_id(debate_date, section_id, house)

        # Build shared metadata for every chunk from this section
        base_meta = {
            'record_id':       record_id,
            **section_meta,
            'department_role': department_role,
            'answering_person': answered_by,
            'answering_role':  answering_role,
            'questions':       [q['question_clean'] for q in questions if q['question_clean']],
            'question_numbers': [q['number'] for q in questions if q['number'] is not None],
            'asking_persons':  list(dict.fromkeys(
                q['asking_person'] for q in questions if q['asking_person']
            )),
        }

        # Extract blocks and chunk
        blocks = extract_blocks(speeches)
        chunks = chunk_blocks(blocks, max_chars)

        for chunk in chunks:
            chunk_id = make_chunk_id(record_id, chunk['chunk_index'])
            all_chunks.append({
                'id': chunk_id,
                **base_meta,
                **chunk,
            })

    return str(xml_path), all_chunks, None


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

_DATE_IN_PATH = re.compile(r'/(\d{4}-\d{2}-\d{2})/')
_SOURCE_IN_PATH = re.compile(r'/debateRecord/([^/]+)/\d{4}-\d{2}-\d{2}/([^/]+)/')


def _source_from_path(p: str) -> tuple[str, str]:
    """
    Return (source_key, doc_type) from .../debateRecord/{source_key}/{date}/{doc_type}/...
    source_key is 'dail', 'seanad' or a committee code; doc_type is 'debate' or 'writtens'.
    """
    m = _SOURCE_IN_PATH.search(p)
    return (m.group(1), m.group(2)) if m else ('', '')


def _date_from_path(p: Path) -> str:
    """Extract YYYY-MM-DD from the file path, or '' if not found."""
    m = _DATE_IN_PATH.search(str(p))
    return m.group(1) if m else ''


def main():
    parser = argparse.ArgumentParser(description='Chunk Oireachtas XML into RAG-ready JSONL')
    parser.add_argument('--xml-dir',   default='questions_xml',  help='Root dir of XML files')
    parser.add_argument('--out',       default='chunks.jsonl',   help='Output JSONL path')
    parser.add_argument('--workers',   type=int, default=XML_WORKERS)
    parser.add_argument('--max-chars', type=int, default=DEFAULT_MAX_CHARS,
                        help='Max characters per prose chunk (default 1500)')
    parser.add_argument('--from-date', default=None,
                        help='Only process XML files on or after this date (YYYY-MM-DD)')
    parser.add_argument('--append',    action='store_true',
                        help='Append to existing output file instead of overwriting')
    args = parser.parse_args()

    xml_dir = Path(args.xml_dir)
    out_path = Path(args.out)

    all_xml_files = list(xml_dir.rglob('*.xml'))

    if args.from_date:
        xml_files = [p for p in all_xml_files if _date_from_path(p) >= args.from_date]
        log.info(f"Filtered to {len(xml_files):,} XML files on or after {args.from_date} "
                 f"(skipped {len(all_xml_files) - len(xml_files):,})")
    else:
        xml_files = all_xml_files

    total = len(xml_files)
    log.info(f"Found {total:,} XML files")
    log.info(f"Output → {out_path}  max_chars={args.max_chars}  workers={args.workers}")

    done        = 0
    total_chunks = 0
    errors      = 0
    start       = time.time()

    mode = 'a' if args.append else 'w'
    with open(out_path, mode, encoding='utf-8') as out_f:
        work = [(p, args.max_chars) for p in xml_files]
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(process_xml, w): w for w in work}
            for fut in as_completed(futures):
                xml_path, chunks, error = fut.result()
                done += 1

                if error:
                    errors += 1
                    log.warning(f"Error in {xml_path}: {error}")
                else:
                    for chunk in chunks:
                        out_f.write(json.dumps(chunk, ensure_ascii=False) + '\n')
                    total_chunks += len(chunks)

                if done % 50_000 == 0:
                    elapsed   = time.time() - start
                    rate      = done / elapsed
                    remaining = (total - done) / rate if rate > 0 else 0
                    log.info(
                        f"{done:,}/{total:,} ({100*done/total:.1f}%)  "
                        f"chunks={total_chunks:,}  errors={errors}  "
                        f"rate={rate:.0f}/s  ETA={remaining/60:.1f}min"
                    )

    elapsed = time.time() - start
    log.info(f"Done in {elapsed:.1f}s — {total_chunks:,} chunks written to {out_path}")
    log.info(f"Errors: {errors}")


if __name__ == '__main__':
    main()
