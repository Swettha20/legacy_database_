# Database Moderniser upgrade and validation plan

Prepared 16 October 2026. Original scope is retained below; implementation/evidence are tracked in UPGRADE_STATUS.md and benchmarks/results/. Deployment scope is local Docker, as selected by the user. The supplied bronze/blue buildless frontend supersedes the initial React/light-theme proposal.

## 1. Outcome and scope

Build an API-first Oracle-to-PostgreSQL migration service with a separate browser frontend. A user can upload a script or choose an authorised Oracle connection, inspect compatibility, review decisions, migrate into a fresh target, verify the result, and download the original and converted files with evidence.

Every operation must work through the documented HTTP API without opening the website. The frontend never connects directly to databases. Internal workers still use Oracle/PostgreSQL drivers: API-first does not require replacing database protocols with HTTP. Use a configurable hosted AI API by default if the intention is to remove the local-model requirement; retain the provider interface and an explicit no-AI mode. Rules-only schema/data migration must work without an AI key. Procedure translation must clearly report unavailable/skipped status rather than inventing a successful result.

Do not promise universal database support or perfect AI translation. A strong release has a published compatibility matrix and measurable correctness within its supported scope.

## 2. Current evidence

- Library health check: nine passes, one warning, zero failures; AI and container restart checks skipped. Three target tables, matching row counts, matching constraint counts, and three ID counters checked. Checkout function missing from the configured target.
- Generated store fixture: 207,120 rows, ten tables, four routines, 22,736,301 bytes. Loaded into a temporary Oracle schema without SQL or compilation errors; business consistency and routine checks passed. Temporary schema removed. Full website-to-PostgreSQL migration remains untested.
- Existing converted SQL/report/ZIP endpoints are present. Original uploaded bytes are currently decoded and submitted as JSON text, rather than retained as an independently downloadable source artifact.
- Jobs are in memory, uploads can proceed despite individual SQL failures, and live migration replaces target tables before data loading completes.

## 3. Mandatory correctness changes

1. **Strict input handling:** fail a job on unexpected source SQL errors. Separate `failed`, `needs_review`, and `completed_with_warnings`; do not label a partial input conversion as fully verified. Optional best-effort mode must be explicitly selected and enumerate lost statements/records.
2. **Fresh target per job:** prepare a new database/schema without dropping the existing target. Verify it before marking it ready. A later switch of application connections is a separate, explicit operation. Failed/cancelled jobs clean up only resources they own.
3. **Preflight:** inventory tables, columns, constraints, indexes, views, routines, packages, triggers, materialized views, partitions, unsupported types, privileges, disk capacity and character handling. Classify each object as supported, approximated, or manual. Block on missing dependencies or unsafe mappings.
4. **Data-aware type decisions:** use declared precision first, then bounded data profiling where needed. Prefer conservative exact numeric types over AI-selected integer/float types when evidence is insufficient. Detect overflow, fractional loss, precision changes and timestamp/timezone loss.
5. **Verification:** compare counts, null counts, numeric totals/min/max, exact constraint definitions and business queries. Add canonical per-row/chunk hashes with exact decimal handling, defined timestamp/timezone rules, explicit null markers and unambiguous length framing. Handle duplicate rows and tables without primary keys using a multiset strategy, not assumed row order.
6. **Procedure testing:** test outputs, errors and side effects against Oracle using isolated test copies. Treat source routines that commit or invoke external effects separately. Syntax acceptance is distinct from passing behavioural tests.
7. **Consistent reads:** first support documented offline/quiesced sources. Then add a verified common Oracle snapshot where supported, with adequate undo retention and explicit failure on snapshot loss. Continuous capture and zero-downtime cutover are later work.
8. **Export verification:** restore the generated download into a second empty PostgreSQL database and repeat verification. Completion of the target load does not by itself certify the downloaded artifact.

Acceptance: malformed input never becomes verified success; intentional corruption is detected; failing a migration leaves the previous target intact; every omitted object appears in the report.

## 4. API and backend design

Components: browser frontend; FastAPI API; PostgreSQL metadata database; Redis/Celery queue; isolated worker processes; artifact storage; Oracle source and PostgreSQL target. Keep metadata storage logically separate from disposable migration targets.

