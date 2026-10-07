#!/usr/bin/env python3
"""Anti-bloat lint for the my-memory wiki.

Rules that prose alone does not enforce:

- MEMORY.md is an index: ≤200 lines, every line ≤150 chars, no dated
  sections, no "Recent additions" changelog creep.
- log.md entries dated after this machine's lint-live date (lint_since.txt,
  stamped by setup.py; fallback GRANDFATHER_DATE) are one-liners ≤300 chars.
- Living pages are current-state-first: no dated `## 20..` section or dated
  `> **20..` blockquote before the first durable section. ERROR on Write
  (new content), WARN on Edit (don't block unrelated fixes to legacy pages).
- priority: critical pages: ≤2000 bytes, first `## ` heading above byte 1500.
- Size warnings (never block): page >20KB, History section >50% of the page.
  These are fixed by `/my-memory:wrap` auto-archiving, not by blocking.

Modes:
  --hook               read Claude Code PreToolUse/PostToolUse JSON on stdin;
                       exit 2 + stderr message on ERROR (blocks / feeds back)
  --file PATH          lint one file, print findings
  --vault [ROOT]       lint the whole vault, print report, exit 0
  --vault-root PATH    override vault root (default: ~/.claude/memory_index/vault.txt)
"""

import argparse
import json
import re
import sys
from collections import namedtuple
from pathlib import Path

ERROR = "ERROR"
WARN = "WARN"

# Grandfather cutoff: log entries dated STRICTLY AFTER the cutoff are linted;
# anything written before the lint existed is exempt. The cutoff is
# PER-MACHINE — setup.py stamps ~/.claude/memory_index/lint_since.txt on its
# first run, so entries written before install never brick a later log.md
# append. Fallback constant: used only if that file is missing.
GRANDFATHER_DATE = "2026-07-06"
LINT_SINCE_FILE = Path.home() / ".claude" / "memory_index" / "lint_since.txt"
_DATE_RE = re.compile(r"20\d\d-\d\d-\d\d")


def _lint_cutoff() -> str:
    try:
        stamped = LINT_SINCE_FILE.read_text().strip()
    except OSError:
        return GRANDFATHER_DATE
    return stamped if _DATE_RE.fullmatch(stamped) else GRANDFATHER_DATE

LOG_ENTRY_MAX_CHARS = 300
LOG_HIGH_ENTRIES = 120  # log.md over this many entries → ERROR; trim via archive_log.py
# Byte ceiling. 120 entries x 300 chars ~= 36KB, so this is the entry rules'
# implied budget with headroom. Needed because the entry COUNT guard alone
# cannot stop a few very long entries from bloating the file.
LOG_MAX_BYTES = 40_000
MEMORY_LINE_MAX_CHARS = 150
MEMORY_MAX_LINES = 200
CRITICAL_MAX_BYTES = 2000
CRITICAL_RULE_MAX_START_BYTE = 1500
PAGE_WARN_BYTES = 20_000
HISTORY_WARN_RATIO = 0.5

DATED_HEADING_RE = re.compile(r"^##+ 20\d\d-\d\d")
DATED_BLOCKQUOTE_RE = re.compile(r"^> \*\*20\d\d-\d\d")
SECTION_RE = re.compile(r"^## ")
HISTORY_SECTION_RE = re.compile(
    r"^## (History|Recent changes|Migration session log|Changelog)\b", re.MULTILINE
)
LOG_ENTRY_RE = re.compile(r"^- (20\d\d-\d\d-\d\d)")

Finding = namedtuple("Finding", "severity rule path message")


def _frontmatter(text):
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---", 4)
    return text[: end + 4] if end != -1 else ""


def lint_memory(text, path="MEMORY.md"):
    findings = []
    lines = text.splitlines()
    if len(lines) > MEMORY_MAX_LINES:
        findings.append(Finding(
            ERROR, "memory-line-count", path,
            f"MEMORY.md has {len(lines)} lines (max {MEMORY_MAX_LINES}; the index "
            f"silently truncates past it). Move detail to pages and link.",
        ))
    for i, line in enumerate(lines, 1):
        if len(line) > MEMORY_LINE_MAX_CHARS:
            findings.append(Finding(
                ERROR, "memory-line-length", path,
                f"MEMORY.md line {i} is {len(line)} chars (max {MEMORY_LINE_MAX_CHARS}). "
                f"Pinned bullets are one line: status + [[link]]; detail lives on the page.",
            ))
    for i, line in enumerate(lines, 1):
        if DATED_HEADING_RE.match(line):
            findings.append(Finding(
                ERROR, "memory-dated-section", path,
                f"MEMORY.md line {i}: dated section '{line.strip()}'. MEMORY.md is an "
                f"INDEX, not a changelog — per-session changes go in log.md only.",
            ))
        if re.match(r"^#+ Recent additions", line):
            findings.append(Finding(
                ERROR, "memory-changelog-creep", path,
                f"MEMORY.md line {i}: 'Recent additions' section. Do not grow a "
                f"changelog here; log.md is the changelog.",
            ))
    return findings


