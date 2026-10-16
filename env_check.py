"""
legacy-db-modernizer: is the right Python being used?

Most "ModuleNotFoundError: No module named 'oracledb'" errors mean one thing:
the project's virtual environment is not active in this terminal window, so
Python is the system one, without the project's packages. Say that in plain
words, with the exact commands, instead of a traceback.
"""

import importlib.util
import ntpath
import os
import sys

# module name -> the name to pip install
REQUIRED = {"oracledb": "oracledb", "psycopg2": "psycopg2-binary", "dotenv": "python-dotenv", "requests": "requests"}


def missing_packages(required: dict = None, find=importlib.util.find_spec) -> list:
    return [pip_name for module, pip_name in (required or REQUIRED).items() if find(module) is None]


def advice(missing: list, python: str = None, here: str = None) -> str:
    python = python or sys.executable
    here = here or os.path.dirname(os.path.abspath(__file__))
    venv_python = ntpath.join(here, "venv", "Scripts", "python.exe")      # the venv layout on Windows
    return (
        f"Python is missing packages this project needs: {', '.join(missing)}\n"
        f"(this window is using: {python})\n\n"
        "This almost always means the project's virtual environment is not active in THIS window.\n"
        "Activate it, then run the same command again - the prompt should start with (venv):\n"
        "    PowerShell (prompt starts with PS):   .\\venv\\Scripts\\Activate.ps1\n"
        "    Command Prompt (prompt ends with >):  venv\\Scripts\\activate\n"
        f"Or skip activating and run the project's own Python directly:\n    {venv_python} <the script>\n\n"
        "If the venv folder does not exist yet:  python -m venv venv   then   pip install -r requirements.txt"
    )


def check_or_explain(say=print) -> bool:
    """True if everything is installed; otherwise prints what to do and returns False."""
    missing = missing_packages()
    if missing:
        say(advice(missing))
        return False
    return True
