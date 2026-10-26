#!/usr/bin/env python3
"""
Stage 1: Parse all XML files to JSON (one JSON per XML)
Stage 2: Consolidate all JSON files into a single Parquet file

Usage:
    python pipeline.py --xml-dir /home/dave/gov/members_questions/questions_xml \
                       --json-dir /home/dave/gov/members_questions/questions_json \
                       --parquet-out /home/dave/gov/members_questions/written_answers.parquet
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

import pyarrow as pa
import pyarrow.parquet as pq
from lxml import etree

# --- Config ---
XML_WORKERS   = 8
PARQUET_BATCH = 10000
LOG_FILE      = "pipeline.log"
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
# XML PARSER
# ─────────────────────────────────────────────

NS = {'akn': 'http://docs.oasis-open.org/legaldocml/ns/akn/3.0/CSD13'}


def get_text(elem) -> str:
    return ' '.join(''.join(elem.itertext()).split())


def clean_question_text(elem) -> str:
    bold_elems = elem.findall('b')
    if not bold_elems:
        bold_elems = elem.findall('{http://docs.oasis-open.org/legaldocml/ns/akn/3.0/CSD13}b')

    if bold_elems:
        last_bold = bold_elems[-1]
        tail = (last_bold.tail or '').strip()
        tail = re.sub(r'\s*\[\d+/\d+\]\s*$', '', tail)
        return tail.strip()

    raw = ' '.join(''.join(elem.itertext()).split())
    raw = re.sub(r'^\d+\.?\s*', '', raw)
    raw = re.sub(r'^.*?(?:Minister for[\w\s,]+?)\s+', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'\s*\[\d+/\d+\]\s*$', '', raw)
    return raw.strip()


def clean_answer_text(paragraphs: list[str]) -> str:
    filtered = [p for p in paragraphs
                if not re.match(r'^I propose to take Questions? Nos?\.', p, re.IGNORECASE)]
    return '\n\n'.join(filtered)


def extract_pq_number(text: str) -> int | None:
    m = re.match(r'^(\d+)\.?\s', text.strip())
    return int(m.group(1)) if m else None


def make_id(date: str, section_id: str, house: str) -> str:
    key = f"{date}|{house}|{section_id}"
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def parse_written_answers(xml_path: str) -> list[dict]:
    try:
        parser = etree.XMLParser(encoding='utf-8', recover=True)
        tree = etree.parse(xml_path, parser)
        root = tree.getroot()
    except Exception as e:
        log.warning(f"Failed to parse XML {xml_path}: {e}")
        return []

    refs: dict[str, str] = {}
    for elem in root.findall('.//{*}references/{*}*'):
        eid = elem.get('eId')
        show_as = elem.get('showAs')
        if eid and show_as:
            refs[eid] = show_as

    def resolve(href: str | None) -> str:
        if not href:
            return ''
        return refs.get(href.lstrip('#'), href.lstrip('#'))

    date_elem = root.find('.//{*}FRBRWork/{*}FRBRdate')
    debate_date = date_elem.get('date', '') if date_elem is not None else ''
    year = int(debate_date[:4]) if debate_date else None

    author_elem = root.find('.//{*}FRBRWork/{*}FRBRauthor')
    author_href = author_elem.get('href', '') if author_elem is not None else ''
    if '/committee/' in author_href.lower():
        house = 'Committee'
    elif '/dail/' in author_href.lower():
        house = 'Dáil'
    elif '/seanad/' in author_href.lower():
        house = 'Seanad'
    else:
        house = ''

    results = []

    for section in root.findall('.//{*}debateSection', NS):
        heading_elem = section.find('akn:heading', NS)
        heading = ''
        if heading_elem is not None:
            heading = (heading_elem.text or '').strip()
            for child in heading_elem:
                tail = (child.tail or '').strip()
                if tail:
                    heading += ' ' + tail
            heading = heading.strip()

        questions = []
        for q in section.findall('akn:question', NS):
            p = q.find('akn:p', NS)
            if p is None:
                continue
            raw = get_text(p)
            pq_num = extract_pq_number(raw)
            asked_by = resolve(q.get('by'))
            directed_to = resolve(q.get('to'))
            clean = clean_question_text(p)

            questions.append({
                'pq_id':            q.get('eId', ''),
                'number':           pq_num,
                'asking_person':    asked_by,
                'directed_to_role': directed_to,
                'question_clean':   clean,
            })

        if not questions:
            continue

        speeches = section.findall('akn:speech', NS)
        if not speeches:
            continue

        answered_by    = resolve(speeches[0].get('by'))
        answering_role = resolve(speeches[0].get('as'))

        paras = [
            get_text(p)
            for speech in speeches
            for p in speech.findall('akn:p', NS)
            if get_text(p)
        ]
        answer_text = clean_answer_text(paras)

        roles           = list(dict.fromkeys(q['directed_to_role'] for q in questions))
        department_role = roles[0] if len(roles) == 1 else '; '.join(roles)

        section_id = section.get('eId', '')

        results.append({
            'id':               make_id(debate_date, section_id, house),
            'date':             debate_date,
            'year':             year,
            'house':            house,
            'heading':          heading,
            'section_id':       section_id,
            'department_role':  department_role,
            'answering_person': answered_by,
            'answering_role':   answering_role,
            'question_numbers': [q['number'] for q in questions if q['number'] is not None],
            'questions':        [q['question_clean'] for q in questions if q['question_clean']],
            'answer_clean':     answer_text,
        })

    return results


# ─────────────────────────────────────────────
# STAGE 1: XML -> JSON
# ─────────────────────────────────────────────

def process_one_xml(args: tuple[Path, Path]) -> tuple[str, int, str | None]:
    xml_path, json_dir = args
    try:
        records = parse_written_answers(str(xml_path))
        if not records:
            return (str(xml_path), 0, None)

        try:
            akn_idx  = xml_path.parts.index('akn')
            rel_path = Path(*xml_path.parts[akn_idx:]).with_suffix('.json')
        except ValueError:
            rel_path = Path(xml_path.stem + '.json')

        out_path = json_dir / rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False)

        return (str(xml_path), len(records), None)

    except Exception as e:
        return (str(xml_path), 0, str(e))


def stage1_xml_to_json(xml_dir: Path, json_dir: Path, workers: int):
    log.info(f"Stage 1: scanning for XML files in {xml_dir}")
    xml_files = list(xml_dir.rglob('*.xml'))
    total     = len(xml_files)
    log.info(f"Found {total:,} XML files")

    pending = []
    skipped = 0
    for xml_path in xml_files:
        try:
            akn_idx  = xml_path.parts.index('akn')
            rel_path = Path(*xml_path.parts[akn_idx:]).with_suffix('.json')
        except ValueError:
            rel_path = Path(xml_path.stem + '.json')
        json_path = json_dir / rel_path
        if json_path.exists() and json_path.stat().st_size > 0:
            skipped += 1
        else:
            pending.append(xml_path)

    log.info(f"Skipping {skipped:,} already converted, {len(pending):,} remaining")

    if not pending:
        log.info("All XML files already converted.")
        return

    args      = [(p, json_dir) for p in pending]
    completed = 0
    errors    = 0
    total_records = 0
    start     = time.time()

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_one_xml, arg): arg for arg in args}
        for future in as_completed(futures):
            xml_path, record_count, error = future.result()
            completed += 1
            if error:
                errors += 1
                log.warning(f"Error in {xml_path}: {error}")
            else:
                total_records += record_count

            if completed % 500 == 0:
                elapsed   = time.time() - start
                rate      = completed / elapsed
                remaining = (len(pending) - completed) / rate if rate > 0 else 0
                log.info(
                    f"Stage 1: {completed:,}/{len(pending):,} "
                    f"({100*completed/len(pending):.1f}%) "
                    f"rate={rate:.1f}/s "
                    f"ETA={remaining/60:.1f}min "
                    f"records={total_records:,} "
                    f"errors={errors}"
                )

    log.info(f"Stage 1 complete: {total_records:,} records, {errors} errors")


# ─────────────────────────────────────────────
# STAGE 2: JSON -> Parquet
# ─────────────────────────────────────────────

PARQUET_SCHEMA = pa.schema([
    pa.field('id',               pa.string()),
    pa.field('date',             pa.string()),
    pa.field('year',             pa.int16()),
    pa.field('house',            pa.string()),
    pa.field('heading',          pa.string()),
    pa.field('section_id',       pa.string()),
    pa.field('department_role',  pa.string()),
    pa.field('answering_person', pa.string()),
    pa.field('answering_role',   pa.string()),
    pa.field('question_numbers', pa.list_(pa.int32())),
    pa.field('questions',        pa.list_(pa.string())),
    pa.field('answer_clean',     pa.string()),
])


def records_to_arrow_table(records: list[dict]) -> pa.Table:
    ids               = []
    dates             = []
    years             = []
    houses            = []
    headings          = []
    section_ids       = []
    department_roles  = []
    answering_persons = []
    answering_roles   = []
    question_numbers  = []
    questions_col     = []
    answer_cleans     = []

    for r in records:
        ids.append(r.get('id', ''))
        dates.append(r.get('date', ''))
        years.append(r.get('year'))
        houses.append(r.get('house', ''))
        headings.append(r.get('heading', ''))
        section_ids.append(r.get('section_id', ''))
        department_roles.append(r.get('department_role', ''))
        answering_persons.append(r.get('answering_person', ''))
        answering_roles.append(r.get('answering_role', ''))
        question_numbers.append(r.get('question_numbers', []))
        answer_cleans.append(r.get('answer_clean', ''))
        questions_col.append([
            str(q) for q in (r.get('questions') or []) if q
        ])

    return pa.table(
        {
            'id':               pa.array(ids,               type=pa.string()),
            'date':             pa.array(dates,             type=pa.string()),
            'year':             pa.array(years,             type=pa.int16()),
            'house':            pa.array(houses,            type=pa.string()),
            'heading':          pa.array(headings,          type=pa.string()),
            'section_id':       pa.array(section_ids,       type=pa.string()),
            'department_role':  pa.array(department_roles,  type=pa.string()),
            'answering_person': pa.array(answering_persons, type=pa.string()),
            'answering_role':   pa.array(answering_roles,   type=pa.string()),
            'question_numbers': pa.array(question_numbers,  type=pa.list_(pa.int32())),
            'questions':        pa.array(questions_col,     type=pa.list_(pa.string())),
            'answer_clean':     pa.array(answer_cleans,     type=pa.string()),
        },
        schema=PARQUET_SCHEMA,
    )


def stage2_json_to_parquet(json_dir: Path, parquet_out: Path):
    log.info(f"Stage 2: scanning for JSON files in {json_dir}")
    json_files = list(json_dir.rglob('*.json'))
    total      = len(json_files)
    log.info(f"Found {total:,} JSON files")

    parquet_out.parent.mkdir(parents=True, exist_ok=True)
    writer     = None
    batch: list[dict] = []
    total_rows = 0
    errors     = 0
    start      = time.time()

    for i, json_path in enumerate(json_files):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                records = json.load(f)
            batch.extend(records)
        except Exception as e:
            errors += 1
            log.warning(f"Error reading {json_path}: {e}")

        if len(batch) >= PARQUET_BATCH:
            table = records_to_arrow_table(batch)
            if writer is None:
                writer = pq.ParquetWriter(parquet_out, PARQUET_SCHEMA, compression='snappy')
            writer.write_table(table)
            total_rows += len(batch)
            batch = []

        if (i + 1) % 1000 == 0:
            elapsed   = time.time() - start
            rate      = (i + 1) / elapsed
            remaining = (total - i - 1) / rate if rate > 0 else 0
            log.info(
                f"Stage 2: {i+1:,}/{total:,} files "
                f"({100*(i+1)/total:.1f}%) "
                f"rate={rate:.1f}/s "
                f"ETA={remaining/60:.1f}min "
                f"rows={total_rows:,}"
            )

    if batch:
        table = records_to_arrow_table(batch)
        if writer is None:
            writer = pq.ParquetWriter(parquet_out, PARQUET_SCHEMA, compression='snappy')
        writer.write_table(table)
        total_rows += len(batch)

    if writer:
        writer.close()

    elapsed = time.time() - start
    log.info(f"Stage 2 complete: {total_rows:,} rows -> {parquet_out} in {elapsed:.1f}s, {errors} errors")
    log.info(f"Parquet file size: {parquet_out.stat().st_size / 1024**2:.1f} MB")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Parse Oireachtas XML -> JSON -> Parquet')
    parser.add_argument('--xml-dir',     required=True,  help='Root directory of downloaded XML files')
    parser.add_argument('--json-dir',    required=True,  help='Output directory for JSON files')
    parser.add_argument('--parquet-out', required=True,  help='Output path for final Parquet file')
    parser.add_argument('--workers',     type=int, default=XML_WORKERS, help='Parallel XML parse workers')
    parser.add_argument('--stage',       type=int, default=0, help='Run only stage 1 or 2 (default: both)')
    args = parser.parse_args()

    xml_dir     = Path(args.xml_dir)
    json_dir    = Path(args.json_dir)
    parquet_out = Path(args.parquet_out)

    if args.stage in (0, 1):
        stage1_xml_to_json(xml_dir, json_dir, args.workers)

    if args.stage in (0, 2):
        stage2_json_to_parquet(json_dir, parquet_out)


if __name__ == '__main__':
    main()
