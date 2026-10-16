# Legacy DB Modernizer

Migrates a legacy **Oracle** database to **PostgreSQL** - schema, data and, optionally, stored procedures - and proves the result: every job writes into a fresh database, is checked value by value against the source, and its exported SQL is restored into a second empty database and checked again.

## What this is, honestly

It does not claim to migrate anything "without fault". It automates what is safe to automate with deterministic rules, uses an AI model only for optional procedure translation, checks every result, and reports plainly what it did not carry over. Anything it cannot migrate safely is blocked or listed for a person, never silently dropped.

## Start

Requires Docker (Docker Desktop on Windows, Linux containers). Roughly 16 GB RAM and 20 GB free disk are a practical starting budget.

```powershell
.\start_all.bat      # build/download, create private settings, seed demo data, start, health-check, open the website
.\health_all.bat     # check the running stack
.\stop_all.bat       # stop it (data and settings are kept)
```

Linux/macOS: `sh start_all.sh`, `sh health_all.sh`, `sh stop_all.sh`.

- Website: http://localhost:18501
- API and its documentation: http://localhost:18000/docs

Sign in as **admin** with `STACK_ADMIN_PASSWORD` from the generated private `.env.stack` (never commit it). First-run details, AI options and troubleshooting: [START_HERE.md](START_HERE.md).

## How a migration works

**Upload a trusted Oracle script, or choose a configured source → Inspect → Review the mappings → Approve → Migrate → Verify → Download.**

1. **Inspect** inventories every table, column, constraint, index, view, routine, package and trigger, profiles numeric columns, and blocks anything unsafe (unsupported types, identifiers PostgreSQL cannot hold, timestamps finer than microseconds) before any target is created.
2. **Review** shows each proposed mapping and every omission; overrides are limited to a safe allowlist.
3. **Migrate** creates a new PostgreSQL database for this job. Previous targets are kept. A configured source is read at a single Oracle point in time (SCN); an uploaded script runs in a locked, read-only scratch schema. Unexpected SQL or compile errors in an upload stop the job.
4. **Verify** compares exact row hashes (as a multiset, so duplicates and tables without primary keys are handled), counts, NULLs and numeric aggregates, and checks column types, nullability, defaults, keys, indexes and simple views against independently built reference definitions.
5. **Export** writes the PostgreSQL SQL, restores it into a second empty database and verifies again.
6. **Download** the untouched original, converted SQL, reports (text and JSON), verification, logs, checksums, manifest, or everything as a ZIP.

Jobs, history and downloads survive API restarts. A crashed worker leaves an interrupted job that can be retried into a new target.

## Architecture

```
browser (frontend/index.html, served by web_server.py)
   │  HTTP only - the website never connects to a database
   ▼
API (api.py → moderniser/service.py) ── job metadata ──▶ PostgreSQL (metadata)
   │  queue
   ▼
Redis ─▶ worker (moderniser/worker.py, engine.py, verify.py)
            │ Oracle driver                │ PostgreSQL driver
            ▼                              ▼
     Oracle (source / scratch)      PostgreSQL (one new database per job)
```

| Part | Files |
|---|---|
| Control plane: auth, uploads, plans, jobs, events, artifacts | `moderniser/service.py`, `store.py`, `queue.py`, `provision.py`, `objects.py`, `backup.py` |
| Migration and verification | `moderniser/worker.py`, `engine.py`, `verify.py` |
| Migration pipeline (schema read → mapping → load) | `read_schema.py`, `build_ir.py`, `type_mapper.py`, `constraints.py`, `write_postgres_schema.py`, `migrate_data.py`, `upload_pipeline.py`, `oracle_script.py`, `generate_report.py` |
| Optional AI procedure translation and checking | `llm_provider.py`, `ai_type_advisor.py`, `plsql_translator.py`, `plsql_rules.py`, `compare_procedures.py`, `procedure_tests.json` |
| Stack setup and health | `compose.yaml`, `Dockerfile`, `runtime_stack.py`, `check_setup.py`, `config.py`, `start_all.*`, `health_all.*`, `stop_all.*`, `scripts/stack-common.*` |
| Website | `frontend/index.html`, `web_server.py`, `legacy_api.py`, `progress_view.py` |
| Demo data | `sample_schema.sql` (seeded into the DEMO schema), `generated_data/` (207,120-row store sample - generate it first) |

## Supported scope

| Object / behaviour | Treatment |
|---|---|
| Ordinary tables with exact numbers, text, dates, binary | Automated and value-checked |
| Bare `NUMBER` | Exact unconstrained `NUMERIC` - no AI guess |
| Primary, foreign, unique keys; simple CHECK constraints and defaults | Automated where supported; omissions reported |
| Ordinary B-tree indexes (including unique) and single-table projection views | Automated |
| Join/expression views, bitmap/function indexes, packages, triggers, materialized views, partitions | Manual - inventoried and reported |
| Stored procedures and functions | Optional AI translation; supplied test calls checked separately |
| Online cutover / change data capture, other database pairs | Out of scope |

Full detail, API behaviour and recovery: [UPGRADE_STATUS.md](UPGRADE_STATUS.md).

## AI is optional

New installations use `STACK_AI_PROVIDER=none`: schema and data migration need no model or key. Set `groq` (hosted, needs `STACK_GROQ_API_KEY`) or `ollama` (local) in `.env.stack` and rerun `start_all` to enable procedure translation. Every translation is scanned for leftover Oracle constructs, compiled in a real PostgreSQL inside a rolled-back transaction, and retried with the exact error; untranslatable constructs are marked for manual work, not guessed.

## Tests and evidence

```powershell
python -m unittest discover -s tests            # focused unit tests
node scripts/check-frontend.mjs                  # frontend scripts parse
.\health_all.bat                                 # running-stack health
docker compose --env-file .env.stack --profile tools run --rm --no-deps bootstrap python scripts/integration_checks.py --internal
.\benchmarks\setup.ps1; python benchmarks\run.py --runs 3    # Oracle Sales History benchmark (1,063,396 rows)
.\scripts\backup.ps1 -RestoreTest                # metadata/artifact backup and isolated restore (while idle)
```

The integration checks run 20 real HTTP acceptance tests: login required, original bytes preserved, exact decimals/Unicode/binary/NULLs/duplicates survive, export restored and re-verified, owner isolation, failed source statements stop the job, and a failed job keeps the previous result. CI (`.github/workflows/validate.yml`) runs the unit tests and frontend check on every push. Recorded results are in [STARTUP_VALIDATION.md](STARTUP_VALIDATION.md) and `benchmarks/results/`; they describe the recorded local machine, not a universal throughput guarantee.

## Limits

- **Trusted inputs only.** An uploaded script executes in a restricted Oracle schema; that is not a hardened sandbox for anonymous internet uploads.
- **Local Docker deployment.** HTTP binds to loopback; no TLS/remote deployment is provided.
- **Quiesced sources.** Configured sources are read at one point in time; this is not change data capture or a zero-downtime cutover.
- **Upload size** is capped at 25 MiB per script; larger databases should be migrated from a configured source.
- **No row-level resume.** A failed or interrupted job is retried from the start into a new target.
