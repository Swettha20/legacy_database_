# Start, stop and check the complete stack

After cloning the repository, double-click **start_all.bat** on Windows. Linux/macOS with Docker: `sh start_all.sh --no-browser`.

The launcher downloads/builds the app, Oracle Free, PostgreSQL and Redis; generates private passwords; waits for databases; provisions the metadata database and limited reader; seeds only an empty DEMO schema; starts the worker/API/website; then checks readiness. Existing database records, settings, artifacts and optional AI models are retained.

Windows can install Docker Desktop through winget if missing. Complete Windows administrator, Docker licence, WSL2, virtualization and reboot prompts yourself, then run start_all again. These OS prerequisites cannot be guaranteed silently. Linux/macOS require Docker Engine + Compose or Docker Desktop to be installed first. No host Python is needed for the application.

Website: http://localhost:18501

API documentation: http://localhost:18000/docs

Sign in as **admin**, using **STACK_ADMIN_PASSWORD** in the generated private `.env.stack`. Do not commit that file. API clients can log in at `POST /v1/auth/login` and send the returned bearer token. Browser authentication uses an HttpOnly cookie; only the last job UUID is stored in the browser.

## Simple flow

Upload a trusted Oracle script, or choose an authorised configured source. Click **Inspect my database**, review mappings and omissions, then **Approve and migrate**. The worker creates a separate target database, copies data, checks every supported value and approved structure, exports SQL, restores it into another empty database and checks again. Download the original, converted SQL, verification, reports or ZIP.

History and downloads survive API restarts. Worker crashes become interrupted/recoverable jobs; retry uses a fresh target. Previous targets are retained.

## Commands

```powershell
.\start_all.bat
.\health_all.bat
.\stop_all.bat

# Options
.\start_all.ps1 -NoBrowser -NoInstall
.\start_all.ps1 -SkipAI -NoBrowser
.\health_all.ps1 -SkipAI
```

```sh
sh start_all.sh --no-browser
sh health_all.sh
sh stop_all.sh
```

Stop affects only this Compose project. It preserves named volumes and private settings, and does not stop unrelated containers or Docker Desktop. Let important jobs finish before stopping. First-run downloads are several GB; Oracle initialisation can take minutes. Around 16 GB host RAM and at least 20 GB free disk are practical starting budgets, subject to workload. Use Linux containers.

## Optional AI

New installations default to `STACK_AI_PROVIDER=none`. Exact schema/data migration works without a model or AI key. Existing provider settings are preserved.

For hosted inference, set `STACK_AI_PROVIDER=groq` and `STACK_GROQ_API_KEY` in private `.env.stack`, then rerun start_all. Accounts/keys cannot be created automatically. For the first run, `start_all.ps1 -AiProvider groq` reads GROQ_API_KEY from the shell environment.

For a local model, set `STACK_AI_PROVIDER=ollama`. The launcher downloads the configured model once and reuses its volume. The small default model is a demo choice; routine behaviour needs separate testing. `-SkipAI` explicitly skips model setup and inference checks.

## Health, benchmarks and recovery

Infrastructure health checks connectivity, metadata, queue, worker and website. Per-job verification is separate; an expected failed test job does not imply broken infrastructure.

- Read [UPGRADE_STATUS.md](UPGRADE_STATUS.md) for supported objects, API behaviour and limitations.
- Run `benchmarks/setup.ps1` to install/verify the pinned official Sales History sample. See [benchmarks/README.md](benchmarks/README.md).
- Run `scripts/backup.ps1 -RestoreTest` while jobs are idle. It pauses writes, backs up metadata/artifacts, restores an isolated copy and verifies checksums. Keep backups off-device and private settings separately.

The selected deployment target is local Docker. HTTP binds to loopback; database/queue ports are not published. This is for trusted inputs, not public anonymous SQL execution. Remote HTTPS/cloud deployment has not been requested for this test.

## Troubleshooting

If startup stops, data is retained. Resolve the displayed error and run the same command again. Do not regenerate passwords for existing database volumes. Common causes: Docker not started, Windows reboot/WSL requirements, port conflicts, insufficient memory/disk or unavailable downloads.

```powershell
docker compose --env-file .env.stack --profile local-ai logs --tail 80
```

Do not share resolved Compose configuration or private settings: they contain credentials. Use a different STACK_PROJECT_NAME and ports for an independent deployment; changing the project name also selects new named volumes. Do not delete existing volumes to resolve a startup warning.
