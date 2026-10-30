#!/usr/bin/env python3
"""
Chunk bill and Act PDFs for RAG.

Input: bills_metadata.jsonl and bills_pdf/ from update_bills.py.
Output: JSONL chunks in the same shape as chunk_for_rag.py, for embed_and_index.py.

For each bill, one document is indexed: the English Act if the bill was enacted,
otherwise its latest English version. Its explanatory memorandum is indexed too.
Every chunk carries the bill's metadata (status, sponsors, version, PDF page).

Text extraction
---------------
The PDFs (all with a text layer; pre-1990s ones were OCR'd) put margin notes and
line numbers ("5", "10", "15") beside the body text, and OCR often merges a
line number into the line next to it. So text is taken word by word, and words
outside the page's body column are dropped. Running headers, page numbers and
the contents pages go too. The contents list is kept as a section-number ->
section-title map for headings.

Chunking
--------
Statutory text is split at section starts ("3.—(1) ...", or "3. (1) ..." in
recent Acts), with section numbers required to run in order so numbered lists
in Schedules don't start new sections. Each chunk is one section, or several
short ones, or part of a long one split at subsection boundaries, prefixed with
"{title}, section N (heading): ". Chunk 0 of each bill is an overview built from
the API metadata (long title, sponsors, status, stages). Memos and bills where
no sections are found are packed by paragraph.

Usage
-----
    python chunk_bills.py --data-dir /data/members_questions --out bills_chunks.jsonl
    python chunk_bills.py --data-dir /data/members_questions --sample 30 --out sample.jsonl
    python chunk_bills.py --data-dir /data/members_questions --bill 2007/31 --out one.jsonl
"""

import argparse
import hashlib
import html
import json
import logging
import random
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import pymupdf

from chunk_for_rag import make_chunk_id

