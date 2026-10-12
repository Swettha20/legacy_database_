"""
legacy-db-modernizer: API

Wraps the pipeline (schema read -> IR -> type mapping -> Postgres schema
write -> data migration) in a FastAPI backend:

    POST /migrate/start             start a migration, returns a job id
    GET  /migrate/status/{job_id}   live status: current step, per-table progress, warnings
    GET  /migrate/report/{job_id}   the migration report, as a downloadable text file

Job tracking is an in-memory dict, so status is lost if the server restarts.
Run the server WITHOUT --reload for real migrations: with --reload, saving any
file restarts the server and kills a running job.

What a failure means is spelled out in describe_failure(), because it depends
on WHERE it happened: the schema step and the data load are each a single
transaction, so a failure leaves Postgres in a specific, knowable state.
"""

import io
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
import zipfile
from collections import deque
from datetime import datetime

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel

import upload_pipeline as U

from build_ir import build_ir
from generate_report import generate_report
from migrate_data import migrate_all_data
from read_schema import read_schema, read_unmigrated_objects
from type_mapper import map_schema
from write_postgres_schema import write_schema_to_postgres, get_pg_connection_for_api

app = FastAPI(title="Legacy DB Modernizer API")

STEPS = [
    ("reading_schema", "Read the Oracle schema"),
    ("building_ir", "Build the intermediate representation"),
    ("mapping_types", "Map types (AI only where ambiguous)"),
    ("writing_postgres_schema", "Create the PostgreSQL tables"),
    ("migrating_data", "Copy the data"),
]
STEP_KEYS = [key for key, _ in STEPS]

# State of one table while data is copied. A table is only SAVED once every
# table has been copied and the single transaction commits.
PENDING, LOADING, LOADED, COMMITTED, ROLLED_BACK, FAILED = (
    "pending", "loading", "loaded", "committed", "rolled_back", "failed")

HERE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_ROOT = os.path.join(HERE, "uploads")
WORKER_COMMAND = [sys.executable, os.path.join(HERE, "upload_pipeline.py"), "--worker"]
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class MigrateRequest(BaseModel):
    translate_procedures: bool = False


def live_steps(translate: bool) -> list:
    """The five steps; with stored-procedure translation, two more are added."""
    if not translate:
        return list(STEPS)
    return list(STEPS) + [("translating_procedures", "Translate the stored procedures (AI)"),
                          ("exporting_files", "Build the PostgreSQL file to download")]


def upload_steps(translate: bool) -> list:
    steps = [("loading_script", "Run your Oracle script in a scratch Oracle schema"),
             ("reading_schema", "Read the schema"),
             ("building_ir", "Build the intermediate representation"),
             ("mapping_types", "Map types (AI only where ambiguous)"),
             ("writing_postgres_schema", "Create the PostgreSQL tables"),
             ("migrating_data", "Copy the data")]
    if translate:
        steps.append(("translating_procedures", "Translate the stored procedures (AI)"))
    steps.append(("exporting_files", "Build the PostgreSQL files to download"))
    return steps


jobs = {}           # job_id -> status dict (this is what the status endpoint returns)
_artifacts = {}     # job_id -> things the report needs but the status should not carry


def migrated_file_path(job_id: str) -> str:
    return os.path.join(UPLOAD_ROOT, job_id[:8], "converted_postgresql.sql")


def migrated_file_name(job: dict) -> str:
    """What the browser saves the file as: <your file's name>_postgresql.sql, or migrated_postgresql.sql."""
    base = os.path.basename(job.get("filename") or "")
    stem, extension = os.path.splitext(base)
    if not extension and base.startswith("."):       # a name that is only an extension, like ".sql"
        stem = ""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return f"{stem}_postgresql.sql" if stem else "migrated_postgresql.sql"


def new_job(steps: list = None, mode: str = "database") -> dict:
    steps = steps or STEPS
    return {
        "mode": mode,
        "status": "queued",
        "started_at": datetime.utcnow().isoformat(),
        "started_ts": time.time(),
        "steps": [{"key": key, "label": label} for key, label in steps],
        "step": 0,
        "step_total": len(steps),
        "procedures": [],
        "tables": {},
        "table_order": [],
        "rows_total": 0,
        "warnings": [],
        "committed": False,
        "summary": None,
        "failure": None,
        "error": None,
    }