def lint_log(text, path="log.md", cutoff=None, baseline=None,
             max_bytes=LOG_MAX_BYTES):
    """Lint log.md.

    `baseline` is the PRE-EDIT text of the file, when known (hook mode). Entries
    that already existed verbatim in the baseline are PRE-EXISTING: their length
    violations become WARN, not ERROR.

    Why: log.md is append-only. Linting the whole file makes the first violation
    that lands brick every later append — the writer is blocked by errors it did
    not cause and cannot fix without editing someone else's entry, so the only
    way forward is to bypass the hook. Lint the DELTA; report the legacy
    backlog as a non-blocking summary.

    baseline=None (the --vault / --file audit path) keeps every entry an ERROR,
    so a full audit still shows the true backlog.
    """
    findings = []
    cutoff = cutoff or _lint_cutoff()
    known = {e[1] for e in _log_entries(baseline)} if baseline is not None else None
    n_entries = 0
    n_legacy_long = 0
    for entry_date, entry_text, line_no in _log_entries(text):
        n_entries += 1
        if entry_date <= cutoff or len(entry_text) <= LOG_ENTRY_MAX_CHARS:
            continue
        if known is not None and entry_text in known:
            n_legacy_long += 1  # pre-existing: not this write's fault
            continue
        findings.append(Finding(
            ERROR, "log-entry-length", path,
            f"log.md entry at line {line_no} ({entry_date}) is {len(entry_text)} chars "
            f"(max {LOG_ENTRY_MAX_CHARS}). Log entries are one line: what changed + "
            f"[[page links]]. The detail belongs on the page itself.",
        ))
    if n_legacy_long:
        findings.append(Finding(
            WARN, "log-entry-length-legacy", path,
            f"{n_legacy_long} pre-existing log.md entries exceed {LOG_ENTRY_MAX_CHARS} "
            f"chars. Not blocking this write. Run archive_log.py --apply to retire them.",
        ))
    # Whole-file guards: never grandfathered, because the bound is on the file,
    # not on any one entry. Both are cleared by the non-destructive trim
    # (`/my-memory:wrap` or `archive_log.py --apply`), which writes via
    # plain file I/O and so bypasses this hook by design.
    if n_entries > LOG_HIGH_ENTRIES:
        findings.append(Finding(
            ERROR, "log-size", path,
            f"log.md has {n_entries} entries (max {LOG_HIGH_ENTRIES}). Run "
            f"/my-memory:wrap (auto-archives oldest → log-archive.md) or "
            f"archive_log.py --apply before appending.",
        ))
    n_bytes = len(text.encode("utf-8"))
    if n_bytes > max_bytes:
        findings.append(Finding(
            ERROR, "log-bytes", path,
            f"log.md is {n_bytes // 1024} KB (max {max_bytes // 1024} KB). Entry "
            f"COUNT can sit under its limit while entry LENGTH blows the budget — that "
            f"is why this guard measures bytes. Run archive_log.py --apply.",
        ))
    return findings


def _log_entries(text):
    """Yield (date, full_entry_text, start_line) for each `- YYYY-MM-DD ...` entry."""
    current = None
    for i, line in enumerate(text.splitlines(), 1):
        m = LOG_ENTRY_RE.match(line)
        if m:
            if current:
                yield current
            current = (m.group(1), line, i)
        elif current:
            current = (current[0], current[1] + "\n" + line, current[2])
    if current:
        yield current


def lint_page(text, mode="write", path="page.md"):
    findings = []
    fm = _frontmatter(text)
    body = text[len(fm):]
    body_lines = body.splitlines()

    # Dated content before the first durable section (the changelog-at-top anti-pattern).
    dated_at_top = None
    for line in body_lines:
        if SECTION_RE.match(line):
            if DATED_HEADING_RE.match(line):
                dated_at_top = f"first section is dated: '{line.strip()}'"
            break
        if DATED_BLOCKQUOTE_RE.match(line):
            dated_at_top = f"dated blockquote above the first section: '{line.strip()[:60]}'"
            break
    if dated_at_top:
        findings.append(Finding(
            ERROR if mode == "write" else WARN, "page-dated-at-top", path,
            f"{dated_at_top}. Living pages are current-state-first: edit '## Current "
            f"state' in place and append dated notes under '## History' at the bottom.",
        ))

    size = len(text.encode("utf-8"))
    if "priority: critical" in fm:
        if size > CRITICAL_MAX_BYTES:
            findings.append(Finding(
                ERROR, "critical-size", path,
                f"priority:critical page is {size} bytes (max {CRITICAL_MAX_BYTES}; the "
                f"injection window cuts at ~2KB). Trim, or split one trigger per file.",
            ))
        first_heading = re.search(r"^## ", body, re.MULTILINE)
        rule_start = len(fm.encode()) + (
            len(body[: first_heading.start()].encode()) if first_heading else size
        )
        if rule_start > CRITICAL_RULE_MAX_START_BYTE:
            findings.append(Finding(
                ERROR, "critical-rule-below-fold", path,
                f"priority:critical rule body starts at byte {rule_start} (max "
                f"{CRITICAL_RULE_MAX_START_BYTE}). No preamble — the imperative must "
                f"survive the injection cutoff.",
            ))

    if size > PAGE_WARN_BYTES:
        findings.append(Finding(
            WARN, "page-size", path,
            f"page is {size} bytes (>~{PAGE_WARN_BYTES // 1000}KB). If it has a "
            f"large `## History`, move that to a `<page>_archive.md` sibling and "
            f"index the sibling in the folder MOC. A long page with a SMALL "
            f"History is usually fine — splitting it buys nothing.",
        ))
    hist = HISTORY_SECTION_RE.search("\n".join(body_lines) + "\n")
    if hist:
        hist_bytes = len("\n".join(body_lines)[hist.start():].encode("utf-8"))
        if size and hist_bytes / size > HISTORY_WARN_RATIO:
            findings.append(Finding(
                WARN, "history-ratio", path,
                f"History section is {hist_bytes * 100 // size}% of the page. "
                f"Move it to a `<page>_archive.md` sibling (link the parent both "
                f"ways) and index the sibling in the folder MOC.",
            ))
    return findings


