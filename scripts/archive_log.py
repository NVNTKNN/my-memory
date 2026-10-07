#!/usr/bin/env python3
"""Trim log.md into log-archive.md (my-memory plugin).

log.md is the append-only changelog. Nothing else evicts old entries, so
without this script it grows forever. This script is the durable trimmer.

Ratchet: when log.md exceeds LOG_HIGH_ENTRIES *or* LOG_MAX_BYTES, move the
OLDEST-by-date entries into log-archive.md until it is back under both. Below
both it is a no-op. The lint_vault.py `log-size` / `log-bytes` ERRORs fire at
the same marks, so an over-budget log.md blocks further Edit/Write appends (the
forcing function); this script writes via plain file I/O, bypassing that hook,
so it can clear it.

There is also a BYTE trigger, because counting entries alone lets a few very
long entries bloat the file while staying under the count. Count is not a
proxy for size.

Reuses lint_vault._log-entry regex so "what is an entry" matches the linter.

Modes (default --check):
  --check              report the plan, mutate nothing, exit 0
  --apply              back up log.md, then rewrite log.md + log-archive.md
  --vault-root PATH    override vault root (default ~/.claude/memory_index/vault.txt)
  --high N / --low N   override entry-count thresholds
  --max-bytes N        override the byte ceiling

Retains only the newest BACKUP_KEEP log.bak.* snapshots after each --apply.
"""

import argparse
import datetime
import os
import re
import sys
from collections import namedtuple
from pathlib import Path

# Put this script's own directory on the path so `import lint_vault` resolves
# whether we run as scripts/archive_log.py (repo) or the flat-deployed copy in
# ~/.claude/memory_index/, and also when imported as scripts.archive_log (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lint_vault import LOG_ENTRY_RE, LOG_MAX_BYTES, _vault_root  # noqa: E402

LOG_HIGH_ENTRIES = 120
LOG_LOW_ENTRIES = 80
# Byte trims target this % of the ceiling, so a trim leaves headroom for the
# next appends instead of stopping exactly at the blocking threshold.
LOW_BYTES_PCT = 75
# How many log.bak.* snapshots to keep. Every --apply writes one, so without a
# retention rule they accumulate forever. Pruning is done HERE, not by hand:
# a one-off manual cleanup is not a process.
BACKUP_KEEP = 3

POINTER_RE = re.compile(r"archived to \[\[log-archive\]\]")

Result = namedtuple("Result", "changed new_log new_archive moved_count boundary")


def prune_backups(root, keep=BACKUP_KEEP):
    """Delete all but the newest `keep` log.bak.* snapshots. Returns the names
    removed. Safe because every archived entry is written to log-archive.md
    before the backup ages out — verified by the caller, not assumed."""
    baks = sorted(Path(root).glob("log.bak.*"), key=lambda p: p.name)
    doomed = baks[:-keep] if keep > 0 else baks
    removed = []
    for b in doomed:
        b.unlink()
        removed.append(b.name)
    return removed


