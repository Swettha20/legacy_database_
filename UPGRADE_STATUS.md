# Database Moderniser — current upgrade

The default Docker application uses an authenticated API, PostgreSQL job storage, a Redis queue and a separate migration worker. The bronze-and-blue browser design follows the supplied reference. React is not required: the existing single-file frontend keeps this checkout easy to run.

## Simple flow

Run `start_all.bat` → sign in → upload or choose a configured source → inspect → review mappings → approve → copy into a new target → verify every supported value → restore the downloaded SQL and verify again → download.

Sign in as **admin**, using **STACK_ADMIN_PASSWORD** from the generated private `.env.stack`. Passwords are never embedded in the website or saved in browser storage. API clients can use the login endpoint's bearer token. Browser sessions use an HttpOnly, SameSite cookie.

The website only calls HTTP APIs. Workers use Oracle and PostgreSQL drivers internally. Rules-only migration needs no AI service or key. Choose `STACK_AI_PROVIDER=groq` for optional hosted AI translation, `ollama` for optional local inference, or `none` to disable AI. Existing settings are preserved on upgrade.

## Implemented scope

- Durable owner-scoped uploads, jobs, plans, mappings, events, artifacts and audit records.
- Separate Celery worker; restarting the API does not terminate migration work.
- Claim locking protects against duplicate task delivery; idempotency keys protect repeated API requests.
- Unexpected source SQL or compilation errors stop the job. SQLcl includes/connection commands require an official installer rather than silently skipped dependencies.
- Fresh PostgreSQL database per job. Previously migrated targets are retained.
- Read-only account for configured-source business data; privileged provisioning stays in the worker/bootstrap, not the API or website.
- Preflight object inventory, bounded numeric profiling, conservative exact numeric mappings, supported override allowlist, unsupported-type/identifier/nanosecond blockers.
- Configured sources use one Oracle SCN for table reads and verification; snapshot loss fails explicitly. Uploaded sources use locked, read-only scratch tables to avoid fresh-DDL flashback timing failures. Quiesce schema changes on configured sources; this is not CDC or a cutover service.
- Exact row hashes with unambiguous framing, null/type markers and decimal preservation. A disk-backed multiset checks duplicate multiplicities without requiring primary keys or stable row order. Counts, nulls and numeric aggregates are also checked.
- Approved column types, nullability, defaults, primary/unique/check/foreign keys, ordinary indexes and supported simple projection views checked against independently created reference definitions.
- Export to SQL, restore in a second empty PostgreSQL database, repeat value and structure verification.
- Original bytes, SQL, text/JSON reports, verification, logs, checksums, manifest and stored ZIP. Download streaming and range forwarding; frontend/proxy avoid buffering whole downloads.
- History, review/edit/approve, cancellation, safe retry, verification and benchmark evidence in the website.
- Seven-day demo retention; 25 MiB parser-tested upload cap, 500 MiB stored-upload quota, three active jobs per account, one migration worker, statement/job time bounds, disk-space guard and job-owned cleanup.
- SSE events with stable IDs/reconnect, paginated history/events, request IDs, API documentation, account creation restricted to administrators.
- Backup and isolated restore validation, CI configuration, focused tests, real integration/failure tests and reproducible public-dataset benchmark scripts.

## Compatibility boundaries

| Object/behaviour | Treatment |
| --- | --- |
| Ordinary tables, supported exact numbers, text, dates, binary | Automated and value-checked |
| Bare NUMBER | Exact unconstrained NUMERIC; no AI guess |
| PK, FK, UNIQUE, simple CHECK/defaults | Automated where supported; omissions reported |
| Ordinary column B-tree indexes | Automated, including unique indexes |
| Simple single-table column-projection views | Automated |
| Join/expression views, bitmap/domain/function indexes | Manual, inventoried |
| Packages, triggers, dimensions, materialized views, partitions | Manual; base-table data can migrate separately |
| Routines | Optional AI syntax translation; supplied behavioural cases checked separately on uploaded scratch copies |
| COMMIT/autonomous routines | Behavioural execution skipped; require a dedicated, reviewed test protocol |
| Timestamp precision above six fractional digits | Blocked before target creation |
| Quoted/mixed-case or identifiers beyond PostgreSQL's supported limit | Blocked for manual mapping |
| Online cutover, CDC, arbitrary database destinations | Outside this release |

A passed table check does not certify application semantics, Oracle feature parity or all AI-generated routines. Procedure case results prove only the supplied calls. The differential checker has a separate disposable-fixture acceptance test covering outputs, errors and side effects.

## Run and test

```powershell
.\start_all.bat
.\health_all.bat
.\stop_all.bat
```

Website: http://localhost:18501. API: http://localhost:18000/docs.

```powershell
# Official independent sample, pinned source and verified counts
.\benchmarks\setup.ps1
# One warm-up plus three measured runs for COPY and INSERT
python benchmarks\run.py --runs 3
# Additional batch-size study, using identical correctness checks
python benchmarks\run.py --runs 3 --methods copy --batches 1000 5000 10000 20000
# Real API acceptance checks
python scripts\integration_checks.py --output .runtime/integration.json
# Backup and restore test (requires idle jobs)
.\scripts\backup.ps1 -RestoreTest
```

The benchmark runs through HTTP and records raw results, per-phase timings, container samples, peak worker memory and status-request p95. Warm-up runs are labelled and excluded from medians. Results describe the recorded local machine, not a universal throughput guarantee. See `benchmarks/README.md` and `benchmarks/results/` for actual evidence.

## Recovery and deployment

Local Docker is the requested deployment target. Services bind HTTP to loopback; database and queue ports are private to Docker. No remote/cloud deployment or HTTPS staging success is claimed. Trusted local SQL inputs are required: a restricted Oracle schema is not a hardened internet-facing execution sandbox.

API restarts retain jobs, ownership and downloads. A crashed worker produces an interrupted/recoverable job after its heartbeat expires; retry uses a new target. Arbitrary row-level resume is deliberately not claimed. Cancellation can take time until the worker stops the child and releases its database sessions.

Use `scripts/backup.ps1 -RestoreTest` while idle. It pauses API/worker writes, saves metadata and artifact archives, restores into a new metadata database/directory, and checks every restored artifact checksum. It never overwrites the live metadata database. Keep backups off-device and retain `.env.stack` separately. PostgreSQL data targets are disposable products of jobs; downloaded SQL and verification are the portable deliverables. User/job deletion removes only job-owned resources after completion.

## Recorded release validation

The local Docker upgrade passed 18 focused tests, 20 real HTTP acceptance checks, 12 recovery checks, seven routine-checker fixture cases, eight public-dataset benchmark runs, fresh-volume startup/migration/restore, and metadata/artifact backup restore. The restored API also served authenticated history and original/artifact downloads. See [STARTUP_VALIDATION.md](STARTUP_VALIDATION.md) and `benchmarks/results/` for evidence and test boundaries. The build uses PostgreSQL 16 backup/restore utilities to match its server.

## Remaining stretch work

Internet-facing untrusted SQL sandboxing, managed TLS/remote deployment, online CDC/cutover, general package/trigger translation, arbitrary row-level resume, additional database pairs and distributed execution remain separate work. Larger upload limits, 5–10 million-row derivatives, 2/4-worker concurrency and a complete batch-size sweep require additional measured resource budgets. They are not silently advertised as completed tests.
