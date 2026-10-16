# Legacy DB Modernizer

## Start everything on a new computer

After cloning, double-click **start_all.bat** on Windows. It bootstraps Docker if needed, downloads the containerised app/databases/AI model, creates private settings and demo data, runs health checks, and opens the website. Stop this stack with **stop_all.bat**; rerun checks with **health_all.bat**. Databases and downloads are retained when stopped.

Linux/macOS with Docker: `sh start_all.sh`, `sh stop_all.sh`, `sh health_all.sh`.

See [START_HERE.md](START_HERE.md) for first-run Windows/WSL requirements, hosted AI mode, ports, troubleshooting and what is actually checked. The new stack uses **http://localhost:18501** (website) and **http://localhost:18000/docs** (API); it is independent of the older native development setup below.

The default browser workspace now uses the dark bronze-and-blue design in [frontend/index.html](frontend/index.html), with API-based uploads, progress and downloads. See [frontend/README.md](frontend/README.md) for its architecture and verified functionality.

An AI-assisted tool that migrates a legacy **Oracle** database to a modern **PostgreSQL** database — schema, data, and stored procedure logic — combining deterministic rule-based translation with AI assistance for genuinely ambiguous decisions, and validating every AI output before trusting it.

## Current upgraded Docker application

Sign in as **admin** with **STACK_ADMIN_PASSWORD** from private `.env.stack`. Follow **Inspect → Review → Approve → Migrate → Verify → Download**. Jobs and files persist across API restarts; each run uses a fresh target. The API, Redis queue and worker are separate services. Rules-only mode is the default for new installations; hosted Groq and local Ollama remain optional.

See [UPGRADE_STATUS.md](UPGRADE_STATUS.md) for architecture, compatibility and recovery, and [benchmarks/README.md](benchmarks/README.md) for the pinned 1,063,396-row public sample and actual evidence. The supplied bronze-and-blue design is preserved.

The sections below include the original standalone development tools and their history. Use the Docker entry points for the authenticated service; legacy CLI scripts do not provide the full control plane or target-isolation guarantees.

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

