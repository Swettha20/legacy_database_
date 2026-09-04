---
name: legacy-db-modernizer
description: AI-assisted legacy database migration tool (Oracle -> PostgreSQL). Two learners working on this together - primary builder owns the pipeline (Docker, extractor, IR, mapper, loader, FastAPI, Streamlit UI), friend owns the AI-training track (schema/type suggestion prompts, PL/SQL -> PL/pgSQL translation prompts, validate-and-retry design). Read this file before making changes so you don't re-derive the architecture or duplicate work already done.
---

# legacy-db-modernizer — Project Status & Handoff

## What this project is

An AI-assisted tool that migrates a legacy Oracle database to a modern PostgreSQL database — schema, data, and stored procedures — with AI helping only on the genuinely ambiguous parts, not doing everything blindly.

**Important framing — say it this way, not otherwise:** this tool does NOT claim to migrate "without any fault" or produce output that "can't be compromised." No real AI system can honestly claim that. Instead, it:
- Automates the confident, mechanical parts of migration
- Applies security-conscious defaults by design (parameterized queries always, least-privilege permission mapping)
- **Transparently flags** anything it isn't confident about for human review

This is the same honest engineering story real enterprise migration tools use (e.g. AWS DMS), and it's the correct way to describe this project in a resume/interview.

---

## Architecture (locked — don't redesign without discussing first)

```
Legacy DB (Oracle)
      │  [Source Adapter — reads schema + data, in batches, not all at once]
      ▼
IR (Intermediate Representation — a clean JSON describing tables, columns,
    types, relationships, and stored procedures)
      │
      ├──▶ [Schema Mapper] — rule-based Oracle→Postgres type mapping first;
      │                      AI only steps in for ambiguous naming/type decisions
      │
      ├──▶ [PL/SQL → PL/pgSQL Translator] — AI-powered, validate+retry pattern
      │
      ▼
Target Adapter — writes the mapped schema + data into PostgreSQL, in batches
      │
      ▼
Confidence/Flagging Report:
   ✅ High confidence (auto-migrated)
   ⚠️  Needs review (migrated, but flagged)
   ❌ Manual action needed (couldn't be safely auto-migrated)
```

**Why this shape:**
- IR layer means the pipeline isn't hard-wired to Oracle/Postgres specifically — a second DB pair could plug in later without rewriting the core. We are NOT building a second pair now; Oracle→Postgres must fully work first.
- Rule-based mapping first, AI second — type mapping is mostly deterministic (Oracle `NUMBER` → Postgres `NUMERIC`, etc.); AI is reserved for genuinely ambiguous cases. Same principle as the earlier `ai-code-migrator` project (rule-based detector, AI-based conversion).
- Batches, not load-the-whole-thing — keeps memory flat regardless of database size.
- Confidence/flagging report — the actual, honest answer to "make sure it's correct and secure": be transparent about what the tool is sure of vs. what needs human eyes.

## Tech stack (kept simple to fit a 2-week timeline)

- **Docker** — runs Oracle XE and (soon) PostgreSQL locally
- **Python** — extractor / mapper / loader logic (`oracledb` driver for source)
- **FastAPI** — lightweight backend (upload, run, status endpoints) — not built yet
- **Streamlit** — UI, same tool used in the previous project — not built yet
- **Ollama (local AI model)** — same free, local, no-API-key approach as the previous project; used only for ambiguous schema/type decisions and PL/SQL translation

**Deliberately deferred unless time remains:** Celery/Redis job queue, a full React dashboard, true GB-scale stress testing, additional database pairs. Document these as "designed for, not yet built" in the final writeup — don't build them under time pressure just to check a box.

---

## Two work tracks

### Track A — Pipeline (primary builder)
Owns: Docker setup, Oracle source adapter, IR format, rule-based type mapper, Postgres target loader, FastAPI backend, Streamlit UI, batching/chunking logic.