Persist uploads, plans, mapping overrides, jobs, events, checkpoints, verification results, artifacts and audit records. A queue message references a job ID, not an entire SQL file. Workers claim jobs with a database lock/lease and heartbeat; API restarts must not terminate work. Assume tasks can be delivered twice: use durable phase transitions and isolated staging resources to make retries idempotent. Start with restart-from-phase/table where safe; do not claim arbitrary row-level resume until tested.

Proposed versioned API:

| Operation | Endpoint |
| --- | --- |
| Upload source bytes | `POST /api/v1/uploads` |
| Inspect upload | `GET /api/v1/uploads/{id}` |
| Download untouched original | `GET /api/v1/uploads/{id}/download` |
| Test authorised connection | `POST /api/v1/connections/test` |
| Start compatibility inspection | `POST /api/v1/plans` |
| Retrieve inspection/mapping | `GET /api/v1/plans/{id}` |
| Save reviewed mapping choices | `PATCH /api/v1/plans/{id}/mappings` |
| Create migration | `POST /api/v1/jobs` |
| List history / inspect job | `GET /api/v1/jobs`, `GET /api/v1/jobs/{id}` |
| Subscribe to progress | `GET /api/v1/jobs/{id}/events` |
| Request cancellation | `POST /api/v1/jobs/{id}/cancel` |
| Retry into isolated target | `POST /api/v1/jobs/{id}/retry` |
| Start/retrieve verification | `POST /api/v1/jobs/{id}/verification`, `GET /api/v1/jobs/{id}/verification` |
| List files | `GET /api/v1/jobs/{id}/artifacts` |
| Download a permitted file | `GET /api/v1/artifacts/{id}/download` |
| Liveness/readiness | `GET /health/live`, `GET /health/ready` |

Use asynchronous job responses with `202 Accepted`, typed errors, pagination, request IDs, idempotency keys, authenticated ownership checks and OpenAPI examples. Progress events need stable event IDs and reconnect support; progress must distinguish estimated work from known row/byte counts. Cancellation is cooperative and may take time while a database statement completes; never report cancellation before confirming the outcome.

Replace JSON-contained SQL with multipart streamed uploads, upload-time checksums and configurable limits. Initial proposed limit: 250 MiB; larger multipart/chunked uploads only after storage and memory tests. Update the parser to process bounded chunks or enforce a separately benchmarked parse-size limit: streaming HTTP alone does not make the whole pipeline memory efficient. Maintain existing endpoints temporarily as wrappers.

## 5. Downloads and retention

Each job exposes: original file, converted PostgreSQL SQL, translated routines, human report, machine-readable JSON report, manifest/checksums, logs and a bundle ZIP. Preserve exact uploaded bytes, filename and encoding metadata. Return original files even if migration fails; return diagnostic files on failure; expose converted output only with an accurate verification state. Upload file access must be scoped to the owner.

Store files durably and stream downloads directly; support retries/ranges where implemented. Avoid loading complete SQL/ZIP files into API/frontend memory. Build ZIPs as stored artifacts or stream them with bounded memory. Use safe download filenames, quotas, expiration notices and explicit deletion. Proposed demo retention is seven days, configurable. Validate checksums before download-ready status.

Acceptance: original download hash equals upload hash; output restores successfully; downloads work after page refresh and API restart; another user cannot fetch an artifact by knowing its ID.

## 6. Frontend redesign

Replace the single Streamlit screen with a React/TypeScript web application. Keep Streamlit until the new UI reaches feature parity. The API owns all business logic. Proposed UI style: light neutral background, dark navy navigation, teal primary action, restrained warning/error colours, clear table typography, keyboard support and responsive layouts.

Primary flow: New migration -> Upload or connection -> Inspect -> Review -> Run -> Verify -> Download.

Screens:

- **Dashboard:** migration history, searchable jobs, status, size, duration and last verification; clear New migration button.
- **Source:** upload with progress/size/encoding information; original download action; separate managed connection mode; no passwords in browser storage.
- **Compatibility review:** tables and relationships; source-to-target mapping; explanation of every warning; included/excluded objects; versioned user overrides.
- **Run:** phase timeline, current table, rows and measured rate, elapsed time, event log, warnings, safe cancellation. A browser refresh reconnects to the job.
- **Verification:** separate results for data, structure, procedures and downloadable restore. Avoid a misleading single percentage score.
- **Files:** original and converted files clearly separated, sizes and checksums, downloadable reports and bundle, retention/deletion controls.
- **Benchmarks:** dataset/version, hardware, timing breakdown, correctness status, memory and storage charts, saved comparisons.