api.py + web_server.py + frontend/index.html — a FastAPI backend and a
buildless, API-driven browser workspace. streamlit_app.py remains an optional
legacy interface.
```

## Tech stack

- **Docker** — runs Oracle XE and PostgreSQL locally, no native installs
- **Python** — `oracledb` and `psycopg2` drivers, FastAPI; HTML/CSS/JavaScript frontend
- **AI provider (your choice, one setting)** — **Ollama** (`codellama:7b`, runs locally: free, private, no API key, no internet) or **Groq** (`openai/gpt-oss-120b`, free-tier cloud API: needs a free API key and internet, noticeably more reliable on the harder translation task)

## What's built and verified

- Real Oracle → real PostgreSQL schema migration, with correct type mapping including foreign key type resolution
- Real data migration in batches, with ID sequences correctly reset to avoid collisions
- AI-assisted type suggestions for ambiguous columns, validated against a fixed allowlist before being trusted
- AI-powered PL/SQL → PL/pgSQL translation of **every stored procedure and function in the schema**, each result checked by an Oracle-construct scan, by a real PostgreSQL parse, and by feeding any error back to the AI for another attempt; constructs that cannot be translated automatically are reported for a person, and behaviours that differ between the databases are listed as review notes (see [Stored procedures](#stored-procedures))
- Provider-switchable AI layer (`llm_provider.py`) with timeouts, retries, rate-limit handling and a safe fallback, so an unreachable AI service degrades one step instead of crashing the run
- Schema fidelity beyond tables and columns: `UNIQUE`, `CHECK` and `DEFAULT` constraints, composite and self-referencing foreign keys with `ON DELETE` rules, declared `NUMBER(p,s)` precision, and a broader set of column types (text, large objects, binary, timestamps with time zones). Whatever it cannot carry over is listed in the report instead of being dropped silently
- Automatic table discovery with dependency ordering (parents before children), so it is not tied to the sample schema
- Bulk loading with `COPY`, about 8x faster than row-by-row insert (roughly 73,000-88,000 rows/s measured against PostgreSQL), with a per-table row-count check
- Refuses to silently change data: a fraction is never rounded into a whole-number column, and a failure anywhere rolls the whole load back
- A differential checker (`compare_procedures.py`) that compares an Oracle procedure with its translation call by call
- A web page that shows the migration live - which of the five steps it is on, which table is being copied and how many rows so far, and warnings as they appear - and a button to download **the migrated PostgreSQL file** itself (a plain `.sql` script that loads anywhere); the report, and everything as one `.zip`, are under a collapsed "More downloads" section. After a failure no file is offered, because there is none to give
- When a migration stops, a message that says exactly what state things are in. The data load is one all-or-nothing transaction, so tables already read are shown as "copied - saved when every table is done" and, after a failure, as "rolled back - NOT saved"; a message claiming "tables 1 and 2 are done" would be false
- A scan for text that is already damaged in the source (the replacement character, UTF-8 read as the wrong encoding such as `Ã©` for `é`, control characters), counted per column and reported as warnings, plus a check that both databases can hold any character
- One command (`python start_demo.py`, or double-click `start_demo.bat`) that starts the containers, waits until both databases really answer, makes sure the AI works, and opens the page
- Upload an Oracle SQL file on the web page and download the converted PostgreSQL files (see [Converting an uploaded Oracle script](#converting-an-uploaded-oracle-script))
- A second sample database with a different shape (a medical clinic - see [Second sample](#second-sample-a-medical-clinic)), run end to end with one command
- A standalone confidence/flagging report
- A working REST API (FastAPI) and API-driven browser workspace, with confirmed graceful failure handling when a database is unreachable

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


11. **A progress message that would have lied** — it is tempting to show "table 1 done, table 2 done" as the load runs, but the load is a single transaction: if table 3 fails, tables 1 and 2 are rolled back too. The page shows tables as "copied - saved when every table is done" and only "saved" after the commit, and the failure message was checked against what Postgres really held afterwards.
12. **Asking the AI a question whose answer was thrown away** — a foreign-key column takes its type from the column it references, yet the AI was still asked about it first. Skipped now, which saves a slow, rate-limited call per such column.

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

Measured on a laptop-class machine against PostgreSQL 16 with synthetic data (Postgres side only, source rows generated in memory): the previous row-by-row insert managed about 10,600 rows/s; `COPY` manages about 73,000-88,000 rows/s with a primary key, a unique index and a foreign key in place (the text-damage scan described below costs about 6%), using roughly 66 MB of memory regardless of table size (batches are streamed). That is about a million rows in 14 seconds. **Your real speed depends on Oracle's read speed, the network and the disk**, which that test cannot see. To measure it end to end on your own machine:

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
3. **Feedback**: any problem found is sent back to the AI with the exact message, for up to three attempts. Every retry lists *all* earlier problems and says to avoid them together, because fixing only the latest let the AI bring an earlier one back (it went round in a circle on a real run). A mistake that is mechanical and always wrong - `RETURNS void` on a function with `OUT` parameters - is corrected by the tool itself without another AI call, and the file header says `AUTO-CORRECTED`.
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

Each call is reported the moment it finishes (`[3/13] book_appointment(...)  ->  SAME  (0.4s)`), and every call has a 30-second limit on both databases. A call that hits the limit is reported as an ERROR with its reason and the usual cause - another program or window holding a lock on the same tables - and is never counted as the procedure's own behaviour.

Each case runs on its own new Oracle connection, closed afterwards. On a real run, the tenth call stalled when it followed nine others on one shared connection, yet ran instantly alone and after just one other case - so no state is shared between cases (`--reuse-connection` brings the old behaviour back for diagnosis). To investigate one call, `python compare_procedures.py --list` numbers the cases and `--only 7` (or `7,10`, or `3-5`) runs just those; the progress lines keep the original numbers.

### Second sample: a medical clinic

The library example alone could hide a tool tuned to one schema. The clinic sample is deliberately different and uses features the library sample does not: a table that references itself, a composite primary key, a foreign key to a *text* key, a composite `UNIQUE`, `IN (...)` checks, string and `SYSTIMESTAMP` defaults, `ON DELETE CASCADE`, an identity column, a `CLOB`, `NUMBER(8,2)` prices, and patient names in several scripts (`José Müller`, `山田 花子`, `Søren Kierkegård`, an emoji in a note). Four stored procedures and functions (a cursor `FOR` loop, an `OUT` parameter, error handling, a date argument) come with comparison tests that include the error branches.

Add `CLINIC_PASSWORD=...` to `.env` (the password of a throwaway local user), then:

```powershell
python run_clinic_demo.py          # Oracle schema CLINIC -> Postgres database clinic_db, end to end
python run_clinic_demo.py --drop   # remove it again
```

It creates the sample in its own Oracle schema and its own Postgres database, so the library sample is never touched, then runs the same pipeline, translates and installs the procedures, compares each with its Oracle original, and runs the health check. Your `.env` is not changed: the two overriding settings apply only to the steps it runs.

### Converting an uploaded Oracle script

On the web page, choose **"An Oracle SQL file I upload"**, pick a `.sql` file, and click **Convert this file**. The file can contain tables, data and stored procedures; try `sample_schema.sql` or `sample_clinic_script.sql`.

The migration reads real metadata from a live Oracle, so an uploaded script cannot be read directly. Instead:

1. The script is run in a **brand-new, temporary Oracle schema** (`UPL_xxxxxxxx`) whose user can only create objects inside itself - never in `SYSTEM` or next to your other data. It is dropped again afterwards, even if something fails. Statements that fail (a `DROP` of something that does not exist, say) are listed but do not stop the script; SQL*Plus commands (`SET DEFINE OFF`, `PROMPT`, `@file`) are skipped and reported.
2. The normal pipeline reads that schema and writes a new PostgreSQL database (`upload_xxxxxxxx`).
3. You download **the migrated PostgreSQL file** (tables, data, id counters and the translated functions - load it anywhere with `psql -d yourdb -f the_file.sql`). Under "More downloads" there is also a `.zip` with `report.txt`, each translated procedure in `translated/`, and `script_load_log.txt` listing every statement that failed.

The page shows the same live progress as a normal migration, plus the stored procedures as they are translated. If the conversion stops, it says exactly what state things are in; a failed conversion leaves no empty database or Oracle schema behind.

Things to know:
- **Only upload scripts you trust.** They execute inside your local Oracle (with the limited rights above).
- **One upload at a time**, and Oracle, PostgreSQL and (for procedures) the AI provider must be running.
- It needs a SQL *script*, not a binary Oracle export (`.dmp`): import those into Oracle with Oracle's own tools first.
- Scripts that qualify names with another schema (`CREATE TABLE hr.employees`) will fail on permissions; remove the prefix. `CREATE USER`, `CONNECT` and `@other_file` are not supported.
- The script is run one statement at a time, so a script with hundreds of thousands of `INSERT`s is slow; for that much data load it into your own Oracle and use the normal migration.
- The temporary Oracle schema is removed, but each upload keeps its PostgreSQL database. List them with `docker exec pg-target psql -U postgres -c "\l"` and remove one with `DROP DATABASE upload_xxxxxxxx;`.
- The step that runs your script inside a real Oracle was tested against a stand-in; the splitting of the script into statements, the pipeline, the cleanup and the downloaded file were tested for real.

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

```powershell
python start_demo.py          # or double-click start_demo.bat
```
It starts the Oracle and Postgres containers, waits until both genuinely accept connections, checks the AI provider (starting Ollama if that is your provider), starts the API and the page, and opens `http://localhost:8501`. Click "Start Migration." Anything already running is reused; Ctrl+C stops what the script started. Server output goes to the `logs/` folder.