MAX_CHARS        = 1500
MIN_SECTION_CHARS = 300     # shorter sections are merged with the next one
WANTED_LANGS     = ("eng", "mul")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler("chunk_bills.log"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

pymupdf.TOOLS.mupdf_display_errors(False)   # damaged-xref warnings on some old scans


# ─────────────────────────────────────────────
# CHOOSING DOCUMENTS
# ─────────────────────────────────────────────

def _docs(bill: dict, kind: str) -> list[dict]:
    return [next(iter(w.values())) for w in bill.get(kind) or []]


def _pdf_uri(doc: dict) -> str | None:
    return ((doc.get("formats") or {}).get("pdf") or {}).get("uri")


def choose_documents(bill: dict) -> list[tuple[str, dict]]:
    """[(role, doc)] where role is "act", "bill" or "memo"."""
    versions = [v for v in _docs(bill, "versions")
                if v.get("lang") in WANTED_LANGS and _pdf_uri(v)]
    acts = [v for v in versions if v.get("docType") == "act"]
    bills = [v for v in versions if v.get("docType") == "bill"]
    chosen: list[tuple[str, dict]] = []
    # Sorting is stable, so same-day versions keep the API's order (last = latest stage)
    if acts:
        chosen.append(("act", sorted(acts, key=lambda v: v.get("date") or "")[-1]))
    elif bills:
        chosen.append(("bill", sorted(bills, key=lambda v: v.get("date") or "")[-1]))
    memos = [d for d in _docs(bill, "relatedDocs")
             if d.get("docType") == "memo" and d.get("lang") in WANTED_LANGS and _pdf_uri(d)]
    if memos:
        chosen.append(("memo", sorted(memos, key=lambda d: d.get("date") or "")[-1]))
    return chosen


# ─────────────────────────────────────────────
# PDF TEXT EXTRACTION
# ─────────────────────────────────────────────

# Page and line numbers; "3." is kept, since a section number can sit on a line of its own
_BARE_NUMBER = re.compile(r"^\d{1,3}$")
_SECTION_NUMBER = re.compile(r"^\d{1,3}[A-Z]{0,2}\.$")
_PURE_NUMBER = re.compile(r"^[\d\s,|]*$|^[\W_]+$")


def _page_lines(page) -> list[str]:
    """Body-column text lines of one page, with margin notes and line numbers dropped."""
    words = page.get_text("words", sort=False)
    if not words:
        return []
    lines: dict[tuple[int, int], list] = {}
    for w in words:
        lines.setdefault((w[5], w[6]), []).append(w)

    # The body column is where the widest lines sit. Bare numbers are left out of
    # the measurement: OCR puts margin line numbers at the start of the body line.
    extents = []
    for ws in lines.values():
        text_words = [w for w in ws if not _BARE_NUMBER.match(w[4])]
        if text_words:
            extents.append((min(w[0] for w in text_words), max(w[2] for w in text_words)))
    if not extents:
        return []
    widest = max(x1 - x0 for x0, x1 in extents)
    # Margin notes are narrow, so lines at least 30% of the widest are body text
    wide = sorted((x0, x1) for x0, x1 in extents if x1 - x0 >= 0.3 * widest)
    if len(wide) >= 4 and widest > 0.3 * page.rect.width:
        # The outermost edge that at least two wide lines share (indented
        # subsections are often the majority, so a percentile sits too far in)
        lefts = sorted(x0 for x0, _ in wide)
        rights = sorted((x1 for _, x1 in wide), reverse=True)
        # (Ragged OCR edges may share none: fall back to a percentile)
        left = next((x for x in lefts if sum(abs(l - x) <= 3 for l in lefts) >= 2),
                    lefts[len(lefts) // 10]) - 4
        right = next((x for x in rights if sum(abs(r - x) <= 3 for r in rights) >= 2),
                     rights[len(rights) // 10]) + 4
    else:
        left, right = float("-inf"), float("inf")

    out = []
    for ws in lines.values():
        # Section numbers ("12.") can hang outside the column, in the contents list
        # especially; margin line numbers never have the full stop
        kept = [w[4] for w in sorted(ws, key=lambda w: w[7])
                if left <= (w[0] + w[2]) / 2 <= right
                or (w[7] == 0 and _SECTION_NUMBER.match(w[4]))]
        text = " ".join(kept).strip()
        if text and not _PURE_NUMBER.match(text):
            out.append(text)
    return out


def _header_key(line: str) -> str:
    return re.sub(r"[^a-z]", "", line.lower())


def extract_pages(pdf_path: Path) -> list[list[str]]:
    """Lines per page, with running headers and footers removed."""
    with pymupdf.open(pdf_path) as doc:
        pages = [_page_lines(p) for p in doc]
    # A line that recurs on many pages is a running header ("[No. 4.] Telecommunications ...")
    counts = Counter()
    for lines in pages:
        counts.update({_header_key(l) for l in lines[:3] + lines[-3:] if len(l) <= 90})
    threshold = max(3, len(pages) // 4)
    repeated = {k for k, n in counts.items() if n >= threshold and len(k) >= 4}
    return [[l for l in lines if _header_key(l) not in repeated] for lines in pages]


# ─────────────────────────────────────────────
# TEXT HELPERS
# ─────────────────────────────────────────────

# Running header fragments that vary per page, so the repeat check misses them
_RUNNING_REF = re.compile(r"\[(?:No\.\s*\d+\.?|\d{4}\.?)\]")


def join_lines(lines: list[str]) -> str:
    """Join PDF lines into running text, undoing end-of-line hyphenation."""
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if out.endswith("\xad"):
            out = out[:-1] + line
        elif (out.endswith("-") and len(out) > 1 and out[-2].isalpha()
              and line[:1].islower()):
            out = out[:-1] + line
        else:
            out = f"{out} {line}" if out else line
    out = _RUNNING_REF.sub("", out.replace("\xad", ""))
    return re.sub(r"\s+", " ", out).strip()


def split_long(text: str, limit: int) -> list[str]:
    """Split text at sentence (or clause) boundaries into pieces of at most `limit`."""
    if len(text) <= limit:
        return [text]
    pieces, current = [], ""
    for part in re.split(r"(?<=[.;:])\s+", text):
        while len(part) > limit:                  # a single enormous clause
            cut = part.rfind(" ", 0, limit) or limit
            pieces.append((current + " " + part[:cut]).strip() if current else part[:cut])
            current, part = "", part[cut:].strip()
        if current and len(current) + 1 + len(part) > limit:
            pieces.append(current)
            current = part
        else:
            current = f"{current} {part}" if current else part
    if current:
        pieces.append(current)
    return pieces


def pack(paragraphs: list[str], limit: int) -> list[str]:
    """Pack paragraphs into chunks of at most `limit` characters."""
    chunks, current = [], ""
    for para in paragraphs:
        for piece in split_long(para, limit):
            if current and len(current) + 1 + len(piece) > limit:
                chunks.append(current)
                current = piece
            else:
                current = f"{current}\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def strip_html(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


# ─────────────────────────────────────────────
# STATUTE STRUCTURE
# ─────────────────────────────────────────────

ENACTING_RE  = re.compile(r"BE\s*IT\s*ENACTED", re.IGNORECASE)
SECTION_RE   = re.compile(r"^(\d{1,3})([A-Z]{0,2})\s*(?:\.\s*(?:[—–_-]+\s*)?|[—–_]+\s*)(?=[(‘“\"'A-Z]|$)")
FIRST_SECTION_RE = re.compile(r"^1\s*\.?\s*[—–_-]+\s*[(A-Z‘“]")
# The body ends where an appended memorandum or the list of Acts referred to starts
BODY_END_RE  = re.compile(r"^(?:EXPLANATORY (?:AND FINANCIAL )?MEMORANDUM|ACTS REFERRED TO)\b",
                          re.IGNORECASE)
# Recent bills and Acts end with the Irish text, which starts with its own long title
IRISH_RE     = re.compile(r"\bd[aá] ngairtear\b|ACHTA[IÍ]TEAR", re.IGNORECASE)
SUBPARA_RE   = re.compile(r"^\((?:\d+[A-Z]?|[a-z]{1,4}|[ivxl]+)\)")
PART_RE      = re.compile(r"^(PART|CHAPTER)\s+([0-9IVXLC]+[A-Z]?)\b\s*(.*)$")
SCHEDULE_RE  = re.compile(r"^(?:FIRST |SECOND |THIRD |FOURTH |FIFTH )?SCHEDULE\b(?:\s+\d+)?")
CONTENTS_NUM = re.compile(r"^(\d{1,3}[A-Z]{0,2})\.\s*(.*)$")


def contents_titles(front: list[str]) -> dict[str, str]:
    """Section number -> title, from the "Arrangement of Sections"/"Contents" list."""
    titles: dict[str, str] = {}
    current = None
    for line in front:
        if BODY_END_RE.match(line):
            break
        if PART_RE.match(line) or SCHEDULE_RE.match(line):
            current = None
            continue
        m = CONTENTS_NUM.match(line)
        if m:
            current = m.group(1)
            titles[current] = m.group(2).strip()
        elif current and len(titles[current]) < 150:
            titles[current] = f"{titles[current]} {line}".strip()
    return {k: v.rstrip(" .") for k, v in titles.items()}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _next_section_ok(num: int, suffix: str, last: int) -> bool:
    """Sections run in order; allow small gaps, since OCR sometimes garbles one."""
    if last == 0:
        return num in (1, 2)
    return last < num <= last + 3 or bool(suffix and num == last)


def split_sections(pages: list[list[str]]):
    """
    Returns (front_lines, units) where units are dicts:
    {"kind": "section"|"schedule"|"preamble", "num", "part", "lines": [(page, line)]}
    or None if no enacting formula or sections were found.
    """
    flat = [(p, line) for p, lines in enumerate(pages) for line in lines]
    start = next((i for i, (_, l) in enumerate(flat) if ENACTING_RE.search(l)), None)
    if start is None:
        # OCR sometimes garbles the enacting words: start before "1.—", which the
        # contents list (number and title only) doesn't have
        start = next((i - 1 for i, (_, l) in enumerate(flat) if FIRST_SECTION_RE.match(l)), None)
    if start is None:
        return None
    front = [l for _, l in flat[:start]]
    # Cut at the Irish text or an appended memo, but only once sections have
    # appeared: older bilingual bills print the Irish formula alongside the English
    end, seen_section = len(flat), False
    for i in range(start + 1, len(flat)):
        line = flat[i][1]
        if SECTION_RE.match(line):
            seen_section = True
        elif seen_section and (IRISH_RE.search(line) or BODY_END_RE.match(line)):
            end = i
            break
    flat = flat[:end]
    units: list[dict] = []
    current = {"kind": "preamble", "num": None, "part": "", "lines": [], "title": ""}
    part = ""
    last = 0
    pending_part = None          # a PART line whose title is on the following line
    for page, line in flat[start + 1:]:
        if pending_part is not None:
            if not SECTION_RE.match(line) and len(line) < 120:
                part = f"{pending_part} – {line.title() if line.isupper() else line}"
                pending_part = None
                continue
            pending_part = None
        m_part = PART_RE.match(line)
        if m_part and line.split()[0].isupper():
            kind, num, rest = m_part.groups()
            part = f"{kind.title()} {num}"
            rest = rest.strip(" .—–-")
            if rest:
                part = f"{part} – {rest.title() if rest.isupper() else rest}"
            else:
                pending_part = part
            continue
        if SCHEDULE_RE.match(line) and line.split()[0].isupper():
            units.append(current)
            current = {"kind": "schedule", "num": line.strip(), "part": "", "lines": [],
                       "title": ""}
            continue
        m = SECTION_RE.match(line)
        if m and current["kind"] != "schedule" and _next_section_ok(int(m.group(1)), m.group(2), last):
            # Recent Acts print the section heading on its own line above the section
            heading = ""
            if current["lines"]:
                prev = current["lines"][-1][1]
                if len(prev) < 100 and not re.search(r"[.;:,—–-]$", prev) and prev[:1].isupper():
                    heading = prev.rstrip(" .")
                    current["lines"].pop()
            units.append(current)
            last = int(m.group(1))
            current = {"kind": "section", "num": m.group(1) + m.group(2), "part": part,
                       "lines": [(page, line)], "title": heading}
            continue
        current["lines"].append((page, line))
    units.append(current)
    units = [u for u in units if u["lines"] or u["kind"] == "schedule"]
    if not any(u["kind"] == "section" for u in units):
        return None
    return front, units


def paragraphs_of(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Group a unit's lines into (page, paragraph) at subsection/paragraph starts."""
    paras: list[tuple[int, list[str]]] = []
    for page, line in lines:
        lone_number = paras and len(paras[-1][1]) == 1 and SECTION_RE.match(paras[-1][1][0]) \
            and paras[-1][1][0].rstrip(" .—–-").isalnum()
        if not paras or (SUBPARA_RE.match(line) and not lone_number):
            paras.append((page, [line]))
        else:
            paras[-1][1].append(line)
    return [(page, join_lines(ls)) for page, ls in paras]


# ─────────────────────────────────────────────
# CHUNK BUILDING
# ─────────────────────────────────────────────

def make_bill_record_id(doc_uri: str) -> str:
    return hashlib.sha256(f"bill|{doc_uri}".encode()).hexdigest()


def _house(bill: dict) -> str:
    show = ((bill.get("originHouse") or {}).get("showAs") or "")
    return "Seanad" if show.startswith("Seanad") else "Dáil"


def _sponsors(bill: dict) -> tuple[list[str], list[str]]:
    names, roles = [], []
    for s in bill.get("sponsors") or []:
        sp = s.get("sponsor") or {}
        name = (sp.get("by") or {}).get("showAs")
        role = (sp.get("as") or {}).get("showAs")
        if name:
            names.append(name)
        if role:
            roles.append(role)
    return names, roles


def bill_metadata(bill: dict, role: str, doc: dict) -> dict:
    act = bill.get("act") or {}
    names, _ = _sponsors(bill)
    date = (act.get("dateSigned") if role == "act" else None) or doc.get("date") or ""
    title = (act.get("shortTitleEn") if role == "act" else None) or bill.get("shortTitleEn") or ""
    return {
        "date":          date[:10] or None,
        "year":          int(date[:4]) if date[:4].isdigit() else int(bill["billYear"]),
        "house":         _house(bill),
        "committee":     "",
        "doc_type":      "bill",
        "heading":       re.sub(r"\s+", " ", title).strip(),
        "bill_uri":      bill.get("uri"),
        "bill_no":       int(bill["billNo"]) if str(bill.get("billNo", "")).isdigit() else None,
        "bill_year":     int(bill["billYear"]),
        "bill_status":   bill.get("status"),
        "bill_source":   bill.get("source"),
        "sponsors":      names,
        "version_label": "Act" if role == "act" else ("Explanatory Memorandum" if role == "memo"
                                                       else doc.get("showAs")),
        "act_no":        int(act["actNo"]) if str(act.get("actNo", "")).isdigit() else None,
        "pdf_url":       _pdf_uri(doc),
    }


def overview_text(bill: dict) -> str:
    act = bill.get("act") or {}
    names, roles = _sponsors(bill)
    parts = [f"{bill.get('shortTitleEn', '').strip()} (Bill No. {bill['billNo']} of {bill['billYear']})."]
    long_title = strip_html(bill.get("longTitleEn"))
    if long_title:
        parts.append(long_title)
    who = ", ".join(names + [r for r in roles if r not in names])
    origin = (bill.get("originHouse") or {}).get("showAs") or ""
    parts.append(f"{bill.get('source') or ''} bill"
                 + (f" introduced in {origin}" if origin else "")
                 + (f", sponsored by {who}" if who else "") + ".")
    stages = []
    for s in bill.get("stages") or []:
        ev = s.get("event") or {}
        dates = [d.get("date") for d in ev.get("dates") or [] if d.get("date")]
        house = (ev.get("house") or {}).get("showAs") or (ev.get("chamber") or {}).get("showAs") or ""
        if ev.get("showAs"):
            stages.append(f"{ev['showAs']} ({house}{', ' + dates[-1] if dates else ''})")
    if stages:
        parts.append("Stages: " + "; ".join(stages) + ".")
    parts.append(f"Status: {bill.get('status')}.")
    if act:
        parts.append(f"Enacted as {act.get('shortTitleEn', '').strip()} "
                     f"(Act No. {act.get('actNo')} of {act.get('actYear')}), "
                     f"signed {act.get('dateSigned', '')[:10]}.")
    return " ".join(p for p in parts if p)


def chunk_document(bill: dict, role: str, doc: dict, pdf_dir: Path,
                   max_chars: int = MAX_CHARS) -> list[dict]:
    uri = _pdf_uri(doc)
    path = pdf_dir / urlparse(uri).path.lstrip("/")
    if not path.exists():
        log.warning(f"Missing PDF {path}")
        return []
    pages = extract_pages(path)
    meta = bill_metadata(bill, role, doc)
    title = meta["heading"]
    record_id = make_bill_record_id(doc.get("uri") or uri)

    # (text, heading_path suffix, section_type, section_id, page)
    pieces: list[tuple[str, str, str, str, int]] = []
    if role != "memo":
        for text in split_long(overview_text(bill), max_chars):
            pieces.append((text, "Overview", "overview", "", 0))

    parsed = split_sections(pages) if role != "memo" else None
    if parsed:
        front, units = parsed
        titles = contents_titles(front)
        i = 0
        while i < len(units):
            group = [units[i]]
            # Merge runs of short sections in the same Part
            while (group[-1]["kind"] == "section" and i + len(group) < len(units)
                   and units[i + len(group)]["kind"] == "section"
                   and units[i + len(group)]["part"] == group[0]["part"]
                   and sum(len(join_lines([l for _, l in u["lines"]])) for u in group) < MIN_SECTION_CHARS):
                group.append(units[i + len(group)])
            i += len(group)
            first, lastu = group[0], group[-1]
            if first["kind"] == "section":
                label = (f"section {first['num']}" if len(group) == 1
                         else f"sections {first['num']}–{lastu['num']}")
                heads = [u["title"] or titles.get(u["num"], "") for u in group]
                head = "; ".join(h for h in heads if h)
                section_type, section_id = "section", f"s{first['num']}"
            elif first["kind"] == "schedule":
                label, head = first["num"].title(), ""
                section_type, section_id = "schedule", _norm(first["num"])
            else:
                label, head = "preamble", ""
                section_type, section_id = "preamble", "preamble"
            paras = [p for u in group for p in paragraphs_of(u["lines"])]
            # Skip a preamble that's only the tail of the enacting words
            if not paras or (first["kind"] == "preamble" and sum(len(t) for _, t in paras) < 100):
                continue
            prefix = f"{title}, {label}" + (f" ({head})" if head else "") + ": "
            path_parts = [title] + ([first["part"]] if first.get("part") else []) \
                         + [label[0].upper() + label[1:] + (f" {head}" if head else "")]
            page_of = paras[0][0]
            for text in pack([t for _, t in paras], max_chars - len(prefix)):
                pieces.append((prefix + text, " > ".join(path_parts[1:]),
                               section_type, section_id, page_of))
    else:
        # Memos, and bills whose sections couldn't be found: pack by paragraph
        label = "Explanatory Memorandum" if role == "memo" else "text"
        prefix = f"{title}, {label}: "
        for p, lines in enumerate(pages):
            for text in pack([join_lines(lines)], max_chars - len(prefix)):
                if len(text) > 150:    # skip cover pages that hold only the title
                    pieces.append((prefix + text, label[0].upper() + label[1:],
                                   "memo" if role == "memo" else "text", "", p))

    chunk_type = {"act": "act_text", "bill": "bill_text", "memo": "bill_memo"}[role]
    chunks = []
    for idx, (text, sub_path, section_type, section_id, page) in enumerate(pieces):
        chunks.append({
            "id":           make_chunk_id(record_id, idx),
            "record_id":    record_id,
            "chunk_index":  idx,
            "chunk_type":   chunk_type,
            **meta,
            "section_type": section_type,
            "section_id":   section_id,
            "heading_path": f"{title} > {sub_path}" if sub_path else title,
            "page":         page + 1,
            "text":         text,
        })
    return chunks


def chunk_bill(bill: dict, pdf_dir: str, max_chars: int) -> list[dict]:
    out = []
    for role, doc in choose_documents(bill):
        try:
            out.extend(chunk_document(bill, role, doc, Path(pdf_dir), max_chars))
        except Exception as e:
            log.warning(f"Failed {bill.get('uri')} {role}: {e}")
    return out


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=Path("."),
                        help="Directory holding bills_metadata.jsonl and bills_pdf/")
    parser.add_argument("--out", type=Path, default=Path("bills_chunks.jsonl"))
    parser.add_argument("--max-chars", type=int, default=MAX_CHARS)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--sample", type=int, help="Random N bills, for checking the output")
    parser.add_argument("--bill", help="One bill, as YEAR/NO (e.g. 2007/31)")
    args = parser.parse_args()

    with open(args.data_dir / "bills_metadata.jsonl", encoding="utf-8") as f:
        bills = [json.loads(line) for line in f]
    if args.bill:
        year, no = args.bill.split("/")
        bills = [b for b in bills if b["billYear"] == year and b["billNo"] == no]
    elif args.sample:
        bills = random.Random(1).sample(bills, min(args.sample, len(bills)))

    pdf_dir = str(args.data_dir / "bills_pdf")
    no_docs = sum(1 for b in bills if not choose_documents(b))
    log.info(f"{len(bills)} bills, {no_docs} without any English document")

    total = 0
    tmp = args.out.with_suffix(".jsonl.part")
    with open(tmp, "w", encoding="utf-8") as out, \
            ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(chunk_bill, b, pdf_dir, args.max_chars) for b in bills]
        for n, fut in enumerate(as_completed(futures), 1):
            for chunk in fut.result():
                out.write(json.dumps(chunk, ensure_ascii=False) + "\n")
                total += 1
            if n % 500 == 0:
                log.info(f"{n}/{len(bills)} bills, {total} chunks")
    tmp.rename(args.out)
    log.info(f"Wrote {total} chunks from {len(bills)} bills to {args.out}")


if __name__ == "__main__":
    main()
