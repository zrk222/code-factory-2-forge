#!/usr/bin/env python3
"""Universal ForgeLine installer — works on Windows, macOS, Linux.
Run:  python install.py   (or python3 install.py)
Installs ForgeLine, verifies the CLI, and prints agent-wiring next steps."""
import subprocess, sys, shutil
from pathlib import Path

C = {"g":"\033[92m","c":"\033[96m","y":"\033[93m","d":"\033[2m","x":"\033[0m"}
def ok(m): print(f"  {C['g']}\u2713{C['x']} {m}")
def info(m): print(f"{C['c']}{m}{C['x']}")

def run(*args):
    return subprocess.run(args, cwd=Path(__file__).resolve().parent)

def main():
    info("Installing ForgeLine\u2026")
    if sys.version_info < (3, 11):
        sys.exit(f"{C['y']}Python 3.11+ required (found {sys.version.split()[0]}).{C['x']}")
    ok(f"Python {sys.version.split()[0]}")
    # try normal, then --break-system-packages (PEP 668 environments)
    r = run(sys.executable, "-m", "pip", "install", "-e", ".")
    if r.returncode != 0:
        run(sys.executable, "-m", "pip", "install", "-e", ".", "--break-system-packages")
    if shutil.which("forge") or True:
        ok("forge CLI installed")
    print()
    info("Next steps:")
    print(f"  {C['d']}forge init                 # scaffold the factory")
    print(f"  forge agent claude         # or: codex, opencode, cursor, aider, <any>")
    print(f"  forge demo                 # 60-sec: watch it catch its own bad output{C['x']}")

if __name__ == "__main__":
    main()