def _entry_spans(text):
    """List of (date, [lines], orig_index) — one per `- YYYY-MM-DD ...` entry.

    Non-matching lines (continuations, blanks) belong to the preceding entry's
    span, so an entry moves whole. Same entry-boundary rule as lint_vault."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if LOG_ENTRY_RE.match(ln)]
    spans = []
    for j, s in enumerate(starts):
        end = starts[j + 1] if j + 1 < len(starts) else len(lines)
        date = LOG_ENTRY_RE.match(lines[s]).group(1)
        spans.append((date, lines[s:end], j))
    return spans


def _refresh_archive(archive_text, moved_block, boundary, today):
    text = re.sub(r"(?m)^updated:.*$", f"updated: {today}", archive_text, count=1)
    text = re.sub(
        r"(?m)^(name: log archive \(\d{4}-\d\d-\d\d to )\d{4}-\d\d-\d\d(\))",
        rf"\g<1>{boundary}\g<2>", text, count=1,
    )
    return text.rstrip("\n") + "\n\n" + moved_block + "\n"


def _block_bytes(spans):
    return len("\n".join("\n".join(e[1]) for e in spans).encode("utf-8"))


def archive(log_text, archive_text, high=LOG_HIGH_ENTRIES,
            low=LOG_LOW_ENTRIES, max_bytes=LOG_MAX_BYTES, today=None):
    """Pure planner+builder. Returns Result; performs no file I/O.

    Trims on EITHER dimension: entry count over `high`, or file size over
    `max_bytes`. The byte trigger exists because count alone is not a proxy for
    size: a few very long entries can bloat the file under the count limit.
    """
    today = today or datetime.date.today().isoformat()

    # Drop any existing pointer line(s); a fresh single pointer is written back.
    kept_lines = [ln for ln in log_text.splitlines() if not POINTER_RE.search(ln)]
    spans = _entry_spans("\n".join(kept_lines))

    over_count = len(spans) > high
    # Measure the WHOLE FILE, exactly as lint_vault.lint_log does. Measuring only
    # the entry block here made the trimmer report "no archive needed" on a file
    # the lint was actively blocking — the pointer line sat in the gap.
    over_bytes = len(log_text.encode("utf-8")) > max_bytes
    if not (over_count or over_bytes):
        return Result(False, log_text, archive_text, 0, None)

    by_age = sorted(range(len(spans)), key=lambda k: (spans[k][0], spans[k][2]))
    n_move = len(spans) - low if over_count else 0
    # Byte-driven: retire the oldest until the remainder fits the LOW watermark,
    # not the ceiling. Trimming to exactly max_bytes leaves zero headroom, so the
    # very next append re-trips the lint — the same ratchet-with-no-hysteresis
    # the entry path avoids via high=120 / low=80.
    byte_target = max_bytes * LOW_BYTES_PCT // 100
    while n_move < len(spans) and _block_bytes(
        [spans[k] for k in by_age[n_move:]]
    ) > byte_target:
        n_move += 1
    move_idx = set(by_age[:n_move])

    moved = [spans[k] for k in by_age[:n_move]]           # oldest-first
    kept = [spans[k] for k in range(len(spans)) if k not in move_idx]  # file order
    assert len(kept) + len(moved) == len(spans), "entry lost during archive"

    boundary = max(e[0] for e in moved)
    # The boundary date can SPLIT: same-day entries may remain. Say so — a
    # pointer claiming "through <date>" when that date is still present below
    # reads as a complete cut and hides entries from anyone grepping the archive.
    same_day_kept = sum(1 for e in kept if e[0] == boundary)
    if same_day_kept:
        pointer = (
            f"- (Entries through {boundary} archived to [[log-archive]]; "
            f"{same_day_kept} entries from {boundary} remain below.)"
        )
    else:
        pointer = f"- (Entries through {boundary} archived to [[log-archive]].)"
    kept_block = "\n".join("\n".join(e[1]) for e in kept)
    new_log = pointer + "\n" + kept_block.rstrip("\n") + "\n"

    moved_block = "\n".join("\n".join(e[1]) for e in moved)
    new_archive = _refresh_archive(archive_text, moved_block, boundary, today)
    return Result(True, new_log, new_archive, len(moved), boundary)


_EMPTY_ARCHIVE = (
    "---\nname: log archive (2026-01-01 to 2026-01-01)\ntype: reference\n"
    "tags:\n  - meta\n  - archive\nupdated: 2026-01-01\n---\n\n"
    "# log.md archive\n"
)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Trim log.md into log-archive.md")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--check", action="store_true", help="report only (default)")
    grp.add_argument("--apply", action="store_true", help="perform the trim")
    ap.add_argument("--vault-root")
    ap.add_argument("--high", type=int, default=LOG_HIGH_ENTRIES)
    ap.add_argument("--low", type=int, default=LOG_LOW_ENTRIES)
    ap.add_argument("--max-bytes", type=int, default=LOG_MAX_BYTES)
    args = ap.parse_args(argv)

    root = _vault_root(args.vault_root)
    if root is None:
        print("archive_log: no vault root (set ~/.claude/memory_index/vault.txt)",
              file=sys.stderr)
        return 1
    log_path = Path(root) / "log.md"
    arch_path = Path(root) / "log-archive.md"
    log_text = log_path.read_text(encoding="utf-8")
    archive_text = (arch_path.read_text(encoding="utf-8")
                    if arch_path.exists() else _EMPTY_ARCHIVE)

    res = archive(log_text, archive_text, high=args.high, low=args.low,
                  max_bytes=args.max_bytes)
    n = len(_entry_spans(log_text))
    kb, max_kb = len(log_text.encode("utf-8")) // 1024, args.max_bytes // 1024
    if not res.changed:
        print(f"log.md: {n} entries (≤ {args.high}), {kb} KB (≤ {max_kb} KB) "
              f"— no archive needed.")
        return 0
    if args.apply:
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        (Path(root) / f"log.bak.{stamp}").write_text(log_text, encoding="utf-8")
        log_path.write_text(res.new_log, encoding="utf-8")
        arch_path.write_text(res.new_archive, encoding="utf-8")
        pruned = prune_backups(root)
        msg = (f"Archived {res.moved_count} entries (through {res.boundary}) "
               f"→ log-archive.md; log.md now {n - res.moved_count} entries. "
               f"Backup: log.bak.{stamp}")
        if pruned:
            msg += f" Pruned {len(pruned)} old backup(s), keeping {BACKUP_KEEP}."
        print(msg)
    else:
        why = []
        if n > args.high:
            why.append(f"{n} entries > {args.high}")
        if kb > max_kb:
            why.append(f"{kb} KB > {max_kb} KB")
        print(f"[check] log.md over budget ({'; '.join(why)}); would archive "
              f"{res.moved_count} entries (through {res.boundary}), leaving "
              f"{n - res.moved_count}. Run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