When it finishes, the page offers **Download the migrated PostgreSQL file (.sql)**: the tables, the data, the id counters and, if you ticked the box, the translated procedures, as one script that loads anywhere with `psql -d yourdb -f the_file.sql`. An uploaded `mydb.sql` is saved as `mydb_postgresql.sql`; a live migration as `migrated_postgresql.sql`. Under "More downloads" are the report and a `.zip` of the file, the report and `translated/`. Tick *Also translate the stored procedures with the AI* to add the procedures (slower; two more steps).

To start the two servers yourself instead, in two terminals:
```powershell
uvicorn api:app
```
```powershell
uvicorn web_server:app --host 127.0.0.1 --port 8501
```

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

**Option C — check that everything is in working order, in one command:**
```powershell
python check_setup.py
```
It reads and changes nothing, and prints a PASS / WARN / FAIL checklist with the fix next to every problem: configuration, the AI provider, both databases, that both databases can hold any character, that every table, row count, `UNIQUE` / foreign key / `CHECK` constraint and id sequence made it across, that the translated procedures are installed, and that the containers restart on their own. Use `--skip-ai` or `--skip-docker` to leave those out. On very large tables the row-count check runs `COUNT(*)` on both sides, which takes a while.

**Option D — convert single source files** (not a database): Java → Python, PHP → Node.js, MySQL schema → PostgreSQL.
```powershell
python convert_file.py samples\LegacyOrder.java
python convert_file.py legacy_cart.php old_schema.sql
python convert_file.py "samples\*.php" --out my_results
```
The file's *content* decides what it is (a PHP script saved as `.txt` is still PHP), the matching converter from `app/` does the work, and the result goes to the `converted/` folder; the original is never changed. Each converter checks the shape of its own output (valid Python; no SQL built by pasting strings together; balanced SQL statements) and starts the file with a WARNING if the check still fails after a retry. It needs only the AI provider, not Oracle, Postgres or Docker. Limits: the checks are about shape, not behaviour; the SQL converter is written for MySQL, not Oracle (use the main migration, or `plsql_translator.py --file` for Oracle procedures in a script); and the detector can mistake an Oracle DDL file or ordinary text containing the word SELECT for SQL. To add another kind of converter, write a plugin that implements `ModernizerPlugin` (`app/plugin_base.py`) and register it in `app/registry.py`.

