"""
legacy-db-modernizer: convert source files, one command

    python convert_file.py samples/LegacyOrder.java
    python convert_file.py legacy_cart.php old_schema.sql
    python convert_file.py "samples\\*.php"

Works on single FILES (not databases). It looks at each file to decide what it
is, picks the matching converter from app/, and writes the result to the
converted/ folder - the original is never changed:

    Java   ->  Python      (checked: the result must be valid Python)
    PHP    ->  Node.js     (checked: no SQL built by pasting strings together)
    SQL    ->  PostgreSQL  (checked: balanced, recognisable statements)

Needs only the AI provider (Groq or Ollama in .env) - no Oracle, Postgres or
Docker. The SQL converter is written for MySQL schemas; for Oracle databases
use the main migration (python start_demo.py) or, for stored procedures in a
script, python plsql_translator.py --file your_script.sql

These converters check the SHAPE of the result, not that it behaves the same as
the original. Read and test what comes out before you rely on it.
"""

import argparse
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "app"))
sys.path.insert(1, os.path.join(HERE, "app", "plugins"))
sys.path.insert(2, HERE)

EXTENSION = {"python": ".py", "node": ".js", "nodejs": ".js", "postgresql": ".sql", "postgres": ".sql"}
SQL_NOTE = ("Note: the SQL converter is written for MySQL schemas. For an Oracle database use the main "
            "migration (python start_demo.py); for Oracle stored procedures in a script use "
            "python plsql_translator.py --file your_script.sql")


def read_text(path: str) -> str:
    """UTF-8 if possible; old files are often Windows-1252, so fall back instead of failing."""
    raw = open(path, "rb").read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        print(f"  note: {os.path.basename(path)} is not valid UTF-8; read as Windows-1252")
        return raw.decode("cp1252", errors="replace")


def expand(patterns: list) -> list:
    """PowerShell does not expand * for programs, so do it here."""
    files = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern]
        files.extend(matches or [pattern])
    return files


def convert_one(path: str, out_dir: str, registry, detect, say=print) -> tuple:
    """(ok, message). Never raises: one bad file must not stop the others."""
    name = os.path.basename(path)
    if not os.path.isfile(path):
        return False, f"{name}: file not found"
    content = read_text(path)
    if not content.strip():
        return False, f"{name}: the file is empty"
    kind = detect(content)
    plugins = registry.get_plugins_for(kind)
    if not plugins:
        supported = sorted({p.source_type for p in registry.all_plugins()})
        return False, (f"{name}: could not tell what kind of file this is (looks '{kind}'). "
                       f"Supported: {', '.join(supported)}")
    plugin = plugins[0]
    say(f"{name}: detected {kind} -> {plugin.name}")
    if kind == "sql":
        say(f"  {SQL_NOTE}")
    try:
        result = plugin.convert(content)
    except Exception as e:
        return False, f"{name}: the conversion failed - {str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__}"
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(name)[0]
    out_path = os.path.join(out_dir, stem + EXTENSION.get(plugin.target_type, ".txt"))
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(result.rstrip("\n") + "\n")
    head = "\n".join(result.lstrip().splitlines()[:3]).upper()
    warned = "WARNING" in head           # the converters start the file with a WARNING comment when their checks fail
    return True, f"{name}: written to {out_path}" + ("  (the checks failed even after a retry - the file starts with a WARNING; review it)" if warned else "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Convert Java, PHP or SQL files with the AI converters.")
    parser.add_argument("files", nargs="+", help="file(s) to convert; * patterns allowed")
    parser.add_argument("--out", default="converted", help="folder for the results (default: converted)")
    args = parser.parse_args(argv)

    from detector import detect_file_type
    from registry import PluginRegistry
    registry = PluginRegistry()

    failures = 0
    for path in expand(args.files):
        ok, message = convert_one(path, args.out, registry, detect_file_type)
        print(("  ✓ " if ok else "  ✗ ") + message)
        failures += 0 if ok else 1
    print()
    print(f"{'All files converted' if not failures else f'{failures} file(s) could not be converted'}. "
          "Results are in the " + args.out + "/ folder; the originals were not changed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
