import os
import sys

# The shared provider layer lives in the project root; app/ is run from
# several places (tests, plugins), so add the root to the path explicitly.
# append (not insert) so nothing in app/ can be shadowed by root modules.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from llm_provider import ask_llm  # noqa: E402


def ask_ollama(prompt: str) -> str:
    """
    Kept under its original name so the plugins (java_to_python,
    php_to_node, sql_to_postgres) work unchanged. It now goes through the
    shared provider layer, so despite the name it uses whichever provider
    LLM_PROVIDER selects in .env (Ollama by default, or Groq).
    """
    return ask_llm(prompt)


def clean_code_output(raw_output: str) -> str:
    """
    Strips common wrapper artifacts CodeLlama adds around code:
    - [PYTHON]...[/PYTHON] style tags
    - Markdown-style ```python fences
    - Any trailing prose/explanation after the code block
    """
    text = raw_output.strip()

    # Strip [PYTHON]/[/PYTHON] or similar [LANG] tags
    for tag_start, tag_end in [("[PYTHON]", "[/PYTHON]"), ("[python]", "[/python]")]:
        if tag_start in text and tag_end in text:
            start = text.index(tag_start) + len(tag_start)
            end = text.index(tag_end)
            text = text[start:end].strip()
            return text

    # Strip markdown code fences like ```python ... ```
    if "```" in text:
        parts = text.split("```")
        # parts[0] = before first fence, parts[1] = code (possibly with a
        # language tag on its own first line), parts[2+] = trailing prose
        if len(parts) >= 2:
            code_block = parts[1]
            # Remove a leading language tag like "python\n"
            lines = code_block.split("\n", 1)
            if len(lines) == 2 and lines[0].strip().isalpha():
                code_block = lines[1]
            return code_block.strip()

    # No known wrapper found - return as-is
    return text


if __name__ == "__main__":
    result = ask_ollama(
        "Write a one-line Python function that multiplies two numbers. "
        "Return ONLY the code, no explanation, no markdown."
    )
    print("--- RAW ---")
    print(result)
    print("\n--- CLEANED ---")
    print(clean_code_output(result))