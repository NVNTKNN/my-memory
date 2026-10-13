"""my-memory setup — Windows and macOS.

Usage:
    Windows:  py -3.11 setup.py "C:\\Users\\you\\Memory"
    macOS:    python3.12 setup.py ~/Documents/Memory

Idempotent: safe to run again. Everything stays on this computer; the only
network use is the one-time download of Python packages and the embedding model.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

WINDOWS = os.name == "nt"
PLUGIN_DIR = Path(__file__).resolve().parent
SCRIPTS = PLUGIN_DIR / "scripts"
HOME = Path.home()
INDEX_DIR = HOME / ".claude" / "memory_index"
CHROMA_DIR = HOME / ".claude" / "memory_chroma"
LOG_DIR = INDEX_DIR / "logs"
SETTINGS = HOME / ".claude" / "settings.json"
VENV = INDEX_DIR / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")
COPIED = ["build.py", "build_wrapper.py", "search.py", "lint_vault.py",
          "archive_log.py", "reconcile.py", "checkpoint_hook.py"]
# Script names whose hooks this setup owns. Re-running setup replaces them.
OWNED = ("search.py", "lint_vault.py", "build_wrapper.py", "build.py",
         "reconcile.py", "checkpoint_hook.py")


def step(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def check_python() -> None:
    v = sys.version_info[:2]
    # chroma-hnswlib 0.7.6 ships Windows wheels only up to Python 3.11, and
    # requirements-windows.txt is resolved and wheel-checked for 3.11 exactly.
    ok = v == (3, 11) if WINDOWS else (3, 10) <= v <= (3, 12)
    if not ok:
        need = "3.11 (install it with: winget install Python.Python.3.11)" if WINDOWS else "3.10 to 3.12"
        sys.exit(f"ERROR: this is Python {v[0]}.{v[1]}; setup needs Python {need}.")


def arg(p: Path) -> str:
    """A path for a hook command that parses the same in Git Bash, PowerShell
    and zsh: forward slashes, quoted only when it contains a space."""
    s = p.as_posix()
    return f'"{s}"' if " " in s else s


def py_cmd(script: str, *extra: str) -> str:
    return " ".join([arg(VENV_PY), "-X", "utf8", arg(INDEX_DIR / script), *extra])


def rule_path(posix: str) -> str:
    """An absolute path in Claude Code permission-rule form: a leading `//`,
    and on Windows the drive as a lowercase segment (C:/Users -> //c/Users)."""
    m = re.match(r"^([A-Za-z]):/", posix)
    if m:
        posix = "/" + m.group(1).lower() + posix[2:]
    return "/" + posix


def load_settings() -> dict:
    if not SETTINGS.is_file():
        return {}
    text = SETTINGS.read_text(encoding="utf-8-sig").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: {SETTINGS} is not valid JSON ({e}). Fix it, then run setup again. "
                 "Setup will not overwrite a file it cannot read.")
    if not isinstance(data, dict):
        sys.exit(f"ERROR: {SETTINGS} is not a JSON object. Fix it, then run setup again.")
    return data


def drop_owned(entries: list) -> list:
    """Remove hook commands this setup owns from a list of hook entries."""
    for entry in entries:
        entry["hooks"] = [h for h in entry.get("hooks", [])
                          if not ("memory_index" in h.get("command", "")
                                  and any(n in h.get("command", "") for n in OWNED))]
    return [e for e in entries if e.get("hooks")]


