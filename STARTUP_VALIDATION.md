# Local deployment validation

Validated on Windows with Docker Desktop, 16 October 2026. Remote/cloud deployment was excluded by user choice.

| Check | Result | Evidence |
| --- | --- | --- |
| Focused unit/API contracts | 18 passed | `tests/` |
| Full HTTP acceptance | 20 passed | `benchmarks/results/integration.json` |
| API restart, cancellation, worker loss, fresh retry, duplicate delivery | 12 passed | `benchmarks/results/recovery.json` |
| Routine comparison harness | Seven identical cases; no differences/errors | Disposable Oracle/PostgreSQL fixtures; does not certify arbitrary AI output |
| Public volume benchmark | Eight verified runs, including two labelled warm-ups | `benchmarks/results/local.json` |
| New Docker volumes/settings | Startup, login, demo migration and restored SQL passed | `benchmarks/results/clean-deployment.json` |
| Backup/restored storage | 194 metadata records restored; 164 artifact and seven original-upload checksums matched | `benchmarks/results/backup-restore.json` |
| Restored HTTP access | Login, history, artifact download and original byte identity passed | Same backup/restore report |
| Launcher settings | Preservation, independent secrets, provider persistence, paths with spaces passed | `tests/test_stack_scripts.ps1` |
| Frontend JavaScript | Both scripts parse | `scripts/check-frontend.mjs` |

The independent Oracle Sales History source has 1,063,396 base-table rows. Median COPY load was 23.98 seconds versus 27.78 seconds for batched INSERT. Full migration/verification/export/restore took about 154 seconds for COPY and 153 seconds for INSERT; verification dominates and there is no large demonstrated end-to-end speed advantage. Baseline image provenance, resource samples, warm-up labels and adaptations are retained with raw results. Subsequent index/view and recovery enhancements have separate acceptance evidence.

The 207,120-row generated upload also migrated, passed exact value checks, and restored successfully. Original byte preservation, export checksum, range downloads, owner isolation and strict failure on invalid source statements are part of the HTTP acceptance suite.

Final validation exposed and corrected two issues: newly created upload tables could fail Oracle flashback reads, so uploaded scratch tables are locked read-only for copying/verification; and PostgreSQL 17 backup utilities emitted settings unsupported by the PostgreSQL 16 server, so the image now bundles matching PostgreSQL 16 utilities. Configured live sources retain a shared SCN; snapshot failures do not silently fall back to current reads.

Backups and isolated test volumes are retained locally for inspection. The original deployment is restarted after testing. No database volumes were deleted. Backup archives live in the private logs volume under `/app/logs/backups/`; preserve `.env.stack` separately and copy backups off-device for disaster recovery.

## Practical limits

Docker was already installed. Fresh named volumes were tested, but a clean Windows installation, winget/UAC/WSL provisioning, reboot continuation, Linux/macOS device deployment, ARM and Groq inference were not tested. The Oracle slim image lacks Oracle Text; its sample text index omission is recorded. Advanced Oracle objects and routine semantics outside explicitly supplied test cases remain manual. CI configuration is provided; no remote CI run or remote HTTPS staging deployment is claimed.
