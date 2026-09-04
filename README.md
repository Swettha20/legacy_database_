[README.md](https://github.com/user-attachments/files/31739220/README.md)
# ai-code-migrator

An AI-powered legacy code modernization tool. Upload a legacy file (Java, PHP, SQL, etc.), the tool detects what it is, and offers a plugin that can modernize it — powered by a **local** AI model (no paid API, no internet required after setup).

The key design goal: **no single fixed "converter."** New language pairs get added as independent plugins, without touching existing code.

---

## How it works

```
Uploaded file
     │
     ▼
detector.py     → identifies file type by CONTENT (not extension): java / php / sql / unknown
     │
     ▼
registry.py     → looks up which plugins can handle that type
     │
     ▼
plugins/*.py    → the matching plugin's convert() method runs
     │
     ▼
llm_client.py   → plugin calls this to talk to the local AI model (Ollama) and clean up its output
     │
     ▼
Modernized code returned
```

This follows the **Strategy design pattern**: one common interface (`ModernizerPlugin`), many interchangeable implementations (one per language pair). Adding a new conversion type later = one new plugin file + one line in the registry.

---

## Tech stack

- **Python 3.13**
- **Ollama** running **CodeLlama 7B** locally — free, private, no API key
- **requests** — calls Ollama's local HTTP API (`http://localhost:11434`)
- **python-dotenv** — reserved for future use (cloud API secrets, if added)

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
│   ├── test_registry.py       # Tests proving registry filtering works
│   ├── llm_client.py          # Shared Ollama API client + output cleanup logic
│   ├── test_plugin.py         # End-to-end test: file → plugin → AI → cleaned output
│   └── plugins/
│       ├── java_to_python.py  # REAL, working: Java → Python via CodeLlama
│       └── php_to_node.py     # STUB ONLY — echoes input, not yet AI-powered
├── samples/
│   ├── LegacyOrder.java       # Sample legacy Java file (JDBC, inline SQL)
│   ├── legacy_cart.php        # Sample legacy PHP file (mysqli, inline SQL)
│   ├── schema.sql             # Sample MySQL schema
│   └── mystery_file.txt       # PHP content saved as .txt — proves detection isn't extension-based
├── requirements.txt
└── .gitignore
```

---

## Getting set up

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
3. Verify it responds: `ollama run codellama:7b "say hello"`

Once that works, run the existing tests to confirm everything works on your machine:

```powershell
python app\test_detector.py
python app\test_registry.py
python app\test_plugin.py
```

> **Windows note:** if `python` isn't recognized, install Python 3.10+ from python.org and make sure "Add to PATH" is checked during install. If PowerShell blocks venv activation, run:
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

---

## Current status

| Phase | Status |
|---|---|
| 1. Environment setup | ✅ Done |
| 2. Detector engine | ✅ Done |
| 3. Plugin interface | ✅ Done |
| 4. Plugin registry | ✅ Done |
| 5. Real AI wiring (Java → Python works end-to-end) | ✅ Done |

---

## Contributing

This project follows the plugin pattern strictly — when adding a new language pair:

1. Create a new file in `app/plugins/`
2. Implement the `ModernizerPlugin` interface from `plugin_base.py`
3. Register it in `registry.py`
4. Add tests

No other files should need to change.

For full architecture notes, lessons learned, and detailed handoff context, see [`PROGRESS.md`](./PROGRESS.md).
