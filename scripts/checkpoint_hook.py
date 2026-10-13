#!/usr/bin/env python3
"""Mid-session memory check (my-memory 1.1.0). Registered on Stop.

Stop: count the messages the user has typed, and Claude's tool calls,
since the last check, read from the session transcript starting at a
per-session byte offset. At THRESHOLD messages or TOOL_THRESHOLD tool calls,
whichever comes first, give Claude a reminder before it stops: Claude decides whether
anything is worth keeping and, if so, hands the save to a background helper
running /my-memory:checkpoint. Both counts reset on every reminder, whatever
Claude decides, and on a /my-memory:wrap. Tool calls count because most
sessions are one or two messages followed by long stretches of Claude working.

Before each reminder, trim log.md if it is over budget, so the helper's log
line is never blocked now that nobody has to run /my-memory:wrap.

The reminder is Stop `hookSpecificOutput.additionalContext`: Claude reads it
and the conversation continues. Not `decision: block`, which Claude Code shows
as "Stop hook error" with the whole text; not `systemMessage`, which Claude
never sees.

Soft-fail contract: main() always returns 0 and prints nothing on error, so a
broken memory system never stops Claude from finishing.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

INDEX_DIR = Path.home() / ".claude" / "memory_index"
THRESHOLD = 10
TOOL_THRESHOLD = 100
BOOKMARK_MAX_AGE_S = 30 * 86400
OPT_OUT_ENV = "MY_MEMORY_NO_CHECKPOINT"
WRAP_MARKER = "<command-name>/my-memory:wrap</command-name>"
SAVED_MARKER = "Saving to memory"
# Claude Code forces a visible reply after a hook makes Claude continue
# ("Your previous response had no visible output"), so "say nothing" is not
# possible; a fixed short line keeps the "no" case quiet and predictable.
NOTHING_LINE = "Memory check: nothing new to save."
# Session ids become file names, so anything but a plain id is refused.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

REMINDER = """\
Memory check (session {session}; {n} messages, {k} tool calls since the last \
one). Did anything get settled since then that a future session needs: a \
decision the user made (your suggestion counts only if they took it up), a \
surprise that actually happened, a correction or preference, a name, number or \
link that took effort to find, something finished, started or blocked? Not \
guesses or unfinished work.
No: reply exactly "{nothing}"
Yes: start ONE background Agent (subagent_type "fork" if offered, else \
"general-purpose" with each fact written out in full) with the task "Run \
/my-memory:checkpoint for session {session} to save: <one line per fact>". \
Don't wait for it; reply "{saved}: <topics>"."""


def _text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _is_main_user_entry(entry: dict) -> bool:
    return (entry.get("type") == "user" and not entry.get("isMeta")
            and not entry.get("isSidechain"))


def is_wrap(entry: dict) -> bool:
    return _is_main_user_entry(entry) and WRAP_MARKER in _text(entry)


def is_typed(entry: dict) -> bool:
    """A message the user typed: not a tool result, agent notice, skill
    body, `!` shell input or subagent prompt."""
    if entry.get("type") == "attachment" and not entry.get("isSidechain"):
        # Sent while Claude was mid-answer: stored as a queued_command.
        att = entry.get("attachment") or {}
        origin = att.get("origin")
        return (att.get("type") == "queued_command" and isinstance(origin, dict)
                and origin.get("kind") == "human")
    if not _is_main_user_entry(entry):
        return False
    if "origin" in entry:
        origin = entry["origin"]
        return isinstance(origin, dict) and origin.get("kind") == "human"
    # Older Claude Code builds wrote no `origin` stamp; their non-typed user
    # entries are tool results (no text) or tag-wrapped notices.
    text = _text(entry).strip()
    return bool(text) and not text.startswith("<")


def _tool_calls(entry: dict) -> int:
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return 0
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return 0
    return sum(1 for p in content if isinstance(p, dict) and p.get("type") == "tool_use")