def set_step(job: dict, key: str):
    keys = [s["key"] for s in job["steps"]]
    job["status"] = key
    job["step"] = keys.index(key) + 1


def progress_handler(job: dict):
    """The callback the data loader reports to; turns events into job state."""
    def handle(event: dict):
        kind = event.get("event")
        if kind == "plan":
            job["table_order"] = list(event["tables"])
            job["tables"] = {t: {"state": PENDING, "rows": 0, "seconds": None} for t in event["tables"]}
        elif kind == "table_start":
            job["tables"][event["table"]]["state"] = LOADING
        elif kind == "rows":
            job["tables"][event["table"]]["rows"] = event["rows"]
        elif kind == "table_done":
            t = job["tables"][event["table"]]
            t.update(state=LOADED, rows=event["rows"], seconds=round(event["seconds"], 2))
        elif kind == "committed":
            job["committed"] = True
            for t in job["tables"].values():
                if t["state"] == LOADED:
                    t["state"] = COMMITTED
        elif kind == "warning":
            job["warnings"].append(event["message"])
        job["rows_total"] = sum(t["rows"] for t in job["tables"].values())
    return handle


def describe_failure(job: dict, error: str) -> dict:
    """
    An accurate account of what state things are in after a failure. The
    wording depends on the step, because:
      * before the Postgres schema is written, nothing in Postgres was touched;
      * the schema step is ONE transaction - a failure leaves it unchanged;
      * the data load is ONE transaction - a failure saves NOTHING, including
        tables that had already been read, and the tables stay as the schema
        step left them (new and empty);
      * for an uploaded script, the PostgreSQL database is brand new, and the
        last steps (procedures, files) come after the data is already saved.
    """
    keys = [st["key"] for st in job["steps"]]
    labels = {st["key"]: st["label"] for st in job["steps"]}
    step = job["status"] if job["status"] in keys else keys[min(max(job["step"], 1), len(keys)) - 1]
    tables = job["tables"]
    upload = job.get("mode") == "upload"
    target = job.get("target", {}).get("postgres_database", "the new database")
    out = {"step": step, "step_label": labels[step], "error": error,
           "loaded_then_rolled_back": [], "failed_table": None, "not_started": [], "saved": []}

    if step == "loading_script":
        out["headline"] = "Oracle could not use your script."
        out["postgres_state"] = "Nothing was created in PostgreSQL. The temporary Oracle schema was removed."
    elif step in ("reading_schema", "building_ir", "mapping_types"):
        out["headline"] = f"Stopped while trying to: {out['step_label'].lower()}."
        out["postgres_state"] = ("Nothing was written to PostgreSQL." if upload else
                                 "PostgreSQL was not touched. Your existing tables and data are exactly as they were.")
    elif step == "writing_postgres_schema":
        out["headline"] = "Stopped while creating the PostgreSQL tables."
        out["postgres_state"] = ("Nothing was written to PostgreSQL." if upload else
                                 "The table creation is a single all-or-nothing step, so PostgreSQL was left "
                                 "unchanged: your existing tables and data are exactly as they were.")
    elif upload and step in ("translating_procedures", "exporting_files"):
        out["saved"] = list(job["table_order"])
        out["headline"] = "The data was converted and saved; a later step failed."
        out["postgres_state"] = (f"The tables and their data ARE in the PostgreSQL database '{target}'. "
                                 f"Only '{out['step_label']}' failed, so " +
                                 ("the stored procedures may be missing" if step == "translating_procedures"
                                  else "the downloadable PostgreSQL file was not produced") + ".")
    elif job["committed"]:
        out["saved"] = list(job["table_order"])
        out["headline"] = "All data was copied and saved; only the final tidy-up failed."
        out["postgres_state"] = ("Every table's data IS saved. What failed afterwards (resetting id counters or "
                                 "refreshing statistics) can be repeated safely - run the migration again, or "
                                 "run `python migrate_data.py`.")
    else:
        failed = next((t for t in job["table_order"] if tables[t]["state"] == LOADING), None)
        out["failed_table"] = failed
        out["loaded_then_rolled_back"] = [t for t in job["table_order"] if tables[t]["state"] == LOADED]
        out["not_started"] = [t for t in job["table_order"] if tables[t]["state"] == PENDING]
        for name, t in tables.items():
            t["state"] = FAILED if name == failed else ROLLED_BACK if t["state"] == LOADED else t["state"]
        out["headline"] = ("Stopped while copying the data" + (f" (during table '{failed}')." if failed else "."))
        out["postgres_state"] = (
            "The data load is all-or-nothing, so NOTHING from this load was saved - including the tables that "
            "had already been read. " +
            ("The PostgreSQL tables exist but are empty." if upload else
             "The PostgreSQL tables exist (the schema step succeeded) but are empty, so data from an earlier "
             "migration is gone until you run it again."))
    out["next_step"] = ("Fix the cause shown above and upload the file again; every upload is converted from scratch."
                        if upload else
                        "Fix the cause shown above and start the migration again. A re-run rebuilds everything from "
                        "scratch, so it is safe to repeat.")
    return out


