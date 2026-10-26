from lxml import etree
import json
import re
import sys
from pathlib import Path


NS = {'akn': 'http://docs.oasis-open.org/legaldocml/ns/akn/3.0/CSD13'}


def get_text(elem) -> str:
    return ' '.join(''.join(elem.itertext()).split())


def clean_question_text(elem) -> str:
    """
    The question text is the tail of the last <b> element in the <p>.
    Strip leading whitespace and trailing [ref/year].
    """
    # Find <b> elements - note they may be in the default namespace or none
    # Try both with and without namespace
    bold_elems = elem.findall('b')
    if not bold_elems:
        bold_elems = elem.findall('{http://docs.oasis-open.org/legaldocml/ns/akn/3.0/CSD13}b')

    if bold_elems:
        last_bold = bold_elems[-1]
        tail = (last_bold.tail or '').strip()
        # Remove trailing [NNNN/YY]
        tail = re.sub(r'\s*\[\d+/\d+\]\s*$', '', tail)
        return tail.strip()

    # Fallback: strip everything up to and including the second bold-style phrase
    raw = ' '.join(''.join(elem.itertext()).split())
    raw = re.sub(r'^\d+\.?\s*', '', raw)
    raw = re.sub(r'^.*?(?:Minister for[\w\s,]+?)\s+', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'\s*\[\d+/\d+\]\s*$', '', raw)
    return raw.strip()

def clean_answer_text(paragraphs: list[str]) -> str:
    """Join paragraphs, dropping the housekeeping 'I propose to take Questions...' line."""
    filtered = [p for p in paragraphs
                if not re.match(r'^I propose to take Questions? Nos?\.', p, re.IGNORECASE)]
    return '\n\n'.join(filtered)


def extract_pq_number(text: str) -> int | None:
    m = re.match(r'^(\d+)\.?\s', text.strip())
    return int(m.group(1)) if m else None


def parse_written_answers(xml_path: str) -> list[dict]:
    tree = etree.parse(xml_path)
    root = tree.getroot()

    # Resolve eId -> display name (captures TLCPerson, TLCRole, TLCOrganization etc.)
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

    # Date
    date_elem = root.find('.//{*}FRBRWork/{*}FRBRdate')
    debate_date = date_elem.get('date', '') if date_elem is not None else ''
    year = int(debate_date[:4]) if debate_date else None

    # House from FRBRauthor href e.g. /ie/oireachtas/house/dail/34
    author_elem = root.find('.//{*}FRBRWork/{*}FRBRauthor')
    author_href = author_elem.get('href', '') if author_elem is not None else ''
    if '/dail/' in author_href.lower():
        house = 'Dáil'
    elif '/seanad/' in author_href.lower():
        house = 'Seanad'
    else:
        house = ''

    results = []

    for section in root.findall('.//{*}debateSection', NS):

        # Heading - take only direct text, ignoring recordedTime child content
        heading_elem = section.find('akn:heading', NS)
        heading = ''
        if heading_elem is not None:
            heading = (heading_elem.text or '').strip()
            for child in heading_elem:
                tail = (child.tail or '').strip()
                if tail:
                    heading += ' ' + tail
            heading = heading.strip()

        # Collect all questions
        questions = []
        for q in section.findall('akn:question', NS):
            p = q.find('akn:p', NS)
            if p is None:
                continue
            raw = get_text(p)
            pq_num = extract_pq_number(raw)
            asked_by = resolve(q.get('by'))
            directed_to = resolve(q.get('to'))
            clean = clean_question_text(p)  # pass element for precise tail extraction

            questions.append({
                'pq_id': q.get('eId', ''),
                'number': pq_num,
                'asking_person': asked_by,
                'directed_to_role': directed_to,
                'question_clean': clean,
            })

        if not questions:
            continue

        # Single speech = the answer
        speech = section.find('akn:speech', NS)
        if speech is None:
            continue

        answered_by = resolve(speech.get('by'))
        answering_role = resolve(speech.get('as'))

        paras = [get_text(p) for p in speech.findall('akn:p', NS) if get_text(p)]
        answer_text = clean_answer_text(paras)

        # department_role: full showAs from TLCRole via resolve()
        roles = list(dict.fromkeys(q['directed_to_role'] for q in questions))
        department_role = roles[0] if len(roles) == 1 else '; '.join(roles)

        results.append({
            'date': debate_date,
            'year': year,
            'house': house,
            'heading': heading,
            'section_id': section.get('eId', ''),
            'department_role': department_role,
            'answering_person': answered_by,
            'answering_role': answering_role,
            'question_numbers': [q['number'] for q in questions],
            'questions': [
                {
                    'number': q['number'],
                    'asking_person': q['asking_person'],
                    'question_clean': q['question_clean'],
                }
                for q in questions
            ],
            'answer_clean': answer_text,
        })

    return results


if __name__ == '__main__':
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('.')
    
    if path.is_dir():
        all_records = []
        for xml_file in sorted(path.glob('**/*.xml')):
            records = parse_written_answers(str(xml_file))
            all_records.extend(records)
            print(f"  {xml_file.name}: {len(records)} sections")
        out_path = path / 'written_answers.json'
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(all_records, f, indent=2, ensure_ascii=False)
        print(f"Wrote {len(all_records)} total records to {out_path}")
    else:
        records = parse_written_answers(str(path))
        out_path = path.with_suffix('.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        print(f"Wrote {len(records)} records to {out_path}")
