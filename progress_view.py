"""
legacy-db-modernizer: how a job's status is shown on the page

Pure functions (no Streamlit, no network) so the wording and the arithmetic
can be tested: the page is just a thin layer that draws what these return.
"""

STATE_ICON = {"pending": "⏳", "loading": "🔄", "loaded": "📥", "committed": "✅", "rolled_back": "↩️", "failed": "❌"}
STATE_TEXT = {
    "pending": "waiting",
    "loading": "copying…",
    "loaded": "copied - saved when every table is done",
    "committed": "saved",
    "rolled_back": "rolled back - NOT saved",
    "failed": "failed",
}
TERMINAL = ("completed", "failed")


def step_rows(data: dict) -> list:
    """[(icon, label)] for the pipeline steps: done, current, waiting, or failed."""
    steps, current, status = data.get("steps", []), data.get("step", 0), data.get("status", "queued")
    rows = []
    for i, step in enumerate(steps, 1):
        if status == "completed" or i < current:
            icon = "✅"
        elif i == current:
            icon = "❌" if status == "failed" else "🔄"
        else:
            icon = "⏳"
        rows.append((icon, f"{i}. {step['label']}"))
    return rows


def table_rows(data: dict) -> list:
    """[(icon, name, description)] for each table, in load order."""
    out = []
    for name in data.get("table_order", []):
        t = data["tables"][name]
        rows = f"{t['rows']:,} rows" if t["state"] != "pending" else ""
        secs = f" in {t['seconds']}s" if t.get("seconds") is not None else ""
        detail = f"{rows}{secs} - " if rows else ""
        out.append((STATE_ICON[t["state"]], name, f"{detail}{STATE_TEXT[t['state']]}"))
    return out


def progress_fraction(data: dict) -> float:
    """Share of tables fully copied (0..1); 1.0 once the job completed."""
    if data.get("status") == "completed":
        return 1.0
    tables = data.get("tables", {})
    if not tables:
        return 0.0
    done = sum(1 for t in tables.values() if t["state"] in ("loaded", "committed"))
    return done / len(tables)


def headline(data: dict) -> str:
    status, tables, order = data.get("status", "queued"), data.get("tables", {}), data.get("table_order", [])
    if status == "queued":
        return "Starting…"
    if status == "completed":
        return f"Done: {len(order)} tables, {data.get('rows_total', 0):,} rows in {data.get('elapsed_seconds', 0)}s"
    if status == "failed":
        return "Stopped before finishing"
    if status == "migrating_data" and order:
        current = next((n for n in order if tables[n]["state"] == "loading"), None)
        if current:
            n = order.index(current) + 1
            return f"Copying table {n} of {len(order)}: {current} ({tables[current]['rows']:,} rows so far)"
        done = sum(1 for t in tables.values() if t["state"] in ("loaded", "committed"))
        return f"{done} of {len(order)} tables copied - saving"
    label = next((s["label"] for s in data.get("steps", []) if s["key"] == status), status)
    return f"Step {data.get('step', 0)} of {data.get('step_total', 5)}: {label}"


def failure_lines(data: dict) -> list:
    """The explanation shown when a migration stops, as plain lines."""
    f = data.get("failure")
    if not f:
        return []
    lines = [f["headline"], f"Error: {f['error']}", f["postgres_state"]]
    if f["loaded_then_rolled_back"]:
        lines.append("Copied, then rolled back (not saved): " + ", ".join(f["loaded_then_rolled_back"]))
    if f["failed_table"]:
        lines.append(f"Failed on: {f['failed_table']}")
    if f["not_started"]:
        lines.append("Never started: " + ", ".join(f["not_started"]))
    if f["saved"]:
        lines.append("Saved: " + ", ".join(f["saved"]))
    lines.append(f["next_step"])
    return lines


def procedure_rows(data: dict) -> list:
    """[(icon, name, description)] for the stored procedures of an uploaded script."""
    out = []
    for p in data.get("procedures", []):
        status = p.get("status", "")
        icon = "✅" if status == "OK" else "🛠️" if status == "NEEDS MANUAL WORK" else "⚠️" if status else "🔄"
        detail = status or "translating…"
        if "attempts" in p:
            detail += f" ({p['attempts']} attempt{'s' if p['attempts'] != 1 else ''})"
        if p.get("installed"):
            detail += " - installed in PostgreSQL"
        out.append((icon, p["name"].lower(), detail))
    return out


def decode_upload(raw: bytes):
    """(text, note). Oracle scripts are often Windows-1252 or UTF-8 with a BOM; accept both."""
    try:
        return raw.decode("utf-8-sig"), ""
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "The file is not valid UTF-8, so it was read as Windows-1252."