## What's deliberately out of scope

Documented honestly rather than hidden:

- **Additional database pairs** — this tool supports Oracle → PostgreSQL specifically. The IR-based architecture is designed to support additional pairs without a rewrite, but that wasn't built, to keep this one pair genuinely solid rather than several pairs half-working.
- **Celery/Redis background job queue, a full React dashboard, true GB-scale stress testing** — deferred in favor of a focused FastAPI + buildless frontend stack that fit a focused build timeline. The batching logic used for data migration is the same pattern that would scale to larger data; it just hasn't been tested at GB scale.
- **Source database performance-parameter migration** — this tool migrates schema, data, and logic. A source database's tuning (memory allocation, connection limits, etc.) is tied to its specific hardware and workload and doesn't transfer meaningfully to different target hardware — intentionally not attempted.
- **Reported but not migrated:** ordinary indexes, views, triggers, packages and standalone sequences are listed in the report so nothing is lost silently, but they are not translated. Exotic column types (`XMLTYPE`, `INTERVAL`, spatial) are flagged for manual work, and circular foreign keys between tables are refused with a clear message.
- **Proof of logical equivalence for stored procedures** — the checks catch known classes of mistakes and the differential checker compares behaviour for the calls you list, but neither can prove a translation equivalent for every input.
- **Repairing damaged text** — the tool compares the two databases' character sets and counts text that already looks damaged (replacement characters, wrong-encoding patterns, control characters), but it cannot tell a deliberate odd value from corruption, and it does not repair anything. Worth checking explicitly in any real migration.
