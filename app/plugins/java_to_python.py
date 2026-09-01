import sys
import os

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

    def convert(self, content: str) -> str:
        prompt = (
            "Convert the following legacy Java code to modern, idiomatic Python. "
            "Return ONLY the Python code, no explanation, no markdown fences.\n\n"
            f"Java code:\n{content}"
        )
        raw_output = ask_ollama(prompt)
        return clean_code_output(raw_output)