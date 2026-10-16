"""
legacy-db-modernizer: one-command start

    python start_demo.py

Gets everything running for a demo with no manual steps beforehand:

  1. starts the Oracle and Postgres containers
  2. waits until both genuinely accept connections (Oracle can take a minute)
  3. makes sure the AI is ready: starts Ollama if that is your provider, or
     sends one tiny prompt to Groq to prove the key works
  4. starts the API and the web page, and opens the page in your browser

Anything that is already running is reused, not started twice. Press Ctrl+C
to stop what this script started (the database containers are left running).

    python start_demo.py --no-browser   # do not open the browser
    python start_demo.py --skip-ai      # do not touch the AI provider
"""

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

API_PORT, UI_PORT = 8000, 8501


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        socket.create_connection((host, port), 0.5).close()
        return True
    except OSError:
        return False


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def wait_until(check, seconds: float, label: str, interval: float = 2.0, say=print) -> bool:
    deadline = time.time() + seconds
    while True:
        if check():
            return True
        if time.time() >= deadline:
            return False
        say(f"      ...{label}")
        time.sleep(interval)


def start_containers(run=subprocess.run):
    try:
        out = run(["docker", "start", "oracle-xe", "pg-target"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"could not run docker ({e}). Is Docker Desktop running?"
    if out.returncode != 0:
        return False, (out.stderr or out.stdout).strip().splitlines()[-1] if (out.stderr or out.stdout).strip() else "docker start failed"
    return True, ""


def oracle_ready() -> bool:
    try:
        from check_setup import connect_oracle
        connect_oracle().close()
        return True
    except Exception:
        return False


def postgres_ready() -> bool:
    try:
        from check_setup import connect_postgres
        connect_postgres().close()
        return True
    except Exception:
        return False


class Launcher:
    """Starts the API and the page as child processes, reusing any already running."""

    def __init__(self, api_cmd, ui_cmd, api_port=API_PORT, ui_port=UI_PORT, logdir="logs", say=print):
        self.cmds = {"API": (api_cmd, api_port, f"http://127.0.0.1:{api_port}/"),
                     "page": (ui_cmd, ui_port, f"http://127.0.0.1:{ui_port}/")}
        self.logdir, self.say, self.children = logdir, say, []

    def _tail(self, path: str, lines: int = 8) -> str:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-lines:]).strip()
        except OSError:
            return "(no log)"

    def start(self, name: str, seconds: float = 60) -> tuple:
        cmd, port, url = self.cmds[name]
        if port_open(port):
            self.say(f"   {name} is already running on port {port} - reusing it")
            return True, ""
        os.makedirs(self.logdir, exist_ok=True)
        log_path = os.path.join(self.logdir, f"{name.lower()}.log")
        log = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        self.children.append((name, proc, log))
        ready = lambda: proc.poll() is None and port_open(port)
        if wait_until(lambda: ready() or proc.poll() is not None, seconds, f"waiting for the {name}", interval=0.5, say=lambda m: None):
            if proc.poll() is None:
                return True, ""
            return False, f"the {name} exited immediately:\n{self._tail(log_path)}"
        return False, f"the {name} did not start within {seconds:.0f}s:\n{self._tail(log_path)}"

    def stop(self):
        for name, proc, log in reversed(self.children):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            log.close()
            self.say(f"   stopped the {name}")
        self.children = []


def ensure_ai(skip: bool, say=print) -> bool:
    if skip:
        say("   skipped by request")
        return True
    from check_setup import check_ai
    from llm_provider import _settings
    if _settings()["provider"] == "ollama" and not http_ok("http://localhost:11434/"):
        say("   Ollama is not running - starting it")
        try:
            subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            say("   ✗ could not start Ollama (is it installed?). Start it yourself, or set LLM_PROVIDER=groq in .env")
            return False
        if not wait_until(lambda: http_ok("http://localhost:11434/"), 30, "waiting for Ollama", say=say):
            say("   ✗ Ollama did not come up within 30s")
            return False
    result = check_ai()
    say(f"   {'✓' if result.status == 'PASS' else '✗'} {result.title}" + (f" - {result.detail}" if result.detail else ""))
    if result.status != "PASS":
        say(f"     -> {result.hint}")
    return result.status == "PASS"


def main(argv=None, launcher=None) -> int:
    parser = argparse.ArgumentParser(description="Start everything for a demo, in one command.")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument("--wait", type=float, default=180, help="seconds to wait for Oracle (default 180)")
    args = parser.parse_args(argv)

    from env_check import check_or_explain
    if not check_or_explain():
        return 1

    from check_setup import check_config
    print("Starting the demo (Ctrl+C stops what this script starts):\n")

    print("1. Configuration")
    cfg = check_config()
    print(f"   {'✓' if cfg.status == 'PASS' else '✗'} {cfg.title}" + (f" - {cfg.detail}" if cfg.detail else ""))
    if cfg.status != "PASS":
        print(f"     -> {cfg.hint}")
        return 1

    print("2. Database containers")
    ok, why = start_containers()
    if not ok:
        print(f"   ✗ {why}")
        return 1
    print("   ✓ oracle-xe and pg-target are started")

    print("3. Waiting for the databases to accept connections")
    if not wait_until(postgres_ready, 60, "waiting for Postgres"):
        print("   ✗ Postgres did not answer within 60s - check: docker logs pg-target")
        return 1
    print("   ✓ Postgres answers")
    if not wait_until(oracle_ready, args.wait, "waiting for Oracle (a cold start takes a minute or more)", interval=5):
        print(f"   ✗ Oracle did not answer within {args.wait:.0f}s - check: docker logs oracle-xe")
        return 1
    print("   ✓ Oracle answers")

    print("4. AI provider")
    if not ensure_ai(args.skip_ai):
        return 1

    print("5. API and web page")
    launcher = launcher or Launcher([sys.executable, "-m", "uvicorn", "api:app", "--port", str(API_PORT)],
                                    [sys.executable, "-m", "uvicorn", "web_server:app",
                                     "--host", "127.0.0.1", "--port", str(UI_PORT)])
    try:
        for name in ("API", "page"):
            ok, why = launcher.start(name)
            if not ok:
                print(f"   ✗ {why}")
                launcher.stop()
                return 1
            print(f"   ✓ the {name} is up")
        url = f"http://localhost:{UI_PORT}"
        print(f"\nReady: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        if launcher.children:
            print("Press Ctrl+C here to stop the API and the page.")
            while any(p.poll() is None for _, p, _ in launcher.children):
                time.sleep(1)
            print("A server stopped unexpectedly - see the logs/ folder.")
            return 1
        return 0
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        launcher.stop()


if __name__ == "__main__":
    sys.exit(main())
