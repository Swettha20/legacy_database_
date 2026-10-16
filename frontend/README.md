# Browser workspace

The frontend is a buildless HTML/CSS/JavaScript file adapted from the supplied Bronze and Time reference: dark bronze/blue palette, editorial typography, inline SVG database artwork and lightweight ambient particles. It retains responsive layouts, keyboard controls and reduced-motion support.

Run with `start_all`. The website is http://localhost:18501. Sign in as admin using STACK_ADMIN_PASSWORD from private .env.stack.

Flow: upload or configured source → inspect → review mappings → approve → migrate → verify → download. History reconnects after refresh and API restarts. Cancellation, fresh-target retry and benchmark evidence use the same HTTP API available to headless clients.

`web_server.py` serves the frontend and proxies same-origin `/api` traffic, session cookies, bearer headers, SSE and file ranges to FastAPI. Request bodies spool to disk above 1 MiB; downloads stream. Database credentials stay in the worker/provisioner. No tokens/passwords are saved in browser storage; only the last job UUID is remembered.

The backend uses PostgreSQL metadata and Redis/Celery. Business logic stays on the API/worker side. See `UPGRADE_STATUS.md` for supported objects, tests, retention and limitations.
