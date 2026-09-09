"""
legacy-db-modernizer: Day 9
Wraps the existing pipeline (schema read -> IR -> type mapping ->
Postgres schema write -> data migration) in a simple FastAPI backend
with two endpoints: start a migration, check its status.

Job tracking uses an in-memory dict, not a real database table (the
original plan called for a migration_jobs table). This is a deliberate
simplification: it's enough to prove the API shape works, but status
resets if the server restarts. Swapping this for a real table later
would be a contained change, not a redesign.
"""

import uuid
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, HTTPException

from read_schema import read_schema
from build_ir import build_ir
from type_mapper import map_schema
from write_postgres_schema import write_schema_to_postgres, get_pg_connection_for_api
from migrate_data import migrate_all_data

app = FastAPI(title="Legacy DB Modernizer API")

# In-memory job store: {job_id: {"status": ..., "started_at": ..., "error": ...}}
jobs = {}


def run_migration(job_id: str):
    jobs[job_id]["status"] = "reading_schema"
    try:
        oracle_schema = read_schema()

        jobs[job_id]["status"] = "building_ir"
        ir = build_ir(oracle_schema)

        jobs[job_id]["status"] = "mapping_types"
        mapped_schema = map_schema(ir)

        jobs[job_id]["status"] = "writing_postgres_schema"
        pg_conn = get_pg_connection_for_api()
        write_schema_to_postgres(mapped_schema, pg_conn)
        pg_conn.close()

        jobs[job_id]["status"] = "migrating_data"
        migrate_all_data()

        jobs[job_id]["status"] = "completed"
        jobs[job_id]["completed_at"] = datetime.utcnow().isoformat()

    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)


@app.post("/migrate/start")
def start_migration(background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "queued",
        "started_at": datetime.utcnow().isoformat(),
    }
    background_tasks.add_task(run_migration, job_id)
    return {"job_id": job_id, "status": "queued"}


@app.get("/migrate/status/{job_id}")
def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


@app.get("/")
def root():
    return {"message": "Legacy DB Modernizer API is running. POST /migrate/start to begin."}