def result_section(job: dict) -> str:
    lines = ["", "=" * 60, "MIGRATION RESULT", "=" * 60]
    lines.append(f"Status:   {job['status'].upper()}")
    lines.append(f"Started:  {job['started_at']} (UTC)")
    lines.append(f"Duration: {job.get('elapsed_seconds', round(time.time() - job['started_ts'], 1))}s")
    if job.get("mode") == "upload":
        lines.append(f"Source:   uploaded file {job.get('filename', '(unnamed)')}")
        t = job.get("target", {})
        lines.append(f"Target:   PostgreSQL database {t.get('postgres_database', '?')} (the temporary Oracle schema "
                     f"{t.get('oracle_schema', '?')} was removed)")
        sc = job.get("script")
        if sc:
            lines.append(f"Script:   {sc['executed']} statements ran, {len(sc['failed'])} failed, "
                         f"{len(sc['skipped'])} SQL*Plus commands skipped, {sc['tables']} tables created")
    if job["tables"]:
        lines.append("")
        lines.append("Tables:")
        wording = {PENDING: "not started", LOADING: "in progress", LOADED: "read, not yet saved",
                   COMMITTED: "saved", ROLLED_BACK: "rolled back (NOT saved)", FAILED: "FAILED"}
        for name in job["table_order"]:
            t = job["tables"][name]
            secs = f", {t['seconds']}s" if t["seconds"] is not None else ""
            lines.append(f"  {name:28} {t['rows']:>10,} rows  {wording[t['state']]}{secs}")
        lines.append(f"  {'TOTAL':28} {job['rows_total']:>10,} rows")
    if job.get("procedures"):
        lines.append("")
        lines.append("Stored procedures:")
        for p in job["procedures"]:
            lines.append(f"  {p['name']:28} {p.get('status', 'working...'):18}"
                         + (f" attempts={p['attempts']}" if "attempts" in p else "")
                         + ("  installed in PostgreSQL" if p.get("installed") else ""))
    if job["warnings"]:
        lines.append("")
        lines.append(f"Warnings ({len(job['warnings'])}):")
        lines.extend(f"  - {w}" for w in job["warnings"])
    f = job["failure"]
    if f:
        lines.append("")
        lines.append("WHAT WENT WRONG:")
        lines.append(f"  {f['headline']}")
        lines.append(f"  Error: {f['error']}")
        lines.append(f"  State: {f['postgres_state']}")
        if f["loaded_then_rolled_back"]:
            lines.append(f"  Read, then rolled back (not saved): {', '.join(f['loaded_then_rolled_back'])}")
        if f["failed_table"]:
            lines.append(f"  Failed on: {f['failed_table']}")
        if f["not_started"]:
            lines.append(f"  Never started: {', '.join(f['not_started'])}")
        lines.append(f"  Next: {f['next_step']}")
    lines.append("")
    return "\n".join(lines)


def build_report_text(job_id: str) -> str:
    job, mapped = jobs[job_id], _artifacts.get(job_id, {}).get("mapped")
    return (generate_report(mapped) if mapped else "MIGRATION REPORT (type mapping did not complete)\n") + result_section(job)