def patch_settings(vault: Path) -> None:
    settings = load_settings()
    if SETTINGS.is_file():
        backup = SETTINGS.with_name("settings.json.bak-my-memory")
        shutil.copy2(SETTINGS, backup)
        print(f"  backed up settings to {backup}")

    # Claude Code's built-in auto-memory would write to a different folder.
    settings["autoMemoryEnabled"] = False
    settings.pop("autoMemoryDirectory", None)
    hooks = settings.setdefault("hooks", {})
    build_log = arg(LOG_DIR / "build.log")
    rec_log = arg(LOG_DIR / "reconcile.log")

    def put(event: str, entry: dict) -> None:
        hooks[event] = drop_owned(hooks.get(event, []))
        hooks[event].append(entry)

    # Every prompt: find the most relevant pages and hand them to Claude.
    put("UserPromptSubmit", {"matcher": "", "hooks": [{
        "type": "command", "command": py_cmd("search.py"),
        "timeout": 30, "statusMessage": "Retrieving relevant memory..."}]})
    # Before a wiki write: tidiness rules (exit 2 blocks the write with a reason).
    put("PreToolUse", {"matcher": "Write|Edit", "hooks": [{
        "type": "command", "command": py_cmd("lint_vault.py", "--hook")}]})
    # After a wiki write: reindex in the background.
    put("PostToolUse", {"matcher": "Write|Edit", "hooks": [{
        "type": "command", "command": py_cmd("build_wrapper.py", "--hook", "--log", build_log),
        "async": True}]})
    # Session start: reindex (catches edits made outside Claude), then self-heal.
    hooks["SessionStart"] = drop_owned(hooks.get("SessionStart", []))
    hooks["SessionStart"].append({"matcher": "startup|resume", "hooks": [{
        "type": "command", "command": py_cmd("build_wrapper.py", "--log", build_log),
        "async": True}]})
    # Not async: it reads the index the entry above writes, and must not race it.
    hooks["SessionStart"].append({"matcher": "startup|resume", "hooks": [{
        "type": "command", "command": py_cmd("reconcile.py", "--apply", "--log", rec_log),
        "timeout": 120}]})
    # Each time Claude finishes an answer: after 10 messages or 100 tool calls
    # since the last check, ask Claude whether anything is worth saving.
    put("Stop", {"hooks": [{
        "type": "command", "command": py_cmd("checkpoint_hook.py"), "timeout": 30}]})
    hooks = {k: v for k, v in hooks.items() if v}
    settings["hooks"] = hooks

    # The memory check saves through a background helper that nobody watches,
    # so it must never need a permission prompt: reading and editing inside the
    # wiki only, plus reading vault.txt, which says where the wiki is. An Edit
    # rule also covers new files; a Write(...) rule would be ignored.
    wiki = rule_path(vault.as_posix())
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    for rule in ("Read(~/.claude/memory_index/vault.txt)",
                 f"Read({wiki}/**)", f"Edit({wiki}/**)"):
        if rule not in allow:
            allow.append(rule)

    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS)
    print("  settings.json updated.")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    check_python()
    vault = Path(os.path.expanduser(sys.argv[1])).resolve()
    if not vault.is_dir():
        sys.exit(f"ERROR: wiki folder not found: {vault}")

    step(f"Creating Python environment at {VENV}")
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    if not VENV_PY.is_file():
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])

    step("Installing search libraries (first run: several minutes)")
    req = SCRIPTS / ("requirements-windows.txt" if WINDOWS else "requirements.txt")
    subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "-q", "--upgrade", "pip"])
    subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "-q",
                           "--only-binary=:all:", "-r", str(req)])

    step(f"Copying scripts to {INDEX_DIR}")
    for name in COPIED:
        shutil.copy2(SCRIPTS / name, INDEX_DIR / name)
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    step("Recording wiki folder")
    (INDEX_DIR / "vault.txt").write_text(f"{vault}\n", encoding="utf-8")
    # Skills call the index scripts through this interpreter path.
    (INDEX_DIR / "python.txt").write_text(f"{VENV_PY.as_posix()}\n", encoding="utf-8")
    # log.md entries dated on or before this day are exempt from the length rule.
    since = INDEX_DIR / "lint_since.txt"
    if not since.exists():
        since.write_text(f"{datetime.date.today():%Y-%m-%d}\n", encoding="utf-8")

    step(f"Adding hooks to {SETTINGS}")
    patch_settings(vault)

    step("Building the search index (first run downloads an ~80 MB model)")
    env = dict(os.environ, HF_HUB_OFFLINE="0", TRANSFORMERS_OFFLINE="0",
               ANONYMIZED_TELEMETRY="False")
    build = [str(VENV_PY), "-X", "utf8", str(INDEX_DIR / "build_wrapper.py")]
    if subprocess.call(build, env=env) != 0:
        # A rare crash while the AI libraries shut down can follow a complete
        # build. A second run only indexes what is missing, so retry once.
        print("  first build exited with an error; retrying once...")
        subprocess.check_call(build, env=env)

    print()
    print("my-memory installed.")
    print(f"  Wiki:  {vault}")
    print(f"  Index: {CHROMA_DIR}")
    print("  Hooks: memory recall on every prompt, tidy-check on wiki edits,")
    print("         reindex + self-heal at session start,")
    print("         memory check every 10 messages or 100 tool calls")
    print(f"  Logs:  {LOG_DIR}")
    print("  Claude Code's built-in auto-memory is now off (autoMemoryEnabled=false).")
    print()
    print("Start a NEW Claude Code session to switch memory on.")


if __name__ == "__main__":
    main()
