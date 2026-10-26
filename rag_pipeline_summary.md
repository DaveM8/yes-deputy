# RAG Data Pipeline & Table Handling Summary

## Overview
Summary of key decisions from parsing parliamentary XML → JSON → Parquet → RAG system.

---

## 1. Core Data Structure
- One record per **answer group**
- Contains:
  - metadata
  - multiple questions
  - one shared answer

✔ Avoids duplication  
✔ Matches real-world structure  

---

## 2. Parquet Strategy
- Flatten to **one row per question**
- Duplicate answer text

✔ Easier querying  
✔ Better for ML pipelines  

---

## 3. Cleaning Decisions
- Remove speaker prefixes at XML stage
- Deduplicate questions per answer
- Keep newlines (normalise only)

---

## 4. Dataset Stats
- Rows: 681,424  
- Questions: 838,816  
- Avg answer length: 1,287  
- 95th percentile: 3,429  
- Max: 87,565  

✔ Long answers are valid → keep them  

---

## 5. Embedding Layer
Purpose:
- Convert data into searchable chunks

Includes:
- chunking
- formatting
- metadata

---

## 6. Tokenisation Strategy
❌ No custom tokens for:
- ministers
- departments

✔ Use metadata instead

---

## 7. Tables (Key Insight)
- ~66k documents contain tables
- Tables are critical information sources

---

## 8. Table Types
- Time-series
- Category breakdown
- Geographic
- Hierarchical
- Event/log

---

## 9. Table Encoding

Keep:
- structured JSON

Add:
- text version for embeddings

Example:

Table title: Sheep Stock  
Columns: Year, Breeding Ewes, Rams, Other Sheep, Total  

Row: Year: 2010; Total: 3,116,232  

---

## 10. Important Rules
- Remove redundant formats
- Keep row-wise text
- Preserve € / commas / %

---

## 11. Chunking Strategy

Two chunk types:

1. answer_text  
2. table  

✔ Tables = separate chunks  

---

## 12. Metadata per Chunk
- date
- department
- answering_person
- heading
- chunk_type
- table_type (if applicable)

---

## 13. Architecture

Storage:
- JSON (canonical)
- Parquet (analysis)

Embedding:
- chunked text
- table chunks

Retrieval:
- vector search
- optional filtering

---

## Key Insight

This is not just a chatbot.

It is a:
**Policy intelligence system**

---

Generated: 2026-04-12 14:45:27
