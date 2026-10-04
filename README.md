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
     │  migrate_data.py — streams real data across in batches with COPY, in one
     │                     all-or-nothing transaction, preserving foreign key
     │                     relationships and ID sequences
     ▼
PostgreSQL (target) — real schema, real data

Separately: plsql_translator.py — translates every PL/SQL stored procedure
and function to PL/pgSQL via the AI model. Each result is scanned for Oracle
constructs that cannot remain (plsql_rules.py), parsed by a real PostgreSQL
(in a transaction that is rolled back), and sent back to the AI with the exact
error if either check fails. compare_procedures.py then runs the same calls
against the Oracle original and the translation and compares results, errors,
printed output and side effects.

llm_provider.py — the one place that talks to an AI model (Ollama or Groq).
constraints.py — translates UNIQUE / CHECK / DEFAULT and foreign-key shapes.

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
- AI-powered PL/SQL → PL/pgSQL translation of **every stored procedure and function in the schema**, each result checked by an Oracle-construct scan, by a real PostgreSQL parse, and by feeding any error back to the AI for another attempt; constructs that cannot be translated automatically are reported for a person, and behaviours that differ between the databases are listed as review notes (see [Stored procedures](#stored-procedures))
- Provider-switchable AI layer (`llm_provider.py`) with timeouts, retries, rate-limit handling and a safe fallback, so an unreachable AI service degrades one step instead of crashing the run
- Schema fidelity beyond tables and columns: `UNIQUE`, `CHECK` and `DEFAULT` constraints, composite and self-referencing foreign keys with `ON DELETE` rules, declared `NUMBER(p,s)` precision, and a broader set of column types (text, large objects, binary, timestamps with time zones). Whatever it cannot carry over is listed in the report instead of being dropped silently
- Automatic table discovery with dependency ordering (parents before children), so it is not tied to the sample schema
- Bulk loading with `COPY`, about 8x faster than row-by-row insert (roughly 78,000-88,000 rows/s measured against PostgreSQL), with a per-table row-count check
- Refuses to silently change data: a fraction is never rounded into a whole-number column, and a failure anywhere rolls the whole load back
- A differential checker (`compare_procedures.py`) that compares an Oracle procedure with its translation call by call
- A standalone confidence/flagging report
- A working REST API (FastAPI) and browser UI (Streamlit), with confirmed graceful failure handling when a database is unreachable

**Verification status, stated plainly.** The three-table sample schema, the AI type advisor, and eight stored procedures were run end to end against a real Oracle XE container, a real PostgreSQL and a real AI provider. Larger and messier schemas, dirty data, volume, and failure cases (a database or the AI going down mid-run) were tested against a real PostgreSQL with a simulated Oracle source. The scale tested is about a million rows; nothing near gigabytes has been tried.

## Real bugs found and fixed along the way

Worth stating plainly, since debugging real issues is the actual engineering work:

1. **Foreign key type mismatch** — the type mapper initially mapped ambiguous `NUMBER` columns to `NUMERIC` even when they were foreign keys referencing `SERIAL` (effectively `INTEGER`) columns, causing Postgres to reject the foreign key constraint. Fixed with a two-pass mapping approach: resolve primary key types first, then match foreign keys to their referenced type exactly.
2. **Eager connection at import time** — `read_schema.py` originally opened its Oracle connection at the top level of the file, meaning simply *importing* it (as the FastAPI backend does) tried to connect immediately, crashing the whole API on startup if Oracle wasn't ready. Fixed by moving the connection inside the function that actually needs it.
3. **Invalid transaction control in translated PL/pgSQL** — the AI's first translation attempt included `COMMIT`/`ROLLBACK` inside the function body, which Oracle procedures allow but plain PL/pgSQL functions do not. This passed structural validation but failed at actual execution time. Fixed by updating the prompt to omit transaction control entirely (relying on Postgres's automatic rollback-on-exception behavior) and adding a validator check that catches this pattern going forward.
4. **Translations that compile but behave differently** — checked against a real PostgreSQL, not assumed: Oracle's `SYSDATE` was translated to `CURRENT_DATE`, silently storing midnight instead of the real time (fixed: `NOW()`); and Oracle's `WHEN NO_DATA_FOUND` handler never fires in Postgres for a plain `SELECT ... INTO`, so a missing book produced a foreign-key error instead of the intended message (fixed: `INTO STRICT`). Both are now enforced in the prompt *and* caught by the validator, which retries with the specific error.
5. **A hardcoded model name quietly retiring** — the cloud model first chosen was shut down by its provider, surfacing as a bare `404`. Fixed by making the model configurable, showing the provider's own error text, and adding `python llm_provider.py --models` to list what a key can currently use.
6. **Constraints silently dropped** — only primary keys, foreign keys and `NOT NULL` were migrated, so the new database accepted duplicate emails and negative stock counts that Oracle rejected. Found by pushing dirty data through the pipeline; `UNIQUE`, `CHECK` and `DEFAULT` are now migrated, and anything untranslatable is flagged in the report.
7. **Silent rounding** — Postgres stores `2.5` in an `INTEGER` column as `3` with no error, and the AI had chosen that type from the column name alone. The loader now refuses a fraction in a whole-number column and says exactly where it is.
8. **A failed rollback hiding the real error** — when the database died mid-load, the error handler's own `rollback()` failed and replaced the real cause with `connection already closed`.
9. **`SELECT ... INTO` is not the same in Postgres** — Oracle raises an error for zero or several rows; a plain Postgres `SELECT INTO` silently returns NULL or the first row, so an Oracle `NO_DATA_FOUND` handler never ran. Verified against a real Postgres, enforced in the prompt and the validator.
10. **A stale copy blocking a good translation** — `CREATE OR REPLACE FUNCTION` refuses to change a return type, so an older installed version caused the validator to reject correct code. Found on the first real run; installs now replace by name inside one transaction.


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

### Stored procedures

```powershell
python plsql_translator.py                        # every procedure and function in the Oracle schema
python plsql_translator.py --name CHECKOUT_BOOK   # just one
python plsql_translator.py --file my_script.sql   # from a script instead of Oracle
python plsql_translator.py --install              # also create the clean ones in Postgres
```

`--install` **replaces** any function of the same name already in Postgres (an Oracle procedure name is unique in its schema, so there is one Postgres function per name). Dropping the old one and creating the new one happen in a single transaction, so if the new one fails to create, the old one stays; if a view or other object depends on the old one, nothing is changed and you are told why.

Each result is written to `translated_procedures/<name>.sql` with a header listing its status and any review notes. To try it on varied code first, `python load_sample_procedures.py` creates seven sample procedures in Oracle (loops, cursors, OUT parameters, dynamic SQL, error handling, and some constructs that cannot be translated).

How a result is trusted, in order:
1. **Oracle-construct scan** (`plsql_rules.py`): leftover `NVL`, `DECODE`, `SYSDATE`, `DUAL`, `ROWNUM`, `DBMS_OUTPUT`, `EXECUTE IMMEDIATE`, `SQL%ROWCOUNT`, Oracle exception names, `CURSOR c IS` and more. Several of these are *accepted* by Postgres when the function is created and only fail when it is called, so a parser alone is not enough. Every replacement the rules recommend was verified on a real PostgreSQL.
2. **A real PostgreSQL parse**: `CREATE FUNCTION` runs inside a transaction that is rolled back, which catches syntax errors, unknown exception names, wrong `END` labels and bad types.
3. **Feedback**: any problem found is sent back to the AI with the exact message, for up to three attempts.
4. Every `SELECT ... INTO` must be `INTO STRICT`: Oracle raises an error for zero or several rows, while plain Postgres silently returns NULL or the first row.

What it deliberately does **not** do:
- **Constructs with no automatic translation** (`PRAGMA`, `BULK COLLECT`/`FORALL`, collection and record types, `CONNECT BY`, pipelined functions, `UTL_*`/`DBMS_*` packages) are not guessed. The AI is told to leave a `-- MANUAL:` comment, the file header lists what is missing, and `--install` skips them.
- **Behaviour differences that no automatic check can see** are listed as review notes instead: Oracle treats `NULL` as an empty string in `||` and `''` as `NULL`; integer division truncates in Postgres; `substr(x, 0, n)` is one character short; `date + number` (days) is an error in Postgres. A translation can pass every check above and still be wrong at run time for these, so **call each translated function with realistic data before relying on it** - or let `compare_procedures.py` do it against the Oracle original.
- **Triggers and packages** are listed in the report but not translated. Procedures that call other procedures are translated independently, so install them together.
- **The quality of the AI's translation of your own code is not proven by this tool.** The checks catch the mistakes above; they cannot prove the logic is equivalent.

### Checking that a translation behaves like the original

Parsing proves a translation is valid Postgres; it cannot prove it does the same thing. `compare_procedures.py` makes the same call against the Oracle original and the Postgres translation, on the same data, and compares everything observable:

```powershell
python compare_procedures.py                     # uses procedure_tests.json
python compare_procedures.py --tests my_tests.json
python compare_procedures.py --allow-commit      # also run originals that COMMIT (see below)
```

It compares the return value or `OUT` parameters, the error (Oracle's `RAISE_APPLICATION_ERROR` text against the translated `RAISE EXCEPTION` text, and Oracle's standard errors against the matching Postgres error class), what was printed (`DBMS_OUTPUT` against `RAISE NOTICE`), and side effects (the `SELECT`s you list under `observe`, run in both databases after the call). Each call is rolled back on both sides. `procedure_tests.json` has calls for the sample procedures, including the error branches; the file format is described at the top of `compare_procedures.py`.

Tested against injected translation bugs: a wrong error message, an off-by-one day, a missing `STRICT` (an unknown row silently returning NULL), a forgotten `WHERE`, and `SYSDATE` translated to `CURRENT_DATE` (losing the time of day) were all reported as different; the corrected versions were reported as identical.

What it does not do, plainly:
- **It proves equivalence only for the calls you list.** Cover the edge cases: no rows, bad input, every error branch.
- **An Oracle original that `COMMIT`s is skipped by default**, because a commit cannot be rolled back and would change your Oracle data. `--allow-commit` runs it for real; re-migrate afterwards to put the data back in step.
- **Generated ids can differ.** Sequences are not rolled back in either database, so a new row's id may not match; `observe` columns that are not generated ids.
- **Date/times are compared within a tolerance** (5 seconds by default), since `SYSDATE` and `NOW()` are never the same instant.
- **The two databases must hold the same data.** List your tables under `"tables"` and it warns when the row counts differ.
- The Postgres side of this tool was tested against a real PostgreSQL. The Oracle side was tested against a stand-in that speaks the driver's API, so its first run on your Oracle is the real test of that half.

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
uvicorn api:app
```
```powershell
streamlit run streamlit_app.py
```
Then open `http://localhost:8501` and click "Start Migration."

Note: use `uvicorn api:app --reload` only while developing. With `--reload`, saving any file restarts the server and kills a migration that is running.

**Option B — step by step, via the individual scripts:**
```powershell
python read_schema.py       # confirm Oracle schema reads correctly
python build_ir.py          # build the IR
python type_mapper.py       # map types (calls the AI advisor for ambiguous cases)
python write_postgres_schema.py   # create the Postgres schema
python migrate_data.py      # migrate the data
python generate_report.py   # produce the confidence report
python plsql_translator.py  # translate all stored procedures/functions (see "Stored procedures")
python plsql_translator.py --install   # create the clean ones in Postgres
python compare_procedures.py   # compare each one with its Oracle original
```

## What's deliberately out of scope

Documented honestly rather than hidden:

- **Additional database pairs** — this tool supports Oracle → PostgreSQL specifically. The IR-based architecture is designed to support additional pairs without a rewrite, but that wasn't built, to keep this one pair genuinely solid rather than several pairs half-working.
- **Celery/Redis background job queue, a full React dashboard, true GB-scale stress testing** — deferred in favor of a simpler FastAPI + Streamlit stack that fit a focused build timeline. The batching logic used for data migration is the same pattern that would scale to larger data; it just hasn't been tested at GB scale.
- **Source database performance-parameter migration** — this tool migrates schema, data, and logic. A source database's tuning (memory allocation, connection limits, etc.) is tied to its specific hardware and workload and doesn't transfer meaningfully to different target hardware — intentionally not attempted.
- **Reported but not migrated:** ordinary indexes, views, triggers, packages and standalone sequences are listed in the report so nothing is lost silently, but they are not translated. Exotic column types (`XMLTYPE`, `INTERVAL`, spatial) are flagged for manual work, and circular foreign keys between tables are refused with a clear message.
- **Proof of logical equivalence for stored procedures** — the checks catch known classes of mistakes and the differential checker compares behaviour for the calls you list, but neither can prove a translation equivalent for every input.
- **Character set / encoding validation** — a known, common real-world migration risk (source/target character set mismatches can silently corrupt data) that this project's sample data didn't exercise. Worth checking explicitly in any real migration.
