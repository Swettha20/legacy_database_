# ai-code-migrator — Project Handoff & Progress

## What this project is

An AI-based legacy code modernization tool. You upload a legacy file (Java, PHP, SQL, etc.), the tool detects what it is, and offers you a plugin that can modernize it — powered by a local AI model (no paid API needed).

The key design goal: **no single fixed "converter."** New language pairs get added as independent plugins, without touching existing code.

---

## Architecture (the big picture)

```
Uploaded file
     │
     ▼
detector.py        → identifies file type by CONTENT (not extension): java / php / sql / unknown
     │
     ▼
registry.py        → looks up which plugins can handle that type
     │
     ▼
plugins/*.py        → the matching plugin's convert() method runs
     │
     ▼
llm_client.py       → plugin calls this to talk to the local AI model (Ollama) and clean up its output
     │
     ▼
Modernized code returned
```

This is the **Strategy design pattern**: one common interface (`ModernizerPlugin`), many interchangeable implementations (one per language pair). Adding a new conversion type later = one new plugin file + one line in the registry. Nothing else changes.

---

## Tech stack

- **Python 3.13** (backend logic)
- **Ollama** running **CodeLlama 7B** locally — free, private, no API key, no internet required after setup
- **requests** — used to call Ollama's local HTTP API (`http://localhost:11434`)
- **python-dotenv** — installed for future use (managing secrets, once/if we add cloud APIs)

### Why local AI instead of a paid API
No cost, and it's a better learning project — you learn to work with model inference directly rather than just calling someone else's endpoint. Trade-off: CodeLlama 7B is less capable than Claude/GPT-4 class models, so output needs defensive cleanup (see `llm_client.py` below) and may need smaller input chunks for larger files later.

---

## Project structure

```
ai-code-migrator/
├── venv/                      (NOT in git — everyone builds their own)
├── app/
│   ├── detector.py            # Content-based file type detector
│   ├── test_detector.py       # Unit tests for the detector
│   ├── plugin_base.py         # Abstract base class — the "contract" every plugin follows
│   ├── registry.py            # Central plugin lookup/filtering
│   ├── test_registry.py       # Tests proving registry filtering actually works
│   ├── llm_client.py          # Shared Ollama API client + output cleanup logic
│   ├── test_plugin.py         # End-to-end test: file → plugin → AI → cleaned output
│   └── plugins/
│       ├── java_to_python.py  # REAL, working: Java → Python via CodeLlama
│       └── php_to_node.py     # STUB ONLY — just echoes input, not yet AI-powered
├── samples/
│   ├── LegacyOrder.java       # Sample legacy Java file (JDBC, inline SQL)
│   ├── legacy_cart.php        # Sample legacy PHP file (mysqli, inline SQL)
│   ├── schema.sql             # Sample MySQL schema
│   └── mystery_file.txt       # PHP content saved as .txt — proves detection isn't extension-based
├── requirements.txt           # pip package list
└── .gitignore
```

---

## Progress so far, phase by phase

### Phase 1 — Environment Setup ✅
- Python 3.13.5, Git 2.51.0, Ollama 0.33.1 all installed and confirmed working
- Virtual environment (`venv`) created — isolates this project's packages
- Ollama's `codellama:7b` model pulled (~3.8GB) and confirmed responding
- `requests` + `python-dotenv` installed

**Windows-specific gotchas hit and fixed** (useful if your friend is also on Windows):
- Use `python`, not `python3`
- PowerShell blocked venv activation by default → fixed with `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`
- `ollama` command not recognized right after install → fixed by fully closing and reopening the terminal (PATH reload)
- Desktop folder wasn't in the expected location (OneDrive redirect) → project lives directly at `C:\Users\maswe\ai-code-migrator` instead

### Phase 2 — Detector Engine ✅
- `detector.py`: reads file content and checks for language "fingerprints" (`<?php`, `public class`/`import java.`, SQL keywords like `CREATE TABLE`)
- Deliberately **rule-based, not AI-based** — detection is a clear-cut classification problem; using an LLM here would be slower and less reliable. Good AI engineering includes knowing when *not* to use AI.
- Proven to work on content, not filenames, via `mystery_file.txt` (real PHP code saved with a `.txt` extension — still correctly detected as PHP)
- Has a full unit test suite (`test_detector.py`)

