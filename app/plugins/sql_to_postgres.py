import sys
import os
import re

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from plugin_base import ModernizerPlugin
from llm_client import ask_ollama, clean_code_output


class SqlToPostgresPlugin(ModernizerPlugin):

    @property
    def source_type(self) -> str:
        return "sql"

    @property
    def target_type(self) -> str:
        return "postgresql"

    @property
    def name(self) -> str:
        return "MySQL → PostgreSQL Schema Migrator"

    def _extract_sql_only(self, text: str) -> str:
        match = re.search(r'\b(CREATE|ALTER|DROP)\s+TABLE\b', text, re.IGNORECASE)
        if not match:
            return text.strip()
        start = match.start()
        last_semicolon = text.rfind(";")
        if last_semicolon == -1 or last_semicolon < start:
            return text[start:].strip()
        return text[start:last_semicolon + 1].strip()

    def _is_structurally_valid(self, sql: str) -> tuple[bool, str]:
        """
        Not a full SQL parser - just catches the most common breakage:
        missing/mismatched parentheses, or no recognizable statement at all.
        Returns (is_valid, error_description).
        """
        if not re.search(r'\b(CREATE|ALTER|DROP)\s+TABLE\b', sql, re.IGNORECASE):
            return False, "No CREATE/ALTER/DROP TABLE statement found"

        open_count = sql.count("(")
        close_count = sql.count(")")
        if open_count != close_count:
            return False, f"Mismatched parentheses ({open_count} open, {close_count} close)"

        if not sql.rstrip().endswith(";"):
            return False, "Output does not end with a semicolon - may be truncated"

        return True, ""

    def _build_prompt(self, content: str, retry: bool = False, error: str = "") -> str:
        base = (
            "Convert the following MySQL schema (DDL) to valid PostgreSQL DDL.\n\n"
            "REQUIRED TRANSLATIONS:\n"
            "1. Replace AUTO_INCREMENT columns with SERIAL (for INT) or BIGSERIAL (for BIGINT).\n"
            "2. Replace backtick-quoted identifiers (`table_name`) with double-quoted identifiers "
            "(\"table_name\"), or remove quoting entirely if the name is a safe lowercase identifier.\n"
            "3. Remove MySQL-specific clauses that don't exist in PostgreSQL, such as "
            "ENGINE=InnoDB and DEFAULT CHARSET=....\n"
            "4. Replace TINYINT(1) with BOOLEAN.\n"
            "5. Replace DATETIME with TIMESTAMP.\n"
            "6. If a column has 'ON UPDATE CURRENT_TIMESTAMP', remove that clause from the column "
            "definition and instead add a comment above the table noting that a trigger is needed "
            "to replicate this behavior in PostgreSQL (PostgreSQL has no inline equivalent).\n\n"
        )
        if retry:
            base += (
                f"IMPORTANT: Your previous attempt had a structural problem: {error}\n"
                "Please fix this and return complete, well-formed PostgreSQL DDL.\n\n"
            )
        base += (
            "Return ONLY the PostgreSQL DDL code, no explanation, no markdown fences.\n\n"
            f"MySQL schema:\n{content}"
        )
        return base

    def convert(self, content: str) -> str:
        print("  [convert] Calling Ollama (attempt 1)...", flush=True)
        raw_output = ask_ollama(self._build_prompt(content, retry=False))
        cleaned = self._extract_sql_only(clean_code_output(raw_output))

        is_valid, error = self._is_structurally_valid(cleaned)
        if is_valid:
            print("  [convert] Attempt 1 is structurally valid.", flush=True)
            return cleaned

        print(f"  [convert] Attempt 1 invalid ({error}), retrying...", flush=True)
        raw_retry = ask_ollama(self._build_prompt(content, retry=True, error=error))
        cleaned_retry = self._extract_sql_only(clean_code_output(raw_retry))

        is_valid_retry, error_retry = self._is_structurally_valid(cleaned_retry)
        if is_valid_retry:
            print("  [convert] Retry is structurally valid.", flush=True)
            return cleaned_retry

        print("  [convert] Still invalid after retry, returning with warning.", flush=True)
        warning = (
            "-- ⚠️ WARNING: This SQL failed structural validation even after a\n"
            f"-- retry ({error_retry}). It may be incomplete or malformed.\n"
            "-- Please review manually before running against a database.\n\n"
        )
        return warning + cleaned_retry