import sys
import os
import ast

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from plugin_base import ModernizerPlugin
from llm_client import ask_ollama, clean_code_output


class JavaToPythonPlugin(ModernizerPlugin):

    @property
    def source_type(self) -> str:
        return "java"

    @property
    def target_type(self) -> str:
        return "python"

    @property
    def name(self) -> str:
        return "Java → Python (basic)"

    def _is_valid_python(self, code: str) -> bool:
        """
        Uses Python's own parser to check syntax validity WITHOUT executing
        the code. Safe: this never runs a single line of the AI's output,
        it only checks whether it's grammatically well-formed Python.
        """
        try:
            ast.parse(code)
            return True
        except SyntaxError:
            return False

    def _build_prompt(self, content: str, retry: bool = False, error: str = "") -> str:
        base = (
            "Convert the following legacy Java code to modern, idiomatic Python. "
            "Return ONLY the Python code, no explanation, no markdown fences.\n\n"
        )
        if retry:
            base += (
                f"IMPORTANT: Your previous attempt had a Python syntax error: {error}\n"
                "Please fix this and return corrected, syntactically valid Python code.\n\n"
            )
        base += f"Java code:\n{content}"
        return base

    def convert(self, content: str) -> str:
        print("  [convert] Calling Ollama (attempt 1)...", flush=True)
        raw_output = ask_ollama(self._build_prompt(content, retry=False))
        cleaned = clean_code_output(raw_output)

        if self._is_valid_python(cleaned):
            print("  [convert] Attempt 1 is valid Python.", flush=True)
            return cleaned

        # Capture the actual error to feed back to the model
        try:
            ast.parse(cleaned)
        except SyntaxError as e:
            error_message = str(e)

        print(f"  [convert] Attempt 1 invalid ({error_message}), retrying...", flush=True)
        raw_retry = ask_ollama(self._build_prompt(content, retry=True, error=error_message))
        cleaned_retry = clean_code_output(raw_retry)

        if self._is_valid_python(cleaned_retry):
            print("  [convert] Retry is valid Python.", flush=True)
            return cleaned_retry

        print("  [convert] Still invalid after retry, returning with warning.", flush=True)
        warning = (
            "# ⚠️ WARNING: This code failed Python syntax validation even after\n"
            "# a retry. It may be incomplete or contain errors. Please review\n"
            "# manually before use.\n\n"
        )
        return warning + cleaned_retry