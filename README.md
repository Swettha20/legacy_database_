# Legacy DB Modernizer

An AI-assisted tool that migrates a legacy **Oracle** database to a modern **PostgreSQL** database — schema, data, and stored procedure logic — combining deterministic rule-based translation with AI assistance for genuinely ambiguous decisions, and validating every AI output before trusting it.

## What this is, honestly

This tool does **not** claim to migrate databases "without any fault" or produce something "impossible to get wrong." No real system can honestly claim that. Instead, it:

- Automates the confident, mechanical parts of migration with deterministic rules
- Uses an AI model only where genuine judgment is needed — ambiguous column types, and translating procedural logic. The model is swappable: a free local model (Ollama) by default, or Groq's free cloud API, chosen with one setting (see [Choosing the AI provider](#choosing-the-ai-provider))
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
- **AI provider (your choice, one setting)** — **Ollama** (`codellama:7b`, runs locally: free, private, no API key, no internet) or **Groq** (`openai/gpt-oss-120b`, free-tier cloud API: needs a free API key and internet, noticeably more reliable on the harder translation task)

## What's built and verified

- Real Oracle → real PostgreSQL schema migration, with correct type mapping including foreign key type resolution
- Real data migration in batches, with ID sequences correctly reset to avoid collisions
- AI-assisted type suggestions for ambiguous columns, validated against a fixed allowlist before being trusted
- AI-powered PL/SQL → PL/pgSQL translation of a real stored procedure (`checkout_book`), including row locking, custom exceptions, and transaction handling — validated structurally, then confirmed by actually executing the translated function against a live database
- Provider-switchable AI layer (`llm_provider.py`) with timeouts, retries, rate-limit handling and a safe fallback, so an unreachable AI service degrades one step instead of crashing the run
- A standalone confidence/flagging report
- A working REST API (FastAPI) and browser UI (Streamlit), with confirmed graceful failure handling when a database is unreachable

## Real bugs found and fixed along the way

Worth stating plainly, since debugging real issues is the actual engineering work:

