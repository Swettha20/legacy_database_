"""Serve the buildless frontend and forward same-origin requests to the API."""
import os
import tempfile
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
API_BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
MAX_PROXY_BYTES = 27 * 1024 * 1024
ALLOWED_PATHS = {"v1", "migrate", "upload", "health", "system", "openapi.json"}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "frontend" / "index.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/health/live")
@app.get("/_stcore/health", include_in_schema=False)
def live():
    return PlainTextResponse("ok")


@app.api_route("/api/{path:path}", methods=["GET", "POST", "HEAD", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    if path.split("/")[0] not in ALLOWED_PATHS or ".." in path.split("/"):
        raise HTTPException(404, "Unknown API route")
    size = 0
    body = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_PROXY_BYTES:
                raise HTTPException(413, "The upload exceeds the 25 MB file limit.")
            body.write(chunk)
        body.seek(0)
    except Exception:
        body.close()
        raise
    headers = {name: request.headers[name] for name in ("content-type", "accept", "authorization", "cookie", "origin", "host", "idempotency-key", "range", "if-range", "last-event-id")
               if name in request.headers}
    try:
        upstream = await run_in_threadpool(
            requests.request, request.method, API_BASE + "/" + path,
            params=list(request.query_params.multi_items()), data=body,
            headers=headers, timeout=(5, 120), stream=True, allow_redirects=False)
    except requests.RequestException:
        raise HTTPException(502, "The migration API is unavailable. Run start_all or check API health.")
    finally:
        body.close()
    forwarded = {key: value for key, value in upstream.headers.items()
                 if key.lower() in {"content-type", "content-disposition", "cache-control", "set-cookie", "x-request-id", "content-length", "content-range", "accept-ranges", "etag", "last-modified"}}
    return StreamingResponse(upstream.iter_content(64 * 1024),
                             status_code=upstream.status_code, headers=forwarded,
                             background=BackgroundTask(upstream.close))
