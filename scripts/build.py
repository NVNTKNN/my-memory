#!/usr/bin/env python3
"""
Incrementally embeds memory .md files into ChromaDB.
Scans the wiki folder (MY_MEMORY_DIR, else ~/.claude/memory_index/vault.txt).
Skips files whose md5 hash matches the stored ChromaDB entry.
"""

import os
import hashlib
import re
import subprocess
import sys
import time

# Force offline model load so a cached model never fires a network HEAD to
# huggingface.co (huggingface_hub does that freshness check even when cached;
# when HF is slow/unreachable it retries >30s and hangs this build). This runs
# async on SessionStart/PostToolUse, so a hang wastes a background process on
# every edit while HF is down. setdefault means the initial setup.py build,
# which sets HF_HUB_OFFLINE=0, can still download the model on a fresh machine.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import chromadb
from sentence_transformers import SentenceTransformer

def _default_memory_dir() -> str:
    try:
        with open(os.path.expanduser("~/.claude/memory_index/vault.txt"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


MEMORY_DIR = os.environ.get("MY_MEMORY_DIR") or _default_memory_dir()
CHROMA_DIR = os.path.expanduser("~/.claude/memory_chroma")
COLLECTION = "memory_wiki"
MODEL_NAME = "all-MiniLM-L6-v2"
EXCLUDE = {"MEMORY.md", "log.md", "schema.md", "log-archive.md"}
# Archived history is superseded content; MOCs are human navigation indexes.
# Both pollute top-5 retrieval if embedded.
EXCLUDE_SUFFIXES = ("_archive.md", "_moc.md")


def indexable(fname: str) -> bool:
    return (
        fname.endswith(".md")
        and fname not in EXCLUDE
        and not fname.endswith(EXCLUDE_SUFFIXES)
    )


SECTION_MIN_CHARS = 120  # sections shorter than this add noise, not recall
_SECTION_HEAD_RE = re.compile(r"^## +(.+?)\s*$")


def _fm_field(content: str, key: str) -> str:
    m = re.search(rf"^{key}:\s*(.+)$", content[:600], re.MULTILINE)
    return m.group(1).strip().strip("'\"") if m else ""


def split_sections(content: str) -> list[tuple[str, str]]:
    """Split a page body into `## ` sections for per-section embeddings.

    The page-level vector only covers the head of the page (MiniLM truncates
    at ~256 tokens), so each section gets its own vector. Returns
    (heading, chunk) pairs; the chunk is prefixed with the page's
    name/description so an ambiguous heading like 'History' still embeds
    with page context. The preamble (frontmatter + text before the first
    `## `) is excluded — the page-level embedding already covers it.
    """
    body = content
    if content.startswith("---\n"):
        end = content.find("\n---", 4)
        if end != -1:
            body = content[end + 4:]
    prefix = " — ".join(
        p for p in (_fm_field(content, "name"), _fm_field(content, "description")) if p
    )

    sections: list[tuple[str, list[str]]] = []
    current = None
    for line in body.splitlines():
        m = _SECTION_HEAD_RE.match(line)
        if m:
            if current:
                sections.append(current)
            current = (m.group(1), [])
        elif current:
            current[1].append(line)
    if current:
        sections.append(current)

    out = []
    for heading, lines in sections:
        text = "\n".join(lines).strip()
        if len(text) < SECTION_MIN_CHARS:
            continue
        chunk = f"## {heading}\n{text}"
        if prefix:
            chunk = f"{prefix}\n{chunk}"
        out.append((heading, chunk))
    return out


def index_sections(collection, model, doc_id: str, basename: str,
                   content_hash: str, sections: list[tuple[str, str]]) -> None:
    """Replace a page's section entries with fresh ones. Section ids are
    `{doc_id}::s{i}`; metadata `parent` ties them back to the page entry."""
    collection.delete(where={"parent": doc_id})
    if not sections:
        return
    embeddings = model.encode([chunk for _, chunk in sections])
    collection.upsert(
        ids=[f"{doc_id}::s{i}" for i in range(len(sections))],
        embeddings=[e.tolist() for e in embeddings],
        documents=[chunk for _, chunk in sections],
        metadatas=[
            {"filename": basename, "hash": content_hash,
             "parent": doc_id, "section": heading}
            for heading, _ in sections
        ],
    )


def ensure_downloaded(path: str) -> None:
    if sys.platform != "darwin":
        return  # brctl is macOS iCloud only
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        subprocess.run(["brctl", "download", path], capture_output=True)
        for _ in range(30):  # poll up to 3 seconds
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return
            time.sleep(0.1)


def file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def collect_md_files() -> list[tuple[str, str]]:
    """Return list of (doc_id, absolute_path) for all indexable .md files."""
    files = []

    # Recursive scan (pages may live in topic subfolders), EXCLUDE list applies. Hidden dirs (.obsidian,
    # .trash, .git) are pruned so only real wiki pages are indexed. doc_id stays
    # the bare filename (names are unique across the vault by convention).
    if os.path.isdir(MEMORY_DIR):
        for root, dirs, fnames in os.walk(MEMORY_DIR):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in fnames:
                if indexable(fname):
                    files.append((fname, os.path.join(root, fname)))

    return files


def main():
    model = SentenceTransformer(MODEL_NAME)
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    collection = client.get_or_create_collection(COLLECTION)

    files = collect_md_files()
    indexed = 0
    skipped = 0

    for doc_id, path in files:
        ensure_downloaded(path)

        try:
            raw_bytes = open(path, "rb").read()
            content = (raw_bytes.decode("utf-8", errors="replace")
                       .replace("\r\n", "\n").strip())
        except OSError as e:
            print(f"  WARN: could not read {path}: {e}")
            continue

        if not content:
            continue

        current_hash = hashlib.md5(raw_bytes).hexdigest()
        sections = split_sections(content)
        existing = collection.get(ids=[doc_id], include=["metadatas"])
        if existing["ids"] and existing["metadatas"][0].get("hash") == current_hash:
            # Migration path: page unchanged but indexed before per-section
            # embeddings existed — backfill its section entries once.
            if sections and not collection.get(ids=[f"{doc_id}::s0"])["ids"]:
                index_sections(collection, model, doc_id,
                               os.path.basename(path), current_hash, sections)
                print(f"  sections backfilled: {doc_id}")
            skipped += 1
            continue

        embedding = model.encode(content).tolist()
        collection.upsert(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[content],
            metadatas=[{"filename": os.path.basename(path), "hash": current_hash, "doc_id": doc_id}],
        )
        index_sections(collection, model, doc_id,
                       os.path.basename(path), current_hash, sections)
        indexed += 1
        print(f"  indexed: {doc_id}")

    print(f"\nDone. {indexed} indexed, {skipped} skipped (unchanged).")


if __name__ == "__main__":
    main()