### Track B — AI Training (friend)
Owns: prompt design for —
1. **Ambiguous schema/type decisions** — given a table's column names, suggest a cleaner name and confirm/correct the type mapping when the rules engine isn't confident
2. **PL/SQL → PL/pgSQL translation** — converting Oracle stored procedures/triggers into PostgreSQL equivalents

Both AI tasks must follow the **validate-and-retry pattern** from the earlier plugin project: never trust raw AI output blindly — check it structurally (does the SQL parse? does the suggested type exist in Postgres? is the rename plausible?), and if it fails, retry with the specific error fed back to the model. This is also where the confidence/flagging report gets its labels from.

**Track B can start now, in parallel, without waiting for Track A's Docker/Oracle setup** — prompt drafting and testing against sample schema snippets doesn't require a live Oracle connection. Use the sample schema (see "Current status" below, once created) as realistic test input for prompts.

---

## 14-Day plan

| Day | Phase | What gets built |
|---|---|---|
| 1 | Phase 0 | Oracle XE running in Docker + tiny sample schema (2-3 tables, 1 stored procedure) |
| 2 | Phase 1 (simple) | Python script connects to Oracle, reads schema metadata |
| 3 | Phase 2 (simple) | Convert schema into a clean IR (JSON): tables, columns, types, relations |
| 4-5 | Phase 3 (simple) | Rule-based Oracle→Postgres type mapping table — no AI yet |
| 6 | Phase 5 (simple) | Write mapped schema into real local PostgreSQL — first full schema-only proof |
| 7-8 | Phase 2 (upgrade) | Add real data migration in batches |
| 9 | Phase 1 (upgrade) | Wrap pipeline in a simple FastAPI backend |
| 10 | Phase 7 (simple) | Streamlit UI: upload, run, status |
| 11-12 | Phase 8 | AI: ambiguous schema/type suggestions + PL/SQL→PL/pgSQL translation, validate+retry, confidence/flagging report |
| 13 | Buffer | Fix what broke, polish error handling and report format |
| 14 | Wrap-up | README, demo script, honest notes on stretch goals not built |

**Rule for every day:** end with something that actually runs. If something breaks, share the actual error output and the specific file — don't guess from memory of what code "probably" does.

---

## Current status (updated as we go — check this section first)

- ✅ **Day 1, partial:** Docker confirmed installed and running. Oracle XE pulled (`gvenzl/oracle-xe:latest`) and running in a container named `oracle-xe`, port 1521 mapped to localhost. Confirmed "DATABASE IS READY TO USE."
- ✅ Separate project folder `legacy-db-modernizer/` with its own venv (isolated from the `ai-code-migrator` project's environment).
- ✅ `oracledb` Python driver installed.
- ✅ `test_connection.py` confirms a real Python → Oracle connection works (`SELECT 'Hello from Oracle' FROM dual` succeeded).
- ⬜ **Day 1, remaining:** sample schema (2-3 tables + 1 stored procedure) not yet created inside the Oracle database. Domain choice (e-commerce-style vs. a different domain, for portfolio variety from the last project) — pending decision.
- ⬜ Day 2 onward: not started.

## How to get set up (for the friend)

```powershell
git clone <repo-url>
cd legacy-db-modernizer
python -m venv venv
venv\Scripts\activate
pip install oracledb
```

Docker must also be installed and running. To start the same Oracle container locally:
```powershell
docker pull gvenzl/oracle-xe:latest
docker run -d --name oracle-xe -p 1521:1521 -e ORACLE_PASSWORD=<choose-a-password> gvenzl/oracle-xe:latest
docker logs -f oracle-xe
```
Wait for `DATABASE IS READY TO USE!` in the logs, then Ctrl+C to stop watching (container keeps running).

Verify the connection:
```powershell
python test_connection.py
```
(Update the password inside `test_connection.py` to match whatever you used in `docker run`.)

## Debugging protocol (both tracks)

1. Share the actual error output and/or the specific file — don't just describe the symptom from memory.
2. Read the real file/error before proposing a fix.
3. Explain the fix in plain words: what broke, why, what changed.
4. Hand back the corrected file ready to drop in.
5. Re-run whatever test/command confirms the fix, and share that output too.
