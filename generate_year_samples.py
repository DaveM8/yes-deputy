"""
Generate one HTML sample per year from a randomly selected debate XML.
Output goes to /data/members_questions/year_samples/
"""

import os
import random
import html
from collections import defaultdict
from lxml import etree

BASE = '/data/members_questions/questions_xml/akn/ie/debateRecord/dail'
OUT_DIR = '/data/members_questions/year_samples'

# ── gather one random sitting day per year ──────────────────────────────────

by_year = defaultdict(list)
for date_dir in os.listdir(BASE):
    debate_xml = os.path.join(BASE, date_dir, 'debate', 'mul@', 'main.xml')
    if os.path.exists(debate_xml):
        by_year[date_dir[:4]].append((date_dir, debate_xml))

random.seed(42)  # reproducible selection
selected = {}
for year, days in sorted(by_year.items()):
    selected[year] = random.choice(days)

print(f"Generating {len(selected)} HTML files…")

# ── HTML helpers ─────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 15px;
    line-height: 1.7;
    background: #fafaf8;
    color: #1a1a1a;
    padding: 2rem 1rem;
}
.page {
    max-width: 820px;
    margin: 0 auto;
    background: #fff;
    padding: 2.5rem 3rem;
    border: 1px solid #ddd;
    box-shadow: 0 2px 8px rgba(0,0,0,.06);
}
h1 { font-size: 1.4rem; margin-bottom: .25rem; color: #111; }
.meta { font-size: .85rem; color: #666; margin-bottom: 2rem; border-bottom: 1px solid #eee; padding-bottom: 1rem; }
.meta a { color: #555; }
.section { margin-bottom: 2rem; }
.section-heading {
    font-size: 1rem;
    font-weight: bold;
    color: #333;
    margin-bottom: .5rem;
    padding: .3rem .6rem;
    background: #f4f4f0;
    border-left: 3px solid #999;
}
.speaker {
    font-size: .82rem;
    font-weight: bold;
    color: #555;
    text-transform: uppercase;
    letter-spacing: .04em;
    margin: .8rem 0 .2rem;
}
p {
    margin-bottom: .5rem;
    text-align: justify;
    hyphens: auto;
}
.nav {
    font-size: .82rem;
    margin-bottom: 1.5rem;
    color: #888;
}
.nav a { color: #555; margin: 0 .3rem; text-decoration: none; }
.nav a:hover { text-decoration: underline; }
.toc { font-size: .85rem; margin-bottom: 2rem; padding: 1rem; background: #f9f9f7; border: 1px solid #e8e8e4; }
.toc h2 { font-size: .9rem; margin-bottom: .5rem; }
.toc ol { padding-left: 1.2rem; }
.toc li { margin-bottom: .15rem; }
.toc a { color: #444; text-decoration: none; }
.toc a:hover { text-decoration: underline; }
"""

def slugify(s):
    return s.lower().replace(' ', '-').replace('/', '-').replace(',', '')

def iter_speeches(section_el):
    """Yield (speaker_name_or_None, [paragraph_texts]) within a debateSection."""
    NS = 'http://docs.oasis-open.org/legaldocml/ns/akn/3.0/CSD13'
    current_speaker = None
    current_paras = []
    for child in section_el:
        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if tag == 'speech':
            if current_paras:
                yield current_speaker, current_paras
            from_el = child.find(f'{{{NS}}}from') or child.find('.//{*}from'.replace('{*}', f'{{{NS}}}'))
            # wildcard fallback
            if from_el is None:
                from_el = next((e for e in child if e.tag.split('}')[-1] == 'from'), None)
            current_speaker = (from_el.text or '').strip() if from_el is not None else None
            paras = []
            for p in child.iter():
                if p.tag.split('}')[-1] == 'p':
                    text = ''.join(p.itertext()).strip()
                    if text:
                        paras.append(text)
            current_paras = paras
        elif tag == 'debateSection':
            # nested — skip, handled at top level
            pass
    if current_paras:
        yield current_speaker, current_paras


def local_tag(el):
    """Return the local tag name, or '' for comments/PIs whose .tag is callable."""
    t = el.tag
    if callable(t):
        return ''
    return t.split('}')[-1] if '}' in t else t


def parse_debate(xml_path):
    """Return list of {heading, speeches: [{speaker, paras}]}."""
    tree = etree.parse(xml_path)
    root = tree.getroot()
    sections = []
    for ds in root.iter():
        if local_tag(ds) != 'debateSection':
            continue
        # only top-level sections (parent is not also a debateSection)
        parent = ds.getparent()
        parent_tag = local_tag(parent) if parent is not None else ''
        if parent_tag == 'debateSection':
            continue
        # heading
        heading_el = next((e for e in ds if local_tag(e) == 'heading'), None)
        heading = ''.join(heading_el.itertext()).strip() if heading_el is not None else '(untitled)'
        # collect all speeches (including nested sections)
        speeches = []
        for sp in ds.iter():
            if local_tag(sp) != 'speech':
                continue
            from_el = next((e for e in sp if local_tag(e) == 'from'), None)
            speaker = (''.join(from_el.itertext()).strip()) if from_el is not None else None
            paras = []
            for p in sp.iter():
                if local_tag(p) == 'p':
                    text = ''.join(p.itertext()).strip()
                    if text:
                        paras.append(text)
            if paras:
                speeches.append({'speaker': speaker, 'paras': paras})
        if speeches:
            sections.append({'heading': heading, 'speeches': speeches})
    return sections


def build_html(year, date, xml_path, sections, prev_year, next_year):
    esc = html.escape
    nav_links = []
    if prev_year:
        nav_links.append(f'<a href="{prev_year}.html">← {prev_year}</a>')
    nav_links.append(f'<span>{year}</span>')
    if next_year:
        nav_links.append(f'<a href="{next_year}.html">{next_year} →</a>')
    nav_html = ' '.join(nav_links)

    # TOC
    toc_items = ''.join(
        f'<li><a href="#s{i}">{esc(s["heading"])}</a></li>'
        for i, s in enumerate(sections)
    )

    # Body sections
    body_parts = []
    for i, sec in enumerate(sections):
        sid = f's{i}'
        heading_html = f'<div class="section-heading" id="{sid}">{esc(sec["heading"])}</div>'
        speech_parts = []
        for sp in sec['speeches']:
            if sp['speaker']:
                speech_parts.append(f'<div class="speaker">{esc(sp["speaker"])}</div>')
            for para in sp['paras']:
                speech_parts.append(f'<p>{esc(para)}</p>')
        body_parts.append(
            f'<div class="section">{heading_html}{"".join(speech_parts)}</div>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dáil Debates — {date}</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">
  <div class="nav">{nav_html}</div>
  <h1>Dáil Debates — {date}</h1>
  <div class="meta">
    Year sample: <strong>{year}</strong> &nbsp;·&nbsp;
    {len(sections)} sections &nbsp;·&nbsp;
    Source: <code>{esc(xml_path)}</code>
  </div>
  <div class="toc">
    <h2>Contents</h2>
    <ol>{toc_items}</ol>
  </div>
  {"".join(body_parts)}
</div>
</body>
</html>"""


# ── generate index page ───────────────────────────────────────────────────────

def build_index(selected_years):
    items = ''.join(
        f'<li><a href="{y}.html">{y}</a> — {d}</li>'
        for y, (d, _) in sorted(selected_years.items())
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Dáil Debates Year Samples</title>
<style>{CSS}
li {{ margin-bottom: .3rem; }}
ul {{ padding-left: 1.2rem; list-style: disc; }}
</style>
</head>
<body>
<div class="page">
  <h1>Dáil Debates — Year Samples</h1>
  <div class="meta">One randomly selected sitting day per year, 1919–2026</div>
  <ul>{items}</ul>
</div>
</body>
</html>"""


# ── main loop ─────────────────────────────────────────────────────────────────

years_sorted = sorted(selected.keys())

with open(os.path.join(OUT_DIR, 'index.html'), 'w', encoding='utf-8') as f:
    f.write(build_index(selected))
print("  index.html")

for idx, year in enumerate(years_sorted):
    date, xml_path = selected[year]
    prev_year = years_sorted[idx - 1] if idx > 0 else None
    next_year = years_sorted[idx + 1] if idx < len(years_sorted) - 1 else None

    try:
        sections = parse_debate(xml_path)
        page = build_html(year, date, xml_path, sections, prev_year, next_year)
        out = os.path.join(OUT_DIR, f'{year}.html')
        with open(out, 'w', encoding='utf-8') as f:
            f.write(page)
        print(f"  {year}.html  ({date}, {len(sections)} sections)")
    except Exception as e:
        print(f"  {year}: ERROR — {e}")

print(f"\nDone. Files in {OUT_DIR}/")
