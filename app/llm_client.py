import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "codellama:7b"


def ask_ollama(prompt: str) -> str:
    """
    Sends a prompt to the local Ollama server and returns the model's
    full text response as a single string.
    """
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL_NAME,
            "prompt": prompt,
            "stream": False,
        },
    )
    response.raise_for_status()
    data = response.json()
    return data["response"]


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