import sys
import os
import re

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from plugin_base import ModernizerPlugin
from llm_client import ask_ollama, clean_code_output


class PhpToNodePlugin(ModernizerPlugin):

    @property
    def source_type(self) -> str:
        return "php"

    @property
    def target_type(self) -> str:
        return "node"

    @property
    def name(self) -> str:
        return "PHP → Node.js (basic)"

    def _has_unsafe_query(self, code: str) -> bool:
        pattern = r'\.(query|execute)\(\s*`[^`]*\$\{[^}]*\}[^`]*`'
        return re.search(pattern, code) is not None

    def _build_prompt(self, content: str, retry: bool = False) -> str:
        base = (
            "Convert the following legacy PHP code to modern Node.js.\n\n"
            "STRICT REQUIREMENTS:\n"
            "1. Use the 'mysql2/promise' package with async/await.\n"
            "2. Every SQL query MUST use '?' placeholders with a separate values array. "
            "Pass user input as a parameter array to conn.query(), never interpolate it directly into the SQL string.\n\n"
            "CORRECT example:\n"
            "  await conn.query('INSERT INTO cart (product_id, quantity) VALUES (?, ?)', [productId, qty]);\n\n"
            "WRONG example — do NOT do this, even with backticks/template literals:\n"
            "  await conn.query(`INSERT INTO cart (product_id, quantity) VALUES (${productId}, ${qty})`);\n\n"
        )
        if retry:
            base += (
                "IMPORTANT: Your previous attempt used a template literal with ${...} "
                "interpolated directly into a SQL string passed to .query(). "
                "That is WRONG and unsafe. You MUST fix this: use a plain string with '?' "
                "placeholders as the first argument, and pass the values as a separate array "
                "as the second argument. Do not use backticks or ${} anywhere near a SQL query.\n\n"
            )
        base += (
            "Return ONLY the Node.js code, no explanation, no markdown fences.\n\n"
            f"PHP code:\n{content}"
        )
        return base

    def convert(self, content: str) -> str:
        print("  [convert] Calling Ollama (attempt 1)...", flush=True)
        raw_output = ask_ollama(self._build_prompt(content, retry=False))
        cleaned = clean_code_output(raw_output)

        if not self._has_unsafe_query(cleaned):
            print("  [convert] Attempt 1 looks safe.", flush=True)
            return cleaned

        print("  [convert] Attempt 1 unsafe, retrying...", flush=True)
        raw_output_retry = ask_ollama(self._build_prompt(content, retry=True))
        cleaned_retry = clean_code_output(raw_output_retry)

        if not self._has_unsafe_query(cleaned_retry):
            print("  [convert] Retry looks safe.", flush=True)
            return cleaned_retry

        print("  [convert] Still unsafe after retry, adding warning.", flush=True)
        warning = (
            "// ⚠️ WARNING: The AI model produced a SQL query using string interpolation\n"
            "// instead of parameterized placeholders, even after a retry. This is a\n"
            "// potential SQL-injection risk. Please manually review and fix any\n"
            "// `.query(`...${...}...`)` calls below before using this code.\n\n"
        )
        return warning + cleaned_retry