def export_converted_file(job: dict, job_dir: str, mapped: dict, functions: list):
    """Writes the downloadable PostgreSQL script. The data is already saved by now, so a
    failure here is a warning, never a reason to call the migration failed."""
    from migrate_data import get_pg_connection
    try:
        os.makedirs(job_dir, exist_ok=True)
        conn = get_pg_connection()
        try:
            U.export_dump(conn, mapped, functions, os.path.join(job_dir, "converted_postgresql.sql"))
        finally:
            conn.close()
    except Exception as e:
        job["warnings"].append(f"The downloadable PostgreSQL file could not be built ({str(e).strip().splitlines()[0][:150]}); "
                               f"the data itself is saved in PostgreSQL.")


def run_migration(job_id: str, translate: bool = False):
    job = jobs[job_id]
    job_dir = os.path.join(UPLOAD_ROOT, job_id[:8])
    try:
        set_step(job, "reading_schema")
        oracle_schema = read_schema()

        set_step(job, "building_ir")
        ir = build_ir(oracle_schema, read_unmigrated_objects())

        set_step(job, "mapping_types")
        mapped_schema = map_schema(ir)
        _artifacts[job_id] = {"mapped": mapped_schema}

        set_step(job, "writing_postgres_schema")
        pg_conn = get_pg_connection_for_api()
        try:
            write_schema_to_postgres(mapped_schema, pg_conn)
        finally:
            pg_conn.close()          # an uncommitted schema change is rolled back by closing

        set_step(job, "migrating_data")
        job["summary"] = migrate_all_data(progress=progress_handler(job))

        functions = []
        if translate:
            set_step(job, "translating_procedures")
            functions = U.translate_procedures(job_dir, lambda **event: handle_worker_event(job, event))
            set_step(job, "exporting_files")
        export_converted_file(job, job_dir, mapped_schema, functions)

        job["status"] = "completed"
        job["step"] = job["step_total"]
        job["completed_at"] = datetime.utcnow().isoformat()
    except Exception as e:
        job["error"] = str(e)
        job["failure"] = describe_failure(job, str(e))
        job["status"] = "failed"
    finally:
        job["elapsed_seconds"] = round(time.time() - job["started_ts"], 1)


@app.post("/migrate/start")
def start_migration(background_tasks: BackgroundTasks, request: MigrateRequest = None):
    translate = bool(request and request.translate_procedures)
    job_id = str(uuid.uuid4())
    jobs[job_id] = new_job(live_steps(translate))
    background_tasks.add_task(run_migration, job_id, translate)
    return {"job_id": job_id, "status": "queued"}


@app.get("/migrate/status/{job_id}")
def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = dict(jobs[job_id])
    job.setdefault("elapsed_seconds", round(time.time() - job["started_ts"], 1))
    job["report_available"] = job["status"] in ("completed", "failed")
    job["download_available"] = job["report_available"] and (
        job.get("mode") == "upload" or os.path.exists(migrated_file_path(job_id)))
    job["file_available"] = job["report_available"] and os.path.exists(migrated_file_path(job_id))
    job["file_name"] = migrated_file_name(job)
    return job


@app.get("/migrate/report/{job_id}", response_class=PlainTextResponse)
def get_report(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    if jobs[job_id]["status"] not in ("completed", "failed"):
        raise HTTPException(status_code=409, detail="The migration has not finished yet")
    return PlainTextResponse(
        build_report_text(job_id),
        headers={"Content-Disposition": f'attachment; filename="migration_report_{job_id[:8]}.txt"'},
    )


# ------------------------------------------------------------ uploaded scripts


class UploadRequest(BaseModel):
    filename: str = "script.sql"
    content: str
    translate_procedures: bool = True


def _connect_admin():
    import oracledb
    from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN
    return oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)


def _connect_user(schema: str, password: str):
    import oracledb
    from config import ORACLE_DSN
    return oracledb.connect(user=schema, password=password, dsn=ORACLE_DSN)


def _active_upload():
    return next((jid for jid, j in jobs.items()
                 if j.get("mode") == "upload" and j["status"] not in ("completed", "failed")), None)


