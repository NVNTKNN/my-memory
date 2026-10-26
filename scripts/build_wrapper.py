"""Wrapper for ~/.claude/memory_index/build.py that handles subfolder vaults.

Reads files recursively from the vault, places them as symlinks into a flat
tmp dir, then invokes build.py against the flat dir via the MY_MEMORY_DIR
env var. After indexing, prunes orphan entries from chroma whose source file
no longer exists in the vault — build.py only UPSERTs and never deletes, so without this step the index slowly accumulates
stale embeddings every time a memory file is deleted or moved out.

Usage:
    python build_wrapper.py [--vault <vault_path>] [--log <file>]
    python build_wrapper.py --hook --log <file>   (PostToolUse: reads the hook
        JSON on stdin and rebuilds only when the edited file is in the vault)

If --vault is omitted, the vault path is read from the plugin config file
~/.claude/memory_index/vault.txt (written by setup.py). This keeps the indexer
decoupled from settings.json `autoMemoryDirectory`, which Claude Code's native
auto-memory feature owns and which collides when both are enabled.
"""
from __future__ import annotations
import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


def try_lock(fd) -> None:
    """Non-blocking exclusive lock; raises OSError if another process holds it."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)


def unlock(fd) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)

SKIP_DIRS = {".obsidian", ".trash"}
# Must mirror build.py's exclusion rule: survivors are "files build would
# index today", or stale entries for newly-excluded files never prune.
EXCLUDE = {"MEMORY.md", "log.md", "schema.md", "log-archive.md"}
EXCLUDE_SUFFIXES = ("_archive.md", "_moc.md")
CHROMA_DIR = os.path.expanduser("~/.claude/memory_chroma")
COLLECTION_NAME = "memory_wiki_ml"  # must match build.py
# Plugin-owned source of truth for the vault path, written by setup.py.
# Decoupled from settings.json `autoMemoryDirectory` (owned by Claude Code's
# native auto-memory feature) to avoid the two features fighting over one key.
VAULT_CONFIG = os.path.expanduser("~/.claude/memory_index/vault.txt")
LOCK_PATH = os.path.expanduser("~/.claude/memory_index/.build.lock")


def read_config_vault() -> "Path | None":
    """Return the vault path from the plugin config file, or None if unset."""
    try:
        with open(VAULT_CONFIG, encoding="utf-8") as f:
            line = f.read().strip()
    except FileNotFoundError:
        return None
    return Path(line) if line else None


def collect_vault_basenames(vault: Path) -> set[str]:
    """Return every .md basename present in the vault (any depth, skipping backup dirs)."""
    names: set[str] = set()
    for src in vault.rglob("*.md"):
        if any(part in SKIP_DIRS for part in src.parts):
            continue
        names.add(src.name)
    return names


def survivor_basenames(vault: Path) -> set[str]:
    """Basenames that must survive orphan pruning: the pages build.py would
    index today."""
    names = collect_vault_basenames(vault)
    return {
        n for n in names
        if n not in EXCLUDE and not n.endswith(EXCLUDE_SUFFIXES)
    }


def symlink_vault_flat(vault: Path, flat: Path) -> None:
    """Symlink every .md from any depth into flat/. Skip duplicates with a warning.

    Windows refuses symlinks without Developer Mode or admin rights, so fall
    back to a copy there. Pages are small, and the flat dir is temporary."""
    for src in vault.rglob("*.md"):
        if any(part in SKIP_DIRS for part in src.parts):
            continue
        dst = flat / src.name
        if dst.exists():
            print(f"WARNING: duplicate basename {src.name} — wikilinks would clash; skipping", file=sys.stderr)
            continue
        try:
            os.symlink(src, dst)
        except OSError:
            shutil.copy2(src, dst)


def prune_orphans(current_basenames: set[str]) -> int:
    """Delete chroma entries whose filename is not in current_basenames.

    Returns the number of entries deleted. Safe to call when chroma collection
    doesn't exist yet — returns 0 in that case.
    """
    try:
        import chromadb
    except ImportError:
        print("WARNING: chromadb not importable; skipping orphan prune", file=sys.stderr)
        return 0
    if not os.path.isdir(CHROMA_DIR):
        return 0
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    if COLLECTION_NAME not in client.list_collections():
        return 0
    col = client.get_collection(COLLECTION_NAME)
    all_docs = col.get()
    orphan_ids: list[str] = []
    for doc_id, meta in zip(all_docs["ids"], all_docs["metadatas"]):
        fname = (meta or {}).get("filename") or doc_id
        if fname not in current_basenames:
            orphan_ids.append(doc_id)
    if orphan_ids:
        col.delete(ids=orphan_ids)
    return len(orphan_ids)


@contextlib.contextmanager
def build_lock():
    """Hold an exclusive, non-blocking lock over the whole index write path.

    SessionStart and PostToolUse both fire this script with `async: true`, so
    two builds can write the same chroma store at once. That race desyncs the hnsw VECTOR segment
    from the sqlite METADATA segment and corrupts the collection; retrieval
    then hangs and floods stderr with `Label not found`. Skipping is the right
    losing behaviour, not queueing: the holder rescans the same vault and picks
    up whatever this run would have indexed.

    Yields True if the lock was taken, False if another build already holds it.
    """
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fd = open(LOCK_PATH, "a+")
    try:
        try_lock(fd)
    except OSError:
        fd.close()
        yield False
        return
    try:
        yield True
    finally:
        unlock(fd)
        fd.close()


def edited_file_in_vault(vault: Path) -> bool:
    """PostToolUse gate: True when the hook JSON on stdin names a file inside
    the vault. Replaces a `jq | grep` shell pipe so the hook runs the same in
    Git Bash, PowerShell and zsh."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return False
    file_path = ((payload or {}).get("tool_input") or {}).get("file_path") or ""
    if not file_path:
        return False
    try:
        Path(file_path).resolve().relative_to(vault.resolve())
    except (ValueError, OSError):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--vault", type=Path, default=None)
    p.add_argument("--hook", action="store_true",
                   help="read PostToolUse JSON on stdin; rebuild only for vault edits")
    p.add_argument("--log", type=Path, default=None,
                   help="append all output to this file instead of the terminal")
    args = p.parse_args(argv)

    if args.log is not None:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        log = open(args.log, "a", encoding="utf-8")
        sys.stdout = sys.stderr = log

    vault = args.vault or read_config_vault()
    if vault is None:
        print(
            f"ERROR: no vault path. Pass --vault or write it to {VAULT_CONFIG} "
            "(run /my-memory:setup).",
            file=sys.stderr,
        )
        return 2

    if args.hook and not edited_file_in_vault(vault):
        return 0

    with build_lock() as acquired:
        if not acquired:
            print("another index build is running — skipping this one.")
            return 0

        home = os.path.expanduser("~")
        current_basenames = survivor_basenames(vault)

        with tempfile.TemporaryDirectory(prefix="memory_index_flat_") as tmp:
            flat = Path(tmp)
            symlink_vault_flat(vault, flat)
            env = os.environ.copy()
            env["MY_MEMORY_DIR"] = str(flat)
            rc = subprocess.call(
                [sys.executable, "-X", "utf8",
                 os.path.join(home, ".claude", "memory_index", "build.py")],
                env=env,
                shell=False,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )

        pruned = prune_orphans(current_basenames)
        if pruned:
            print(f"Pruned {pruned} orphan entries (files no longer in vault).")
        return rc


if __name__ == "__main__":
    raise SystemExit(main())