def lint_text(filename, text, mode="write", path=None, baseline=None):
    path = path or filename
    name = Path(filename).name
    if not name.endswith(".md"):
        return []
    if name.endswith("_archive.md") or name == "log-archive.md" or name == "schema.md":
        return []
    if name == "MEMORY.md":
        return lint_memory(text, path)
    if name == "log.md":
        return lint_log(text, path, baseline=baseline)
    return lint_page(text, mode=mode, path=path)


def lint_vault(root):
    findings = []
    for p in sorted(Path(root).rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(lint_text(p.name, text, mode="write", path=str(p)))
    return findings


def _vault_root(override=None):
    if override:
        return Path(override).resolve()
    cfg = Path.home() / ".claude" / "memory_index" / "vault.txt"
    try:
        return Path(cfg.read_text().strip()).resolve()
    except OSError:
        return None


def apply_edit(text, tool_input):
    """Apply an Edit tool_input (old_string→new_string) to text in memory.
    Returns the post-edit text, or None if old_string is absent (the Edit
    itself will fail, so there is nothing meaningful to lint)."""
    old = tool_input.get("old_string") or ""
    if not old or old not in text:
        return None
    new = tool_input.get("new_string") or ""
    if tool_input.get("replace_all"):
        return text.replace(old, new)
    return text.replace(old, new, 1)


def _run_hook(vault_root_override=None):
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if not file_path.endswith(".md"):
        return 0
    root = _vault_root(vault_root_override)
    if root is None:
        return 0
    try:
        Path(file_path).resolve().relative_to(root)
    except ValueError:
        return 0

    # Disk holds the PRE-edit state (PreToolUse fires before the write lands).
    # It is the baseline for delta-linting append-only files — see lint_log.
    try:
        baseline = Path(file_path).read_text(encoding="utf-8")
    except OSError:
        baseline = None

    content = tool_input.get("content")
    if content is not None:
        mode = "write"
        text = content
    else:
        # Lint the post-edit text by applying old_string→new_string in memory;
        # fall back to disk only when the payload carries no edit.
        mode = "edit"
        if baseline is None:
            return 0
        text = baseline
        if "old_string" in tool_input:
            text = apply_edit(text, tool_input)
            if text is None:
                return 0  # old_string not found — the Edit will fail on its own

    findings = lint_text(
        Path(file_path).name, text, mode=mode, path=file_path, baseline=baseline
    )
    errors = [f for f in findings if f.severity == ERROR]
    warns = [f for f in findings if f.severity == WARN]
    if errors:
        for f in errors:
            print(f"[my-memory lint] {f.rule}: {f.message}", file=sys.stderr)
        for f in warns:
            print(f"[my-memory lint] {f.rule} (warning): {f.message}", file=sys.stderr)
        print(
            "Fix the content to satisfy the wiki rules above and retry.",
            file=sys.stderr,
        )
        return 2
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hook", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--vault", nargs="?", const="__default__")
    ap.add_argument("--vault-root")
    ap.add_argument("--mode", choices=["write", "edit"], default="write")
    args = ap.parse_args(argv)

    if args.hook:
        return _run_hook(args.vault_root)

    if args.file:
        p = Path(args.file)
        findings = lint_text(p.name, p.read_text(encoding="utf-8"), mode=args.mode, path=str(p))
    elif args.vault:
        root = _vault_root(args.vault_root) if args.vault == "__default__" else Path(args.vault)
        if root is None:
            print("No vault configured (~/.claude/memory_index/vault.txt).", file=sys.stderr)
            return 1
        findings = lint_vault(root)
    else:
        ap.print_help()
        return 1

    for f in findings:
        print(f"{f.severity} {f.rule} {f.path}: {f.message}")
    errors = sum(1 for f in findings if f.severity == ERROR)
    warns = len(findings) - errors
    print(f"\n{errors} error(s), {warns} warning(s)")
    return 0 if args.vault else (2 if errors else 0)


if __name__ == "__main__":
    sys.exit(main())