def handle_worker_event(job: dict, event: dict):
    kind = event.get("event")
    if kind == "step":
        if event["key"] in [s["key"] for s in job["steps"]]:
            set_step(job, event["key"])
    elif kind == "procedure_start":
        job["procedures"].append({"name": event["name"], "status": "translating..."})
    elif kind == "procedure_done":
        entry = next((p for p in job["procedures"] if p["name"] == event["name"]), None)
        if entry is None:
            entry = {"name": event["name"]}
            job["procedures"].append(entry)
        entry.update(status=event["status"], attempts=event["attempts"], installed=event["installed"],
                     notes=event.get("notes", 0))
    elif kind == "done":
        job["summary"] = event.get("summary")
        job["worker_done"] = True
    elif kind == "error":
        job["worker_error"] = event["message"]
    else:
        progress_handler(job)(event)


def record_script_load(job: dict, load):
    job["script"] = {"executed": load.executed, "failed": load.failed, "skipped": load.skipped,
                     "tables": load.tables, "compile_errors": load.compile_errors}
    for f in load.failed[:U.MAX_ERRORS_SHOWN]:
        job["warnings"].append(f"Your script: line {f['line']} ({f['kind']}) failed in Oracle: {f['message']}")
    if len(load.failed) > U.MAX_ERRORS_SHOWN:
        job["warnings"].append(f"...and {len(load.failed) - U.MAX_ERRORS_SHOWN} more statements failed.")
    for e in load.compile_errors[:U.MAX_ERRORS_SHOWN]:
        job["warnings"].append(f"Oracle could not compile: {e}")
    if load.skipped:
        job["warnings"].append(f"{len(load.skipped)} SQL*Plus command line(s) were skipped (they are not SQL).")


def run_worker(job: dict, job_dir: str, schema: str, pg_database: str, translate: bool):
    env = dict(os.environ, ORACLE_SCHEMA=schema, PG_DBNAME=pg_database,
               PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    env.pop("ORACLE_TABLES", None)       # convert everything the script created, not a list meant for another run
    cmd = WORKER_COMMAND + [job_dir] + (["--translate"] if translate else [])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env, cwd=HERE)
    tail = deque(maxlen=25)
    for line in proc.stdout:
        if line.startswith(U.EVENT_MARK):
            try:
                handle_worker_event(job, json.loads(line[len(U.EVENT_MARK):]))
            except (ValueError, KeyError):
                tail.append(line.rstrip())
        else:
            tail.append(line.rstrip())
    code = proc.wait()
    if job.get("worker_error"):
        raise RuntimeError(job["worker_error"])
    if code != 0 or not job.get("worker_done"):
        raise RuntimeError("the conversion process stopped unexpectedly"
                           + (": " + " | ".join(list(tail)[-3:]) if tail else f" (exit code {code})"))


def run_upload(job_id: str, script_text: str, translate: bool):
    job = jobs[job_id]
    schema, pg_database = U.scratch_schema_name(job_id), U.pg_database_name(job_id)
    job_dir = os.path.join(UPLOAD_ROOT, job_id[:8])
    job["target"] = {"oracle_schema": schema, "postgres_database": pg_database}
    password = secrets.token_hex(12)          # lives only in this function; never stored on the job
    admin, schema_created, database_created = None, False, False
    try:
        os.makedirs(job_dir, exist_ok=True)
        set_step(job, "loading_script")
        admin = _connect_admin()
        U.create_scratch_schema(admin, schema, password)
        schema_created = True
        user = _connect_user(schema, password)
        try:
            try:
                load = U.run_script(user, script_text,
                                    progress=lambda done, total: job.update(script_progress={"done": done, "total": total}))
            except U.ScriptError as e:
                if e.result is not None:
                    record_script_load(job, e.result)      # keep the full list of what failed, even on failure
                raise
        finally:
            user.close()
        record_script_load(job, load)

        U.create_pg_database(pg_database)
        database_created = True
        run_worker(job, job_dir, schema, pg_database, translate)

        job["status"] = "completed"
        job["step"] = job["step_total"]
        job["completed_at"] = datetime.utcnow().isoformat()
    except Exception as e:
        job["error"] = str(e)
        job["failure"] = describe_failure(job, str(e))
        job["status"] = "failed"
        if database_created and not job["committed"]:
            try:
                U.drop_pg_database(pg_database)        # an empty database left behind helps nobody
                job["target"]["postgres_database"] = None
            except Exception:
                pass
    finally:
        if admin is not None:
            if schema_created:
                try:
                    U.drop_scratch_schema(admin, schema)
                except Exception as e:
                    job["warnings"].append(f"The temporary Oracle schema {schema} could not be removed ({e}); "
                                           f"remove it with: DROP USER {schema} CASCADE")
            try:
                admin.close()
            except Exception:
                pass
        mapped_path = os.path.join(job_dir, "mapped_schema.json")
        if os.path.exists(mapped_path):
            with open(mapped_path, encoding="utf-8") as f:
                _artifacts[job_id] = {"mapped": json.load(f)}
        job["elapsed_seconds"] = round(time.time() - job["started_ts"], 1)


