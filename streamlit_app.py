"""
legacy-db-modernizer: web page

A Streamlit page with two ways in:

  * convert the Oracle database that is already running (the original flow), or
  * upload an Oracle SQL file, which is run in a temporary Oracle schema and
    converted to PostgreSQL files you can download.

Either way the page shows the migration live - which step it is on, which table
is being copied and how far along, any warnings - and, if it stops, exactly what
state things are in. Buttons download the report (and, for an upload, the
converted PostgreSQL files).
"""

import os
import time

import requests
import streamlit as st

from progress_view import (TERMINAL, decode_upload, failure_lines, headline, procedure_rows, progress_fraction,
                           step_rows, table_rows)

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "0.7"))
MODE_LIVE = "The Oracle database already running in Docker"
MODE_UPLOAD = "An Oracle SQL file I upload"

st.set_page_config(page_title="Legacy DB Modernizer", page_icon="🗄️")
st.title("🗄️ Legacy DB Modernizer")
st.caption("Oracle → PostgreSQL migration: deterministic rules, AI only where a judgment call is needed, "
           "and every AI answer checked before it is trusted.")

for key in ("job_id", "report_text", "report_name", "zip_bytes", "zip_name", "sql_bytes", "sql_name", "last_mode", "upload_note"):
    st.session_state.setdefault(key, None)

mode = st.radio("What do you want to convert?", [MODE_LIVE, MODE_UPLOAD], key="mode")
if st.session_state["last_mode"] not in (None, mode):          # switching mode starts a clean page
    st.session_state.update(job_id=None, report_text=None, report_name=None, zip_bytes=None, zip_name=None,
                            sql_bytes=None, sql_name=None)
st.session_state["last_mode"] = mode


def start(endpoint: str, payload: dict = None):
    st.session_state.update(report_text=None, report_name=None, zip_bytes=None, zip_name=None,
                            sql_bytes=None, sql_name=None)
    try:
        response = requests.post(f"{API_BASE_URL}{endpoint}", json=payload, timeout=60)
        if response.status_code >= 400:
            st.session_state["job_id"] = None
            st.error(response.json().get("detail", response.text))
            return
        st.session_state["job_id"] = response.json()["job_id"]
    except requests.exceptions.RequestException:
        st.session_state["job_id"] = None
        st.error("Couldn't reach the API. Start everything with `python start_demo.py`, "
                 "or run `uvicorn api:app` in another terminal.")


if mode == MODE_LIVE:
    st.write("Runs the whole pipeline on the database already in Oracle: read the schema → build the intermediate "
             "representation → map types → create the PostgreSQL tables → copy the data.")
    translate_live = st.checkbox("Also translate the stored procedures with the AI (slower)", value=False, key="translate_live")
    if st.button("🚀 Start Migration", type="primary"):
        start("/migrate/start", {"translate_procedures": translate_live})
else:
    st.write("Upload an Oracle SQL script (tables, data, and optionally stored procedures). It is run in a "
             "temporary Oracle schema, converted, and you download the PostgreSQL result.")
    uploaded = st.file_uploader("Oracle SQL script", type=["sql", "txt", "pls", "plsql", "ddl"])
    translate = st.checkbox("Also translate the stored procedures with the AI (slower)", value=True)
    st.warning("The script runs inside your local Oracle, in a temporary schema that is deleted afterwards. "
               "Only upload scripts you trust.")
    if st.button("🚀 Convert this file", type="primary", disabled=uploaded is None):
        text, note = decode_upload(uploaded.getvalue())
        st.session_state["upload_note"] = note
        start("/upload/start", {"filename": uploaded.name, "content": text, "translate_procedures": translate})
    if st.session_state["upload_note"] and st.session_state["job_id"]:
        st.info(st.session_state["upload_note"])


