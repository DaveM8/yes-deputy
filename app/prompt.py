from __future__ import annotations

from datetime import date

from app.models import ChatMessage, SourceCard

_SYSTEM_TEMPLATE = """\
You are an expert assistant on Irish parliamentary proceedings. You help users explore \
the official Oireachtas record: Dáil and Seanad debates, Leaders' Questions, statements, \
committee hearings, and parliamentary questions with ministerial answers.

INSTRUCTIONS:
- Answer based primarily on the SOURCE DOCUMENTS provided below.
- When citing a source, refer to it by number: "(Source 1)", "(Source 2)", etc.
- If the sources do not contain enough information to answer confidently, say so clearly.
- Be concise. Where a direct quote is useful, include it briefly.
- If asked about statistics or figures, prioritise table chunks (marked [TABLE]).
- Do not speculate about policy intentions unless directly stated in a source.
- If the question spans multiple years or departments, synthesise across sources.
- Attribute what was said to the named speaker. Debate sources are labelled "Speaker"; \
answers to parliamentary questions are labelled "Minister".
- Sources marked "ordered newest first" are the most recent matches, so the first one \
answers questions like "when did X last speak".

TODAY'S DATE: {today}

SOURCE DOCUMENTS ({n_sources} retrieved{ordering}):
{sources_block}"""

_SPEECH_TEMPLATE = """\
--- SOURCE {n} ---
Date: {date} | House: {house}{committee}
Section: {heading_path} ({section_type})
Speaker: {speaker}{speaker_role}{table_label}
Text:
{text}
"""

_SOURCE_TEMPLATE = """\
--- SOURCE {n} ---
Date: {date} | House: {house}
Topic: {heading}
Minister: {answering_person} ({answering_role})
Department: {department_role}{table_label}
Text:
{text}
"""


def _format_source(n: int, card: SourceCard) -> str:
    table_label = ""
    if card.chunk_type == "table":
        caption = card.table_caption or "table"
        table_label = f"\n[TABLE: {caption}]"
    if card.speaker:  # debate / committee speech (including tables inside speeches)
        return _SPEECH_TEMPLATE.format(
            n=n,
            date=card.date,
            house=card.house,
            committee=f" | Committee: {card.committee}" if card.committee else "",
            heading_path=card.heading_path or card.heading,
            section_type=card.section_type or "debate",
            speaker=card.speaker,
            speaker_role=f" ({card.speaker_role})" if card.speaker_role else "",
            table_label=table_label,
            text=card.text,
        )
    return _SOURCE_TEMPLATE.format(
        n=n,
        date=card.date,
        house=card.house,
        heading=card.heading,
        answering_person=card.answering_person,
        answering_role=card.answering_role,
        department_role=card.department_role,
        table_label=table_label,
        text=card.text,
    )


def build_system_prompt(cards: list[SourceCard], *, newest_first: bool = False) -> str:
    if cards:
        sources_block = "\n".join(_format_source(i + 1, c) for i, c in enumerate(cards))
    else:
        sources_block = "(No relevant source documents were found for this query.)"
    return _SYSTEM_TEMPLATE.format(
        today=date.today().isoformat(),
        n_sources=len(cards),
        ordering=", ordered newest first" if newest_first and cards else "",
        sources_block=sources_block,
    )


def build_messages(
    history: list[ChatMessage],
    new_message: str,
    *,
    max_turns: int = 6,
) -> list[dict]:
    """
    Return the messages list for the LLM call.

    Keeps the last `max_turns` complete user+assistant pairs from history,
    then appends the new user message. Pairs are always dropped together so
    the history never starts with an assistant turn.
    """
    # Work with complete pairs only
    pairs: list[tuple[ChatMessage, ChatMessage]] = []
    i = 0
    while i + 1 < len(history):
        if history[i].role == "user" and history[i + 1].role == "assistant":
            pairs.append((history[i], history[i + 1]))
            i += 2
        else:
            i += 1  # skip malformed entries

    # Keep only the last max_turns pairs
    pairs = pairs[-max_turns:]

    messages: list[dict] = []
    for user_msg, asst_msg in pairs:
        messages.append({"role": "user", "content": user_msg.content})
        messages.append({"role": "assistant", "content": asst_msg.content})
    messages.append({"role": "user", "content": new_message})
    return messages