1. **Foreign key type mismatch** — the type mapper initially mapped ambiguous `NUMBER` columns to `NUMERIC` even when they were foreign keys referencing `SERIAL` (effectively `INTEGER`) columns, causing Postgres to reject the foreign key constraint. Fixed with a two-pass mapping approach: resolve primary key types first, then match foreign keys to their referenced type exactly.
2. **Eager connection at import time** — `read_schema.py` originally opened its Oracle connection at the top level of the file, meaning simply *importing* it (as the FastAPI backend does) tried to connect immediately, crashing the whole API on startup if Oracle wasn't ready. Fixed by moving the connection inside the function that actually needs it.
3. **Invalid transaction control in translated PL/pgSQL** — the AI's first translation attempt included `COMMIT`/`ROLLBACK` inside the function body, which Oracle procedures allow but plain PL/pgSQL functions do not. This passed structural validation but failed at actual execution time. Fixed by updating the prompt to omit transaction control entirely (relying on Postgres's automatic rollback-on-exception behavior) and adding a validator check that catches this pattern going forward.
4. **Translations that compile but behave differently** — checked against a real PostgreSQL, not assumed: Oracle's `SYSDATE` was translated to `CURRENT_DATE`, silently storing midnight instead of the real time (fixed: `NOW()`); and Oracle's `WHEN NO_DATA_FOUND` handler never fires in Postgres for a plain `SELECT ... INTO`, so a missing book produced a foreign-key error instead of the intended message (fixed: `INTO STRICT`). Both are now enforced in the prompt *and* caught by the validator, which retries with the specific error.
5. **A hardcoded model name quietly retiring** — the cloud model first chosen was shut down by its provider, surfacing as a bare `404`. Fixed by making the model configurable, showing the provider's own error text, and adding `python llm_provider.py --models` to list what a key can currently use.

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
docker run -d --name oracle-xe --restart unless-stopped -p 1521:1521 -e ORACLE_PASSWORD=<your-password> gvenzl/oracle-xe:latest
docker pull postgres:16
docker run -d --name pg-target --restart unless-stopped -p 5432:5432 -e POSTGRES_PASSWORD=<your-password> -e POSTGRES_DB=modernized_db postgres:16
```

Copy `.env.example` to `.env` and fill in your database passwords (`.env` is gitignored — never commit it). Then pick an AI provider (below).

### Performance and measuring migration speed

Data is read from Oracle in batches of 5,000 rows and written with PostgreSQL `COPY` (falling back to a slower, driver-adapted insert for any value type it does not recognise). Each table's row count is checked against what was read before anything is committed, and a failure anywhere rolls the whole load back.

Measured on a laptop-class machine against PostgreSQL 16 with synthetic data (Postgres side only, source rows generated in memory): the previous row-by-row insert managed about 10,600 rows/s; `COPY` manages about 78,000-88,000 rows/s with a primary key, a unique index and a foreign key in place, using roughly 66 MB of memory regardless of table size (batches are streamed). That is about a million rows in 13 seconds. **Your real speed depends on Oracle's read speed, the network and the disk**, which that test cannot see. To measure it end to end on your own machine:

```powershell
python seed_oracle_volume.py 300000          # ~300k customers + ~700k orders in scratch tables
$env:ORACLE_TABLES = "VOL_CUSTOMERS,VOL_ORDERS"
python build_ir.py; python type_mapper.py; python write_postgres_schema.py
Measure-Command { python migrate_data.py }   # the number that matters
Remove-Item Env:ORACLE_TABLES
python seed_oracle_volume.py --drop          # remove the Oracle scratch tables
```

Not yet tested at hundreds of millions of rows, and the whole load runs as one transaction, so a very large migration needs enough disk for PostgreSQL's write-ahead log.

### Choosing what to migrate

By default the tool migrates every table owned by the user you connect as (`ORACLE_USER`), discovering them automatically and ordering them so parent tables are created and loaded before the tables that reference them. Two optional settings in `.env`:

```
ORACLE_SCHEMA=APP                    # migrate a different schema than the login user
ORACLE_TABLES=MEMBERS,BOOKS,LOANS    # or list the tables explicitly
```

Stated plainly: a schema with more than 200 discoverable tables is refused (set `ORACLE_TABLES` to choose), circular foreign keys between tables are refused with a clear message, and a table's unsupported column types are skipped and reported rather than failing the run. A table that references *itself* (employee → manager) is supported.

### Choosing the AI provider

Set `LLM_PROVIDER` in `.env`. If it is missing, the default is `ollama`.

**Option 1 — Ollama (local, default).** Free, private, works offline. Install Ollama, then:
```powershell
ollama pull codellama:7b
```
```
LLM_PROVIDER=ollama
```

**Option 2 — Groq (free-tier cloud API).** Create a free key at [console.groq.com](https://console.groq.com) (API Keys), then add to `.env`:
```
LLM_PROVIDER=groq
GROQ_API_KEY=your_key_here
GROQ_MODEL=openai/gpt-oss-120b
```
Check the setup with `python llm_provider.py` (prints the active provider and a test reply).

Trade-offs, stated plainly:
- **Groq sends prompts over the internet.** Here those are column names and stored-procedure text from the sample schema. Don't send confidential production code to any free cloud tier without reading its data terms.
- **Free tiers have rate limits** and can change. The code waits briefly on a rate-limit response and otherwise falls back safely, but heavy use may need a paid plan.
- **Cloud models get retired.** If calls fail with a 404 / `model_not_found`, run `python llm_provider.py --models` and set `GROQ_MODEL` to one that is listed.
- **Ollama costs nothing and keeps data local**, but a 7B model on CPU is slower and less consistent — it needed retries on the PL/SQL translation where the larger model succeeded first time.

**Note on restarts:** the `--restart unless-stopped` flag above makes Docker bring both containers back up whenever Docker itself starts (for Docker Desktop, enable *Start Docker Desktop when you sign in*). If you created the containers earlier without it, add it with `docker update --restart unless-stopped oracle-xe pg-target`. Oracle takes a minute or more to accept connections after a cold start, so `.\start_services.ps1` is still useful: it starts the containers if needed and waits until Oracle genuinely accepts a connection. A container you stopped by hand with `docker stop` stays stopped until you start it again.

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