def draw(data: dict, slots: dict):
    with slots["headline"].container():
        st.subheader(headline(data))
    slots["bar"].progress(progress_fraction(data))
    with slots["steps"].container():
        for icon, label in step_rows(data):
            st.write(f"{icon} {label}")
    with slots["tables"].container():
        rows = table_rows(data)
        if rows:
            st.markdown("**Tables**")
            for icon, name, detail in rows:
                st.write(f"{icon} `{name}` - {detail}")
    with slots["procedures"].container():
        procedures = procedure_rows(data)
        if procedures:
            st.markdown("**Stored procedures**")
            for icon, name, detail in procedures:
                st.write(f"{icon} `{name}` - {detail}")
    with slots["warnings"].container():
        if data.get("warnings"):
            with st.expander(f"⚠️ {len(data['warnings'])} warning(s) - worth a look", expanded=True):
                for warning in data["warnings"]:
                    st.write(f"- {warning}")


if st.session_state["job_id"]:
    job_id = st.session_state["job_id"]
    slots = {name: st.empty() for name in ("headline", "bar", "steps", "tables", "procedures", "warnings", "result")}

    while True:
        try:
            data = requests.get(f"{API_BASE_URL}/migrate/status/{job_id}", timeout=10).json()
        except requests.exceptions.RequestException:
            slots["result"].error("Lost contact with the API. If the server was restarted, the job is gone "
                                  "(status is kept in memory) - start a new migration.")
            break
        draw(data, slots)

        if data.get("status") in TERMINAL:
            with slots["result"].container():
                if data["status"] == "completed":
                    word = "Conversion" if data.get("mode") == "upload" else "Migration"
                    st.success(f"✅ {word} completed at {data.get('completed_at')}. "
                               f"{data.get('rows_total', 0):,} rows copied and saved.")
                    database = (data.get("target") or {}).get("postgres_database")
                    if data.get("mode") == "upload" and database:
                        st.info(f"The converted database is also in PostgreSQL as `{database}`, and the files below "
                                f"load it anywhere: `psql -d yourdb -f converted_postgresql.sql`.")
                    elif data.get("file_available"):
                        st.info("The migrated database is in PostgreSQL, and the file below loads it anywhere: "
                                "`psql -d yourdb -f <the file>`.")
                else:
                    st.error("❌ The conversion did not finish - here is exactly where things stand:")
                    for line in failure_lines(data):
                        st.write(line)
            if st.session_state["report_text"] is None:
                try:
                    r = requests.get(f"{API_BASE_URL}/migrate/report/{job_id}", timeout=15)
                    if r.ok:
                        st.session_state.update(report_text=r.text, report_name=f"migration_report_{job_id[:8]}.txt")
                    if data.get("file_available"):
                        f = requests.get(f"{API_BASE_URL}/migrate/file/{job_id}", timeout=120)
                        if f.ok:
                            st.session_state.update(sql_bytes=f.content, sql_name=data.get("file_name", "migrated_postgresql.sql"))
                    if data.get("download_available"):
                        z = requests.get(f"{API_BASE_URL}/migrate/download/{job_id}", timeout=60)
                        if z.ok:
                            st.session_state.update(zip_bytes=z.content, zip_name=f"converted_{job_id[:8]}.zip")
                except requests.exceptions.RequestException:
                    pass
            break
        time.sleep(POLL_SECONDS)

if st.session_state["sql_bytes"]:
    st.download_button("⬇️ Download the migrated PostgreSQL file (.sql)", st.session_state["sql_bytes"],
                       file_name=st.session_state["sql_name"], mime="application/sql", type="primary")
if st.session_state["zip_bytes"] or st.session_state["report_text"]:
    with st.expander("More downloads (the report, or everything as one .zip)"):
        if st.session_state["zip_bytes"]:
            st.download_button("⬇️ Everything as one .zip (file, report, translated procedures)", st.session_state["zip_bytes"],
                               file_name=st.session_state["zip_name"], mime="application/zip")
        if st.session_state["report_text"]:
            st.download_button("⬇️ The full report", st.session_state["report_text"],
                               file_name=st.session_state["report_name"], mime="text/plain")
