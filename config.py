"""
legacy-db-modernizer: centralized configuration
Reads connection details and settings from .env instead of having them
hardcoded in every individual script. Every script that needs to connect
to Oracle, Postgres, or Ollama should import from here instead of
defining its own connection strings.
"""

import os
from dotenv import load_dotenv

load_dotenv()

ORACLE_USER = os.getenv("ORACLE_USER", "system")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD")
ORACLE_DSN = os.getenv("ORACLE_DSN", "localhost:1521/XEPDB1")

# Which Oracle schema (owner) to migrate. Defaults to the connecting user,
# i.e. exactly what the tool always did (SYSTEM for the sample database).
ORACLE_SCHEMA = os.getenv("ORACLE_SCHEMA", ORACLE_USER).strip().upper()

# Optional explicit table list, e.g. ORACLE_TABLES=MEMBERS,BOOKS,LOANS
# Unset/empty = discover every table the schema owns (excluding Oracle's own).
_tables = os.getenv("ORACLE_TABLES", "").strip()
ORACLE_TABLES = [t.strip().upper() for t in _tables.split(",") if t.strip()] or None

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD")
PG_DBNAME = os.getenv("PG_DBNAME", "modernized_db")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "codellama:7b")

if ORACLE_PASSWORD is None:
    raise ValueError("ORACLE_PASSWORD not set - check your .env file exists and is loaded")
if PG_PASSWORD is None:
    raise ValueError("PG_PASSWORD not set - check your .env file exists and is loaded")