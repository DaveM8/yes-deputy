#!/usr/bin/env python3
"""
Scan all XML files in questions_xml for structural anomalies beyond tables.
Reports counts and examples of every unusual element found inside <speech> or <question> blocks.

Usage:
    python scan_anomalies.py [--xml-dir questions_xml] [--workers 8] [--examples 3]
"""

import argparse
import json
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from lxml import etree

NS_AKN = 'http://docs.oasis-open.org/legaldocml/ns/akn/3.0/CSD13'

# Elements we already know about and handle correctly
KNOWN_INLINE = {'b', 'i', 'a', 'br', 'sup', 'sub', 'span', 'from', 'recordedTime'}
KNOWN_BLOCK  = {'p', 'table', 'ul', 'ol', 'li', 'blockList', 'item', 'intro', 'embeddedStructure'}


def localname(tag: str) -> str:
    """Strip namespace URI, return bare local name."""
    if tag.startswith('{'):
        return tag.split('}', 1)[1]
    return tag


def scan_file(xml_path: Path) -> dict:
    """
    Parse one XML file and return a dict of findings:
      - element_counts: Counter of unusual element local-names found in speech/question
      - examples: {element_name: [{"file": ..., "context": ...}, ...]}
      - flags: set of string flags for special conditions
    """
    results = {
        'element_counts': Counter(),
        'examples': defaultdict(list),
        'flags': set(),
        'file': str(xml_path),
    }

    try:
        parser = etree.XMLParser(encoding='utf-8', recover=True)
        tree = etree.parse(str(xml_path), parser)
        root = tree.getroot()
    except Exception as e:
        results['flags'].add(f'PARSE_ERROR:{e}')
        return results

    # Look inside speech and question elements
    content_blocks = (
        root.findall('.//{*}speech') +
        root.findall('.//{*}question')
    )

    for block in content_blocks:
        block_tag = localname(block.tag)

        for elem in block.iter():
            # Skip comments, processing instructions (tag is callable, not str)
            if not isinstance(elem.tag, str):
                continue
            tag = localname(elem.tag)

            # Skip the container itself
            if tag == block_tag:
                continue

            # Flag anything not in our known sets
            if tag not in KNOWN_INLINE and tag not in KNOWN_BLOCK:
                results['element_counts'][tag] += 1
                if len(results['examples'][tag]) < 3:
                    # Capture a text snippet as context
                    text = (elem.text or '').strip()[:120]
                    results['examples'][tag].append({
                        'file': str(xml_path),
                        'text': text,
                        'attribs': dict(elem.attrib),
                    })

            # Check table structure in detail
            if tag == 'table':
                rows = elem.findall('.//{*}tr')
                cols = max(
                    (len(r.findall('.//{*}td')) + len(r.findall('.//{*}th'))
                     for r in rows),
                    default=0
                )
                results['element_counts']['table'] += 1
                if cols == 0:
                    results['flags'].add('EMPTY_TABLE')

            # Nested tables
            if tag == 'table':
                for nested in elem.findall('.//{*}table'):
                    results['element_counts']['nested_table'] += 1
                    results['flags'].add('NESTED_TABLE')

        # Check for speech with no <p> children (bare text)
        if block_tag == 'speech':
            paras = block.findall('{%s}p' % NS_AKN)
            if not paras:
                paras = block.findall('p')
            if not paras:
                # Check if there's any text at all
                raw = ''.join(block.itertext()).strip()
                if raw:
                    results['flags'].add('SPEECH_NO_P')
                    results['element_counts']['speech_no_p'] += 1

        # Flag questions with no <p>
        if block_tag == 'question':
            paras = block.findall('{%s}p' % NS_AKN) or block.findall('p')
            if not paras:
                results['flags'].add('QUESTION_NO_P')
                results['element_counts']['question_no_p'] += 1

    # Check for sections with multiple <speech> elements (multi-part answers)
    for section in root.findall('.//{*}debateSection'):
        speeches = section.findall('{%s}speech' % NS_AKN) or section.findall('speech')
        if len(speeches) > 1:
            results['flags'].add('MULTI_SPEECH')
            results['element_counts']['multi_speech_section'] += 1

    # Check for sections with zero questions
    for section in root.findall('.//{*}debateSection'):
        qs = section.findall('{%s}question' % NS_AKN) or section.findall('question')
        sp = section.findall('{%s}speech' % NS_AKN) or section.findall('speech')
        if not qs and sp:
            results['flags'].add('SECTION_NO_QUESTION')
            results['element_counts']['section_no_question'] += 1
        if qs and not sp:
            results['flags'].add('SECTION_NO_SPEECH')
            results['element_counts']['section_no_speech'] += 1

    return results


def merge(accum: dict, result: dict):
    accum['element_counts'].update(result['element_counts'])
    for tag, exs in result['examples'].items():
        existing = accum['examples'][tag]
        for ex in exs:
            if len(existing) < 5:
                existing.append(ex)
    accum['flags'].update(result['flags'])
    accum['file_count'] += 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--xml-dir',  default='questions_xml')
    parser.add_argument('--workers',  type=int, default=8)
    parser.add_argument('--out',      default='anomaly_report.json')
    args = parser.parse_args()

    xml_dir = Path(args.xml_dir)
    xml_files = list(xml_dir.rglob('*.xml'))
    total = len(xml_files)
    print(f"Scanning {total:,} XML files with {args.workers} workers...")

    accum = {
        'element_counts': Counter(),
        'examples': defaultdict(list),
        'flags': set(),
        'file_count': 0,
    }

    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(scan_file, p): p for p in xml_files}
        for fut in as_completed(futures):
            result = fut.result()
            merge(accum, result)
            done += 1
            if done % 50000 == 0:
                pct = 100 * done / total
                print(f"  {done:,}/{total:,} ({pct:.1f}%)  flags so far: {sorted(accum['flags'])}")

    # Sort by count descending
    counts_sorted = sorted(accum['element_counts'].items(), key=lambda x: -x[1])

    report = {
        'files_scanned': accum['file_count'],
        'element_counts': counts_sorted,
        'flags_seen': sorted(accum['flags']),
        'examples': {tag: exs for tag, exs in accum['examples'].items()},
    }

    # Print summary to stdout
    print("\n" + "="*60)
    print(f"Files scanned : {accum['file_count']:,}")
    print(f"Flags seen    : {', '.join(sorted(accum['flags']))}")
    print("\nElement counts (inside <speech>/<question>):")
    for tag, count in counts_sorted:
        print(f"  {tag:<30} {count:>10,}")

    print("\nExamples (up to 5 per element):")
    for tag, exs in sorted(accum['examples'].items()):
        print(f"\n  [{tag}]")
        for ex in exs:
            print(f"    file   : {Path(ex['file']).name}")
            print(f"    attribs: {ex['attribs']}")
            print(f"    text   : {ex['text']!r}")

    # Write full JSON report
    out_path = Path(args.out)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nFull report written to {out_path}")


if __name__ == '__main__':
    main()