@app.post("/upload/start")
def start_upload(request: UploadRequest, background_tasks: BackgroundTasks):
    content = request.content
    if not content.strip():
        raise HTTPException(status_code=400, detail="The file is empty.")
    if len(content.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if not re.search(r"\bCREATE\s+(GLOBAL\s+TEMPORARY\s+)?TABLE\b", content, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="No CREATE TABLE statement found - this does not look like an "
                                                    "Oracle SQL script that defines a database.")
    busy = _active_upload()
    if busy:
        raise HTTPException(status_code=409, detail="Another upload is still being converted. Wait for it to finish.")
    job_id = str(uuid.uuid4())
    jobs[job_id] = new_job(upload_steps(request.translate_procedures), mode="upload")
    jobs[job_id]["filename"] = os.path.basename(request.filename) or "script.sql"
    background_tasks.add_task(run_upload, job_id, content, request.translate_procedures)
    return {"job_id": job_id, "status": "queued"}


def build_zip(job_id: str) -> bytes:
    job = jobs[job_id]
    job_dir = os.path.join(UPLOAD_ROOT, job_id[:8])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.txt", build_report_text(job_id))
        for root, _, files in os.walk(job_dir):
            for name in sorted(files):
                if name == "mapped_schema.json":
                    continue
                path = os.path.join(root, name)
                z.write(path, os.path.relpath(path, job_dir).replace(os.sep, "/"))
        sc = job.get("script")
        if sc:
            lines = [f"{sc['executed']} statements ran; {len(sc['failed'])} failed; {sc['tables']} tables created.", ""]
            lines += [f"line {f['line']} ({f['kind']}): {f['message']}" for f in sc["failed"]]
            lines += [""] + [f"skipped (SQL*Plus command): {c}" for c in sc["skipped"]]
            lines += [""] + [f"compile error: {e}" for e in sc["compile_errors"]]
            z.writestr("script_load_log.txt", "\n".join(lines) + "\n")
    return buffer.getvalue()


@app.get("/migrate/file/{job_id}")
def download_migrated_file(job_id: str):
    """The migrated PostgreSQL script itself, as a plain .sql download (no zip, no report)."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] not in ("completed", "failed"):
        raise HTTPException(status_code=409, detail="The conversion has not finished yet")
    path = migrated_file_path(job_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No migrated file was produced for this job")
    return FileResponse(path, media_type="application/sql", filename=migrated_file_name(job))


@app.get("/migrate/download/{job_id}")
@app.get("/upload/download/{job_id}")
def download_converted(job_id: str):
    """The converted files as a zip: the PostgreSQL script, the report, translated procedures."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] not in ("completed", "failed"):
        raise HTTPException(status_code=409, detail="The conversion has not finished yet")
    if job.get("mode") != "upload" and not os.path.exists(os.path.join(UPLOAD_ROOT, job_id[:8], "converted_postgresql.sql")):
        raise HTTPException(status_code=404, detail="No converted file was produced for this job")
    return Response(build_zip(job_id), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="converted_{job_id[:8]}.zip"'})


@app.get("/")
def root():
    return {"message": "Legacy DB Modernizer API is running. POST /migrate/start to begin."}
