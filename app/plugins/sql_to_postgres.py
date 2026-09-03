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
        """
        SQL DDL has a very recognizable shape - it starts with a statement
        keyword (CREATE/ALTER/etc.) and ends with a semicolon. We use that
        shape to strip away any prose the model added before/after, even if
        it didn't use any markdown or [TAG] wrapper we could detect generically.
        """
        # Find the first SQL statement keyword, case-insensitive
        match = re.search(r'\b(CREATE|ALTER|DROP)\s+TABLE\b', text, re.IGNORECASE)
        if not match:
            # No recognizable SQL found - fall back to returning as-is
            return text.strip()

        start = match.start()

        # Find the LAST semicolon in the text - that's the end of the SQL block
        last_semicolon = text.rfind(";")
        if last_semicolon == -1 or last_semicolon < start:
            # No semicolon found after our start point - just return from start onward
            return text[start:].strip()

        return text[start:last_semicolon + 1].strip()

    def convert(self, content: str) -> str:
        prompt = (
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
            "Return ONLY the PostgreSQL DDL code, no explanation, no markdown fences.\n\n"
            f"MySQL schema:\n{content}"
        )
        raw_output = ask_ollama(prompt)
        cleaned = clean_code_output(raw_output)
        return self._extract_sql_only(cleaned)