def scan(transcript: str, offset: int):
    """Count typed messages and tool calls after `offset`.
    Returns (count, tools, restarted, headless, end).

    `restarted` means the counts replace the running totals, not add to them:
    a /wrap was seen, or the file shrank below the bookmark. A half-written
    last line is left for the next Stop, so `end` stops at the last newline.
    """
    with open(transcript, "rb") as f:
        restarted = offset > os.fstat(f.fileno()).st_size
        if restarted:
            offset = 0
        f.seek(offset)
        data = f.read()
    cut = data.rfind(b"\n") + 1
    count = tools = 0
    headless = False
    for raw in data[:cut].splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        # `claude -p` and Agent SDK runs stamp entrypoint sdk-*.
        headless = headless or str(entry.get("entrypoint", "")).startswith("sdk")
        if is_wrap(entry):
            count, tools, restarted = 0, 0, True
        elif is_typed(entry):
            count += 1
        else:
            tools += _tool_calls(entry)
    return count, tools, restarted, headless, offset + cut


def _bookmark_path(session: str) -> Path:
    return INDEX_DIR / "checkpoints" / f"{session}.json"


def load_bookmark(session: str) -> dict:
    try:
        bm = json.loads(_bookmark_path(session).read_text(encoding="utf-8"))
        return {"offset": int(bm["offset"]), "count": int(bm["count"]),
                "tools": int(bm.get("tools", 0)), "awaiting": bool(bm.get("awaiting")),
                "headless": bool(bm.get("headless"))}
    except (OSError, ValueError, KeyError, TypeError):
        return {"offset": 0, "count": 0, "tools": 0, "awaiting": False, "headless": False}


def save_bookmark(session: str, bm: dict) -> None:
    path = _bookmark_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(bm), encoding="utf-8")
    os.replace(tmp, path)


def prune_bookmarks() -> None:
    cutoff = time.time() - BOOKMARK_MAX_AGE_S
    for p in (INDEX_DIR / "checkpoints").glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def log(session: str, event: str, **fields) -> None:
    path = INDEX_DIR / "logs" / "checkpoint.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "session": session, "event": event, **fields}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def check(payload: dict):
    """The Stop-hook output: a reminder for Claude, or None to let it stop."""
    if payload.get("hook_event_name") != "Stop":
        return None
    if os.environ.get(OPT_OUT_ENV):
        return None
    if not (INDEX_DIR / "vault.txt").is_file():
        return None
    session = payload.get("session_id") or ""
    transcript = payload.get("transcript_path") or ""
    if not SESSION_ID_RE.match(session) or not os.path.isfile(transcript):
        return None

    bm = load_bookmark(session)
    if bm["awaiting"]:
        said = payload.get("last_assistant_message") or ""
        log(session, "outcome", outcome="saved" if SAVED_MARKER in said else "nothing")

    new, new_tools, restarted, headless, end = scan(transcript, bm["offset"])
    count = new if restarted else bm["count"] + new
    tools = new_tools if restarted else bm["tools"] + new_tools
    # A scripted run reads Claude's last reply as its result; a reminder would
    # replace it. Never remind there.
    headless = headless or bm["headless"]
    due = count >= THRESHOLD or tools >= TOOL_THRESHOLD
    if due and not headless and not payload.get("stop_hook_active"):
        save_bookmark(session, {"offset": end, "count": 0, "tools": 0,
                                "awaiting": True, "headless": False})
        log(session, "reminded", count=count, tools=tools)
        trim_log()
        prune_bookmarks()
        text = REMINDER.format(session=session, n=count, k=tools,
                               saved=SAVED_MARKER, nothing=NOTHING_LINE)
        return {"hookSpecificOutput": {"hookEventName": "Stop",
                                       "additionalContext": text}}
    save_bookmark(session, {"offset": end, "count": count, "tools": tools,
                            "awaiting": False, "headless": headless})
    return None


def trim_log() -> None:
    """Move the oldest log.md entries to log-archive.md if log.md is over
    budget. No-op otherwise. A hook needs no permission, the helper would."""
    subprocess.run(
        [sys.executable, "-X", "utf8", str(INDEX_DIR / "archive_log.py"), "--apply"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=False,
    )


def main(stdin_text=None) -> int:
    try:
        payload = json.loads(sys.stdin.read() if stdin_text is None else stdin_text)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        out = check(payload)
        if out:
            print(json.dumps(out))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
