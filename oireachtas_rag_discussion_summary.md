# Oireachtas Search Tool — Architecture Discussion Summary

## Context

Discussion of RAG pipeline architecture for the Oireachtas parliamentary data project, evolving from advanced LLM retrieval ideas toward a cleaner, human-first product vision.

---

## RAG Ideas Explored

### Query Rewriting / Two-Pass LLM
- Pass user question to LLM first to extract structured metadata (date ranges, member names, bill types, question types) and rewrite the query for better semantic search
- Well-established pattern — known as **query rewriting** or **query transformation**
- Reduces hallucinations by improving retrieval precision before synthesis
- Well suited to Oireachtas data given rich structured metadata already available

### Question-Space / Answer-Space Split Retrieval
- Exploit the natural Q&A structure of parliamentary debates
- User questions likely embed more similarly to how TDs phrase parliamentary questions than to ministerial answers
- Proposed pipeline:
  1. Search questions index with user query → retrieve matching Q&A pairs
  2. Pass to LLM → synthesise intermediate answer
  3. Use intermediate answer to search answers index (answer vocabulary matches answer embeddings better)
  4. Pass full combined retrieval set + original question to LLM for final answer
- This is a form of **iterative retrieval / retrieval chaining**
- Risk: error propagation if step 2 misfires — mitigated by eval set

### Deferral / Uninformative Answer Problem
- Parliamentary answers frequently defer ("I will revert to the Deputy", "the matter is under consideration")
- Retrieving well-matched questions whose answers are uninformative is a key failure mode
- Mitigation options:
  - **Pre-filter at index time** — classify and exclude deferrals at ingest (cheap, keyword/regex-based)
  - **Score at retrieval time** — answer length + absence of deferral phrases
  - **LLM filter at synthesis** — instruct synthesis LLM to discard uninformative answers
  - Recommended: Option 1 as baseline + Option 3 as safety net

---

## Final Product Decision — The Human-First Research Tool

> **This is the primary product. LLM/RAG is a future add-on, not the core.**

### Vision
Build a clean, frictionless **parliamentary research tool** that allows users to search Oireachtas data via keyword and semantic search, read results in context, and compose and save their own research answers. Think specialist search engine (closer to Westlaw than ChatGPT) — the user brings their expertise, the tool provides precise retrieval.

### Why This Ordering Is Correct
- The search interface is genuinely useful standalone — for journalists, researchers, lobbyists, academics
- No dependency on RAG quality to deliver value from day one
- Every human answer saved accumulates passively as a high-quality eval/training dataset
- When LLM answers are added later, they can be immediately benchmarked against the human answer baseline

### Core Features of the Search Interface
- **Keyword + semantic (hybrid) search** over Oireachtas debates
- **Faceted filtering** — house, date range, member, party, question type (oral/written/topical)
- **Result display with metadata** — member name, date, house, question type
- **Deferral-aware ranking** — surface substantive answers, suppress procedural deferrals
- **Inline source display** — sources visible alongside the user's writing area, no context switching
- **Research/answer workspace** — text area for user to compose their own answer using retrieved results as reference
- **Auto-save** — all composed answers saved to database
- **Keyboard-first UX** — tab navigation, minimal friction

### The Annotation Angle
The saved human answers are not just user output — they form a **golden evaluation dataset** for future RAG testing. This means:
- RAG architecture experiments (including the two-pass pipeline above) can be benchmarked against real human answers from day one of LLM integration
- The product generates its own eval infrastructure passively

### LLM Integration — Future Phase
- Add as optional enhancement once the human research tool is solid
- Display LLM answer alongside human research workspace, not instead of it
- Immediately testable against accumulated human answer pairs
- Two-pass iterative retrieval pipeline (described above) is the candidate architecture to evaluate

---

## Next Steps
1. Spec and build the search + research interface
2. Confirm current state of Oireachtas data in Elasticsearch (what fields, what indices)
3. Implement hybrid search (keyword + semantic) with metadata filters
4. Polish UX for frictionless human annotation/research
5. Begin accumulating human answer pairs
6. *Later:* layer in LLM answer generation and benchmark against human baseline