### Phase 3 — Plugin Interface ✅
- `plugin_base.py`: an **abstract base class** (`ModernizerPlugin`) every plugin must implement — `source_type`, `target_type`, `name`, `convert()`
- Using Python's `ABC` means Python will **refuse to create** an incomplete plugin (missing a required method) — fails fast and loud at creation time instead of silently breaking later
- First real plugin (`JavaToPythonPlugin`) built and proven to fulfill the contract end-to-end

### Phase 4 — Plugin Registry ✅
- `registry.py`: central place that holds all plugins and filters them by `source_type`
- Proven with two plugins (Java→Python, and a PHP→Node.js stub) that filtering actually works — querying for `"sql"` correctly returns an empty list, since no SQL plugin exists yet
- This is what will power the "dropdown" — the UI will just ask the registry "what can handle this file type?" and get back the right options

### Phase 5 — Real AI Wiring ✅ (current state)
- `llm_client.py`: shared function `ask_ollama(prompt)` that POSTs to Ollama's local API, plus `clean_code_output()` which strips `[PYTHON]` tags or markdown fences that CodeLlama sometimes wraps around its answers
- **Important lesson learned:** raw LLM output is rarely clean enough to use directly — always needs a parsing/cleanup step
- `JavaToPythonPlugin.convert()` now calls the real model instead of returning placeholder text
- **Confirmed working end-to-end**: fed it the sample `LegacyOrder.java` (JDBC + raw SQL string concatenation), and it returned a modernized Python class using `mysql.connector` with **parameterized queries** (fixing a SQL-injection-shaped pattern in the original) and proper exception handling

---

## What's NOT done yet (pick up here)

1. **`PhpToNodePlugin` is still a stub** — it just echoes the input back with a comment. Needs the same treatment as `JavaToPythonPlugin`: write a good prompt, call `ask_ollama()`, clean the output.
2. **No SQL plugin yet** — nothing handles schema translation (e.g. MySQL DDL → PostgreSQL DDL) or data migration/ETL yet. This was part of the original project goal (database migration) and hasn't been started.
3. **No chunking strategy for large files** — current approach sends the whole file as one prompt. Fine for small files; will likely need splitting by class/method for bigger real-world legacy files, since CodeLlama 7B has a limited context window and output quality drops on long inputs.
4. **No UI yet** — everything so far is command-line/test-script driven. The plan is a simple **Streamlit** app: upload a file → show detected type → show a dropdown of matching plugins (from the registry) → run conversion → display/download result.
5. **No validation/self-correction loop** — right now we trust whatever the AI returns. A planned future phase: check if the output is syntactically valid Python (e.g. try to parse/compile it) and, if not, feed the error back to the model to retry.

## Suggested next steps, in order

1. Finish `PhpToNodePlugin` for real (mirrors what's already done for Java→Python — good first task to get familiar with the codebase)
2. Build a SQL plugin for schema translation (MySQL DDL → PostgreSQL DDL) — this is the actual "database migration" capability the project needs
3. Add a basic validation step after `convert()` (e.g., try `ast.parse()` on Python output to catch broken syntax)
4. Build the Streamlit UI wiring together detector → registry → chosen plugin
5. Only then: tackle chunking for larger files, once we've actually seen it become a problem with bigger real-world samples

---

## How to get set up (for your friend)

```powershell
git clone <repo-url>
cd ai-code-migrator
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Then install Ollama separately (not a pip package):
1. Download from https://ollama.com/download
2. Install, then in a terminal: `ollama pull codellama:7b`
3. Verify: `ollama run codellama:7b "say hello"`

Once that responds, run the existing tests to confirm everything works on their machine too:
```powershell
python app\test_detector.py
python app\test_registry.py
python app\test_plugin.py
```

If `python` isn't recognized on their machine, they may need to install Python 3.10+ from python.org and make sure "Add to PATH" is checked during install (a Windows-specific gotcha we hit ourselves).