Error example: `Stopped: order 104 has an invalid customer reference. 39,999 orders were read; this job was not promoted. Download the diagnostic report.` No generic success banner above failed rows.

## 7. Public Oracle dataset

Use Oracle's **Sales History (SH)** sample as the primary independent benchmark, from the public `oracle-samples/db-sample-schemas` repository. Its published installer expects 1,063,396 records across nine populated tables, including 918,843 sales, 82,112 costs and 55,500 customers. Preserve the repository's permissive licence notices. The sample scripts/data are publicly licensed; Oracle Database itself is proprietary.

Sources:
- Repository: https://github.com/oracle-samples/db-sample-schemas
- Counts: https://github.com/oracle-samples/db-sample-schemas/blob/main/sales_history/sh_install.sql
- Licence: https://github.com/oracle-samples/db-sample-schemas/blob/main/LICENSE.txt
- Requirements: https://github.com/oracle-samples/db-sample-schemas/blob/main/sales_history/README.md
- Loader: https://github.com/oracle-samples/db-sample-schemas/blob/main/sales_history/sh_populate.sql

The current sample requires Oracle 19c+ and SQLcl, and loads accompanying CSV files. Do not upload its installer directly to our current single-file parser: `@@` includes, SQLcl `LOAD`, user creation and data paths are a different input format. Check the installed Oracle version first and use a compatible, pinned release in a dedicated benchmark environment.

Install SH in Oracle with its supported installer, validate every published row count, record actual segment/data sizes, then migrate via the connection API. Test browser upload separately with our generated fixture and, later, a supported exported SH script/package. Pin repository commit, file hashes, schema version, database versions and installer logs. Preserve an unmodified source benchmark; record any compatibility adaptation separately.

SH includes partitions and specialised analytical objects. Measure base-table/data transfer independently from object fidelity, and report missing features. Add ordinary indexes and simple views first; bitmap indexes, dimensions and materialized views need explicit equivalents or manual-work reporting. SH is a volume/analytics test, not sufficient coverage for stored-procedure translation; retain the library, clinic and store routine tests.

## 8. Efficiency test protocol

Datasets: tiny library; generated 207,120-row store; unmodified SH; then reproducibly scaled SH-derived fixtures around five and ten million rows if Oracle edition, RAM and disk permit. Clearly label scaled data as synthetic derivatives; preserve key relationships and business totals.

Measure phases separately: upload, source installation, discovery, profiling/mapping, schema creation, source reads, target load, indexes/constraints, AI translation, verification, export, download and restore. Publish an end-to-end total as well. Record row counts and byte sizes; distinguish logical payload, source segments, target storage, files and PostgreSQL WAL. A million narrow records is not a GB-scale test.

Use the current implementation as baseline and compare one change at a time: row inserts versus COPY on the same real source; batch sizes 1,000/5,000/10,000/20,000; load concurrency 1/2/4 only after isolation is correct; indexes before versus after loading. Keep data checks and final index/constraint state equal. Do not obtain speedups by skipping verification, silently dropping objects or weakening durability.

Run an unmeasured warm-up and at least three measured runs per configuration; report median and range, with any cold-start run shown separately. Record hardware, CPU/RAM limits, disk, network path, database/image versions, cache conditions, application commit and exact command. Measure peak worker/API/database memory, CPU, rows/sec, payload MB/sec, WAL/disk growth, AI calls/retries/cost and API response latency. Use k6 for HTTP load and service responsiveness, not as a replacement for migration correctness checks.

Stress/failure tests: malformed SQL; invalid foreign keys; decimal overflow; null/empty-string differences; Unicode; large text/binary values; source failure; target failure; worker termination; API restart; duplicate task delivery; disk full; AI timeout/rate limit; repeated requests; overlapping jobs; interrupted downloads and cancellation. Run destructive tests only in disposable environments.

Acceptance gates: zero unexplained value mismatches; all omissions enumerated; original hash preserved; restored output verified; target isolation preserved; bounded/characterised memory; no duplicate rows from retries. Provisional API target: p95 status responses below one second during one active large job on the recorded staging hardware. Throughput and final resource budgets are set after the baseline, not invented in advance.

## 9. Deployment and deployment testing

