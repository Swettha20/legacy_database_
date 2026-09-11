"""
legacy-db-modernizer: Day 10
A simple Streamlit UI that calls the FastAPI backend (Day 9) to trigger
a migration and poll its status - rather than running the pipeline
functions directly. This keeps the UI as just one possible "client" of
the API, the same way a future real frontend or another script could be.
"""

import time
import requests
import streamlit as st

API_BASE_URL = "http://localhost:8000"

st.set_page_config(page_title="Legacy DB Modernizer", page_icon="🗄️")
st.title("🗄️ Legacy DB Modernizer")
st.caption("Oracle → PostgreSQL migration, powered by a local pipeline + AI-assisted review.")

if "job_id" not in st.session_state:
    st.session_state["job_id"] = None

st.write("This runs the full pipeline: read Oracle schema → build IR → "
         "map types → write Postgres schema → migrate data.")

if st.button("🚀 Start Migration", type="primary"):
    try:
        response = requests.post(f"{API_BASE_URL}/migrate/start")
        response.raise_for_status()
        st.session_state["job_id"] = response.json()["job_id"]
        st.success(f"Migration started. Job ID: {st.session_state['job_id']}")
    except requests.exceptions.ConnectionError:
        st.error("Couldn't reach the API. Is `uvicorn api:app --reload` running?")

if st.session_state["job_id"]:
    status_placeholder = st.empty()
    job_id = st.session_state["job_id"]

    # Poll the status endpoint every second until the job finishes or fails.
    # This is a simple loop, not a background thread - fine for a demo/local
    # tool where the migration finishes in seconds; a production UI would
    # use something more sophisticated (e.g. auto-refresh on a timer).
    while True:
        response = requests.get(f"{API_BASE_URL}/migrate/status/{job_id}")
        data = response.json()
        status = data.get("status", "unknown")

        status_placeholder.info(f"Status: **{status}**")

        if status == "completed":
            status_placeholder.success(
                f"✅ Migration completed at {data.get('completed_at')}"
            )
            break
        elif status == "failed":
            status_placeholder.error(f"❌ Migration failed: {data.get('error')}")
            break

        time.sleep(1)