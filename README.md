# Legacy DB Modernizer

An AI-assisted tool that migrates a legacy **Oracle** database to a modern **PostgreSQL** database — schema, data, and stored procedure logic — combining deterministic rule-based translation with AI assistance for genuinely ambiguous decisions, and validating every AI output before trusting it.

## What this is, honestly

This tool does **not** claim to migrate databases "without any fault" or produce something "impossible to get wrong." No real system can honestly claim that. Instead, it:

- Automates the confident, mechanical parts of migration with deterministic rules
- Uses a local AI model (Ollama) only where genuine judgment is needed — ambiguous column types, and translating procedural logic
- **Validates every AI output** before accepting it, and retries with specific feedback when validation fails
- **Transparently reports** what it's confident about versus what a human should review, via a standalone confidence report

This mirrors how real enterprise migration tools (e.g. AWS DMS) are actually built — automate what's safe to automate, flag what isn't.

## Architecture

```
Oracle (source)
     │  read_schema.py — reads real schema metadata via Oracle's system catalog
     ▼
schema_ir.json — a clean, source-agnostic Intermediate Representation
     │  type_mapper.py — rule-based type mapping; calls ai_type_advisor.py
     │                    for ambiguous columns, validated against an allowlist
     ▼
mapped_schema.json — Oracle types mapped to PostgreSQL types, with a
                      confidence level and reasoning note on every column
     │  write_postgres_schema.py — generates and runs CREATE TABLE statements
     │  migrate_data.py — copies real data across in batches, preserving
     │                     foreign key relationships and ID sequences
     ▼
PostgreSQL (target) — real schema, real data

Separately: plsql_translator.py — translates PL/SQL stored procedures to
PL/pgSQL via the AI model, validated structurally and by actually running
the translated function against a live database before trusting it.

generate_report.py — produces a standalone, human-readable confidence
report from mapped_schema.json.

api.py + streamlit_app.py — wraps the whole pipeline in a FastAPI backend
and a Streamlit UI, so it runs end-to-end from a single button click.
```

## Tech stack

- **Docker** — runs Oracle XE and PostgreSQL locally, no native installs
- **Python** — `oracledb` and `psycopg2` drivers, FastAPI, Streamlit
- **Ollama (local AI, `codellama:7b`)** — free, private, no API key required

## What's built and verified

- Real Oracle → real PostgreSQL schema migration, with correct type mapping including foreign key type resolution
- Real data migration in batches, with ID sequences correctly reset to avoid collisions
- AI-assisted type suggestions for ambiguous columns, validated against a fixed allowlist before being trusted
- AI-powered PL/SQL → PL/pgSQL translation of a real stored procedure (`checkout_book`), including row locking, custom exceptions, and transaction handling — validated structurally, then confirmed by actually executing the translated function against a live database
- A standalone confidence/flagging report
- A working REST API (FastAPI) and browser UI (Streamlit), with confirmed graceful failure handling when a database is unreachable

## Real bugs found and fixed along the way

Worth stating plainly, since debugging real issues is the actual engineering work:

1. **Foreign key type mismatch** — the type mapper initially mapped ambiguous `NUMBER` columns to `NUMERIC` even when they were foreign keys referencing `SERIAL` (effectively `INTEGER`) columns, causing Postgres to reject the foreign key constraint. Fixed with a two-pass mapping approach: resolve primary key types first, then match foreign keys to their referenced type exactly.
2. **Eager connection at import time** — `read_schema.py` originally opened its Oracle connection at the top level of the file, meaning simply *importing* it (as the FastAPI backend does) tried to connect immediately, crashing the whole API on startup if Oracle wasn't ready. Fixed by moving the connection inside the function that actually needs it.
3. **Invalid transaction control in translated PL/pgSQL** — the AI's first translation attempt included `COMMIT`/`ROLLBACK` inside the function body, which Oracle procedures allow but plain PL/pgSQL functions do not. This passed structural validation but failed at actual execution time. Fixed by updating the prompt to omit transaction control entirely (relying on Postgres's automatic rollback-on-exception behavior) and adding a validator check that catches this pattern going forward.

## Getting set up

```powershell
git clone <repo-url>
cd legacy-db-modernizer
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Docker must be installed and running:

```powershell
docker pull gvenzl/oracle-xe:latest
docker run -d --name oracle-xe -p 1521:1521 -e ORACLE_PASSWORD=<your-password> gvenzl/oracle-xe:latest
docker pull postgres:16
docker run -d --name pg-target -p 5432:5432 -e POSTGRES_PASSWORD=<your-password> -e POSTGRES_DB=modernized_db postgres:16
```

Ollama must also be installed, with the model pulled:
```powershell
ollama pull codellama:7b
```

**Note:** on restart, Docker containers do not start automatically — run `docker start oracle-xe pg-target` each time before working with this project.

Load the sample schema (`sample_schema.sql`) against the Oracle container, using SYSTEM or a SQL client of your choice.

## Running it

**Option A — via the UI (recommended):**

Two terminals:
```powershell
uvicorn api:app --reload
```
```powershell
streamlit run streamlit_app.py
```
Then open `http://localhost:8501` and click "Start Migration."

**Option B — step by step, via the individual scripts:**
```powershell
python read_schema.py       # confirm Oracle schema reads correctly
python build_ir.py          # build the IR
python type_mapper.py       # map types (calls the AI advisor for ambiguous cases)
python write_postgres_schema.py   # create the Postgres schema
python migrate_data.py      # migrate the data
python generate_report.py   # produce the confidence report
python plsql_translator.py  # translate the stored procedure
```

## What's deliberately out of scope

Documented honestly rather than hidden:

- **Additional database pairs** — this tool supports Oracle → PostgreSQL specifically. The IR-based architecture is designed to support additional pairs without a rewrite, but that wasn't built, to keep this one pair genuinely solid rather than several pairs half-working.
- **Celery/Redis background job queue, a full React dashboard, true GB-scale stress testing** — deferred in favor of a simpler FastAPI + Streamlit stack that fit a focused build timeline. The batching logic used for data migration is the same pattern that would scale to larger data; it just hasn't been tested at GB scale.
- **Source database performance-parameter migration** — this tool migrates schema, data, and logic. A source database's tuning (memory allocation, connection limits, etc.) is tied to its specific hardware and workload and doesn't transfer meaningfully to different target hardware — intentionally not attempted.
- **Character set / encoding validation** — a known, common real-world migration risk (source/target character set mismatches can silently corrupt data) that this project's sample data didn't exercise. Worth checking explicitly in any real migration.