First create reproducible Docker images and Compose configuration. Services: reverse proxy/frontend, API, worker, queue, metadata PostgreSQL, target PostgreSQL and isolated Oracle benchmark instance. Use named persistent volumes and restricted internal networks. Expose only HTTPS for a remote demo; database administration stays private. Separate source provisioner credentials from normal worker credentials; do not use SYSTEM for everyday migration reads.

Start with local Compose, then deploy identical versioned images to a clean Linux staging VM. Suggested initial capacity for a combined benchmark host is four vCPUs, 16 GB RAM and 100 GB SSD, subject to actual measurements and Oracle edition limits; this is a planning assumption, not a guaranteed minimum. Check Oracle image availability, architecture and licence terms before selecting the host. Reserve capacity for simultaneous source, target, restore copy, artifacts and WAL.

Security appropriate to this product: login and owner-scoped jobs/files, secret management, credential/log redaction, TLS, quotas, timeouts, bounded worker concurrency and allowed database destinations. An arbitrary SQL upload is executable code: use dedicated restricted database users and, for an internet-facing service, an isolated disposable database execution environment. Keep the first remote demo authenticated and restricted to trusted inputs until those controls pass.

CI pipeline: static checks -> focused unit/API tests -> real Oracle/PostgreSQL integration tests -> frontend build -> browser tests -> immutable image build -> staging smoke test. Put expensive volume/chaos tests in a separate explicit run. Keep deployment configuration and benchmark configuration distinct.

Deployment acceptance tests:
1. Start on a clean host with documented commands and healthy readiness checks.
2. Complete authenticated upload -> inspect -> migrate -> verify -> download through HTTPS.
3. Restore the downloaded SQL on a fresh PostgreSQL instance and compare results.
4. Refresh browser and restart API; job history and downloads remain available.
5. Terminate/restart a worker; the job becomes recoverable or explicitly failed without corrupting another target.
6. Restart the stack/host and verify volumes, artifact checksums and queue/job reconciliation.
7. Confirm unauthorised job/file requests fail and secrets stay out of logs.
8. Exercise upload limits, proxy limits, timeouts, disk capacity and cleanup/expiry.
9. Upgrade images and roll back using a tested metadata migration/backup strategy.
10. Restore metadata and artifacts from backup and show that recovered files are downloadable.

The remote deployment test requires a selected host/account and access. No cloud spending, remote deployment or successful benchmark is implied by this plan.

## 10. Priorities and approximate sequence

| Stage | Work | Evidence needed to finish |
| --- | --- | --- |
| 1: correctness, 4-6 working days | Strict errors, isolated target, truthful states, initial verification | Negative tests and protected previous target |
| 2: API/artifacts, 4-6 days | Durable jobs, worker/queue, upload/download, auth, API contracts | Headless end-to-end run and restart tests |
| 3: frontend, 4-6 days | Six-step flow, history, review, progress, files | Browser tests and refresh/accessibility checks |
| 4: benchmark, 4-6 days | Install pinned SH, baseline, tuning, restore checks | Reproducible results and correctness report |
| 5: deployment, 3-5 days | Containers, CI, Linux staging, recovery | Fresh-host deployment and failure-test report |

Estimate: about four to six focused weeks for one experienced builder, with extra time for unfamiliar infrastructure or newly discovered compatibility bugs. Integrate tests in each stage; reserve final time for repeatability and documentation. Frontend and backend can advance independently once API contracts are fixed.

Mandatory release scope: strict outcomes, protected targets, durable jobs, original/converted downloads, authenticated API, full supported-data verification, restored-export testing, independent benchmark, clean deployment and honest compatibility documentation.

Useful enhancements after this: ordinary indexes/simple views, mapping profiles, cost estimates, comparison of repeated runs, HTML reports, downloadable API examples/client, optional webhook completion and better accessible small-screen views.

Later stretch scope: true table/chunk resume, common snapshot support across more configurations, robust trigger/package translation, continuous change capture and cutover, extra database pairs, distributed workers and Kubernetes. These are not prerequisites for an excellent focused Oracle-to-PostgreSQL release.

## 11. Release evidence package

Deliver source and architecture notes, API specification/examples, deployment files, compatibility matrix, pinned datasets and licence notices, benchmark runner and raw results, correctness/failure reports, export-restore evidence, recovery runbook, updated project status and a short demonstration recording. The strongest demo intentionally fails an input, explains the failure, protects the old target, then successfully migrates and restores a valid dataset.
