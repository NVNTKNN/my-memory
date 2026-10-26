#!/usr/bin/env python3
"""Whole-vault reconciler for the my-memory wiki.

lint_vault.py is an admission controller: it runs on PreToolUse and rejects
bad NEW writes. It can never see defects that already exist, because it only
ever receives the single file being written. Every defect this script finds
predates the check that would have caught it.

Repair is graded by risk:
  - mechanical (exactly one correct answer, no semantic risk) -> auto-fixed
  - judgment (more than one valid resolution)                 -> worklist

Modes:
  --check      report only: never touches vault pages or CLAUDE.md, always
               exits 0. It still writes the worklist (tool state under
               ~/.claude/memory_index/, never user content). Wins over
               --apply if both are passed.
  --apply      apply the mechanical auto-fixes (moc-gap, stale-page-count);
               everything else is still report only. Default (no flags) is
               also report only.

main() never raises: it is wired into SessionStart, and a hygiene tool that
blocks a session is worse than the defects it finds.
"""

import argparse
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt
import json
import os
import re
import sys
from collections import Counter, namedtuple
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

# Put this script's own directory on the path so `import lint_vault` resolves
# whether we run as scripts/reconcile.py (repo), as the flat-deployed copy in
# ~/.claude/memory_index/ (setup.py copies the scripts there WITHOUT the
# `scripts/` package, so a `from scripts.lint_vault import ...` dies with
# ModuleNotFoundError at import time — before main()'s try/except and its
# always-exit-0 contract can engage), and when imported as scripts.reconcile
# (tests). Mirrors archive_log.py / reorganize_folders.py.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lint_vault import ERROR, WARN, Finding, _vault_root  # noqa: E402

INDEX_DIR = Path.home() / ".claude" / "memory_index"
WORKLIST_MD = INDEX_DIR / "worklist.md"
WORKLIST_JSON = INDEX_DIR / "worklist.json"
LOCK_PATH = INDEX_DIR / ".reconcile.lock"

_SEVERITY_ORDER = {ERROR: 0, WARN: 1}

# log.md / log-archive.md are append-only journals, not pages.
SKIP_NAMES = {"log.md", "log-archive.md"}

# Junk dirs build_wrapper.py also skips. Obsidian creates .trash the first
# time a note is deleted, and a trashed copy sorts BEFORE the live page, so
# without this it wins the setdefault below, inflates the page count written
# into CLAUDE.md, and hides broken links to deleted pages.
SKIP_DIRS = {".obsidian", ".trash"}

Page = namedtuple("Page", "stem path folder text")

# [[target]], [[target|label]], [[target#anchor]] -> capture "target" only.
# The closing ]] is REQUIRED and nothing may span a newline: without that, a
# truncated link fragment ("[[some_long_page_na" with no closer, as an
# unsanitized MOC bullet used to produce) swallowed every following line as a
# link target and manufactured a permanent broken-wikilink finding.
LINK_RE = re.compile(r"\[\[([^\]\n|#]+)[^\]\n]*\]\]")

# `[[` is not markup everywhere: it is legal JSON (`"distances": [[1310.2]]` --
# a real OSRM response quoted in this vault), a Python nested list, and LaTeX.
# A page that documents wikilink syntax writes `[[filename]]` in backticks and
# means the literal string, not a link.
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def link_targets(text):
    """Wikilink targets in `text`, ignoring anything inside code.

    Without the strip, the checker invents broken links out of quoted data
    such as JSON arrays and backticked syntax examples."""
    stripped = _INLINE_CODE_RE.sub(" ", _FENCE_RE.sub(" ", text))
    return [t.strip() for t in LINK_RE.findall(stripped)]


def load_pages(vault_root, findings=None):
    """Map page stem -> Page for every content page. Later duplicates lose.

    `findings` is an optional accumulator list. An unreadable or non-UTF-8
    file is skipped (this must never raise), but silently dropping it lowers
    len(pages) by one and check_self_claims would then rewrite CLAUDE.md to
    that wrong number with nothing explaining why — so the skip is recorded
    as a `page-unreadable` finding when a caller supplies the list."""
    pages = {}
    roots = [Path(vault_root)]
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.md")):
            if p.name in SKIP_NAMES:
                continue
            if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                if findings is not None:
                    findings.append(Finding(
                        WARN, "page-unreadable", str(p),
                        f"could not be read ({type(exc).__name__}) — excluded from the "
                        f"page count and from every other check. Fix the file encoding "
                        f"or permissions, or remove it.",
                    ))
                continue
            rel = p.relative_to(root)
            folder = rel.parts[0] if len(rel.parts) > 1 else "(root)"
            pages.setdefault(p.stem, Page(p.stem, p, folder, text))
    return pages


SKILLS_DIR = Path.home() / ".claude" / "skills"


def installed_skill_names(skills_dir=None):
    """Names of installed skills. A page may cross-reference a skill by name
    ([[weekly-report]]) -- a real, resolvable reference, not a dangling
    page link. A directory only counts once it holds a SKILL.md."""
    d = Path(skills_dir) if skills_dir is not None else SKILLS_DIR
    if not d.is_dir():
        return set()
    return {p.name for p in d.iterdir() if (p / "SKILL.md").is_file()}


def resolvable_non_pages(vault_root, skills_dir=None):
    """Stems that RESOLVE a link without being pages of this vault: the
    journals SKIP_NAMES excludes (real files that MEMORY.md links to) and the
    installed skill names. These resolve links and nothing
    else; they are never added to `pages`."""
    root = Path(vault_root)
    extra = {Path(n).stem for n in SKIP_NAMES if (root / n).is_file()}
    return extra | installed_skill_names(skills_dir)


def check_broken_links(pages, extra_targets=()):
    """A [[target]] with no page of that stem. REPORT ONLY — a broken link can
    mean a typo, a rename, or a page that was never written, and only a reader
    can tell which.

    `extra_targets` holds names that RESOLVE a link but are not pages of this
    vault (installed skill names, the log journals). It is a separate
    argument rather than merged into `pages`, because `pages` is both the page
    COUNT check_self_claims writes and the set every auto-fix edits."""
    findings = []
    for page in sorted(pages.values(), key=lambda p: p.stem):
        missing = []
        for target in link_targets(page.text):
            if (not target or target == page.stem
                    or target in pages or target in extra_targets):
                continue
            if target not in missing:
                missing.append(target)
        if missing:
            findings.append(Finding(
                WARN, "broken-wikilink", str(page.path),
                f"{len(missing)} unresolved wikilink(s): "
                + ", ".join(f"[[{t}]]" for t in missing[:8])
                + ("…" if len(missing) > 8 else "")
                + ". Resolve by retargeting, removing, or writing the page.",
            ))
    return findings


BACKUP_KEEP = 3


def backup(path, keep=BACKUP_KEEP):
    """Snapshot a file before modifying it, then prune the newer snapshots to
    `keep`. The OLDEST snapshot of a given file is retained PERMANENTLY.

    stale-page-count re-fires every time the vault gains a page, so a plain
    "keep the newest 3" window discards the pristine pre-reconcile copy after
    four ordinary session starts — and the file it guards (the home CLAUDE.md)
    is not under version control, so the first snapshot is the one to keep.

    Mirrors the archive_log.py convention (`<name>.bak.<stamp>`, keep 3)
    rather than importing it: archive_log's prune globs `log.bak.*`
    specifically, and widening it would change a working script's contract.
    The vault is not under git, so this is the only rollback that exists.
    """
    path = Path(path)
    # Microsecond precision (not archive_log.py's second precision): repeated
    # calls within the same second — as in test_backup_writes_and_prunes,
    # and plausible for reconcile itself if it backs up >1 file per run —
    # must not collide and silently overwrite a prior snapshot.
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    snap = path.with_name(f"{path.stem}.bak.{stamp}")
    snap.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    # Stamps are fixed-width and zero-padded, so lexicographic == chronological.
    baks = sorted(path.parent.glob(f"{path.stem}.bak.*"))
    for doomed in baks[1:-keep] if keep > 0 else baks:
        doomed.unlink()
    return snap


DESC_SNIPPET_MAX = 100


def _first_line_desc(text, fallback):
    """The page's own description, sanitized for use INSIDE a MOC bullet.

    Descriptions often carry a [[wikilink]], so the raw value cannot be
    pasted into a generated bullet:
      - a hard [:100] cut lands mid-link ("[[some_long_page_na") and
        the next run reads that fragment as a link target, manufacturing a
        broken-wikilink finding on the MOC that re-running can never clear;
      - an INTACT [[X]] carried into the MOC adds X to the MOC's `linked` set,
        so a genuine moc-gap for X is suppressed forever.
    Strip the brackets and truncate on a word boundary.
    """
    m = re.search(r"^description:\s*[\"']?(.+?)[\"']?\s*$", text, re.M)
    if not m:
        return fallback
    desc = m.group(1).replace("[", "").replace("]", "").strip()
    if len(desc) <= DESC_SNIPPET_MAX:
        return desc
    return desc[:DESC_SNIPPET_MAX].rsplit(" ", 1)[0].rstrip() + "…"


MOC_ADDED_HEADING = "## Added by reconcile"


def _append_moc_gap_lines(text, lines):
    """Append new MOC bullets under an explicit MOC_ADDED_HEADING section
    instead of the bare file tail (Ruling R5): several real MOCs end in a
    semantically loaded heading (Superseded, People, Related elsewhere,
    History), and a tail append would silently misfile a brand-new live
    page under one of those. Reuse the heading on a later run rather than
    creating a second one."""
    body = text.rstrip("\n")
    if MOC_ADDED_HEADING not in body:
        body += "\n\n" + MOC_ADDED_HEADING + "\n\n" + "\n".join(lines)
    else:
        body += "\n" + "\n".join(lines)
    return body + "\n"


def check_moc_coverage(pages, apply=False):
    """Every folder has a <folder>_moc.md indexing ALL its pages (the wiki's
    'Map-of-content (MOC) indexes'). A page absent from its MOC is a `moc-gap`
    and is AUTO-FIXED. A folder with no MOC at all is `moc-missing` and is only
    REPORTED — a new MOC needs curated structure, not a generated stub."""
    findings = []
    by_folder = {}
    for page in pages.values():
        if page.folder == "(root)":
            continue
        by_folder.setdefault(page.folder, []).append(page)

    for folder, members in sorted(by_folder.items()):
        moc_stem = f"{folder}_moc"
        moc = pages.get(moc_stem)
        if moc is None or moc.folder != folder:
            findings.append(Finding(
                WARN, "moc-missing", folder,
                f"folder '{folder}' has {len(members)} page(s) and no {moc_stem}.md. "
                f"Create one by hand — a generated stub would carry no status text.",
            ))
            continue
        linked = set(link_targets(moc.text))
        gaps = sorted(p for p in members
                      if p.stem != moc_stem and p.stem not in linked)
        if not gaps:
            continue
        findings.append(Finding(
            WARN, "moc-gap", str(moc.path),
            f"{len(gaps)} page(s) missing from {moc_stem}.md: "
            + ", ".join(g.stem for g in gaps[:8])
            + ("…" if len(gaps) > 8 else ""),
        ))
        if apply:
            backup(moc.path)
            lines = [f"- [[{p.stem}]] — {_first_line_desc(p.text, 'added by reconcile')}"
                     for p in gaps]
            body = _append_moc_gap_lines(moc.text, lines)
            moc.path.write_text(body, encoding="utf-8")
    return findings


def claim_files_for(root, include_home):
    """Files whose page-count claims we check. The home CLAUDE.md is included
    ONLY when operating on the configured vault — never when --vault-root
    points somewhere else, or a test against a fixture would rewrite the
    user's real global config."""
    files = [Path(root) / "MEMORY.md"]
    if include_home:
        files.insert(0, Path.home() / ".claude" / "CLAUDE.md")
    return files


# Unanchored: a lone unrelated "<n> pages" (e.g. inside a code block or URL)
# would be treated as the claim if it were the file's only match.
# [ \t]+ not \s+: \s spans newlines, so a claim wrapped as "120\npages" would
# be rewritten to a single reflowed line in the user's CLAUDE.md.
COUNT_RE = re.compile(r"\b(\d{2,5})[ \t]+pages\b")


def check_self_claims(pages, vault_root, claim_files, apply=False, allow_outside=()):
    """A file that states how many pages the vault holds must state the true
    number. Prose counts drift silently, because nothing else compares them
    to reality.

    AUTO-FIX only when exactly one '<n> pages' match exists in the file. Two
    or more and the script cannot know which is the claim — report instead.

    Every claim_files entry must resolve under vault_root, unless it is
    explicitly named in allow_outside (main() passes the home CLAUDE.md
    there, and only when operating on the configured vault). A path that is
    neither gets a skip and a `claim-file-outside-vault` finding, not a
    silent rewrite: the write site enforces the containment rule itself."""
    findings = []
    actual = len(pages)
    vault_root = Path(vault_root).resolve()
    allowed = {Path(p).resolve() for p in allow_outside}
    for path in claim_files:
        path = Path(path)
        resolved = path.resolve()
        if not resolved.is_relative_to(vault_root) and resolved not in allowed:
            findings.append(Finding(
                WARN, "claim-file-outside-vault", str(path),
                f"outside vault_root ({vault_root}) and not allowlisted — "
                f"skipped, not checked.",
            ))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        matches = COUNT_RE.findall(text)
        if not matches:
            continue
        if len(matches) > 1:
            if any(int(m) != actual for m in matches):
                findings.append(Finding(
                    WARN, "stale-page-count", str(path),
                    f"{len(matches)} page-count claims ({', '.join(matches)}) but the "
                    f"vault holds {actual}. ambiguous — not auto-fixed; correct by hand.",
                ))
            continue
        if int(matches[0]) == actual:
            continue
        findings.append(Finding(
            WARN, "stale-page-count", str(path),
            f"claims {matches[0]} pages; vault holds {actual}.",
        ))
        if apply:
            backup(path)
            path.write_text(COUNT_RE.sub(f"{actual} pages", text, count=1), encoding="utf-8")
    return findings


INFLIGHT_CAP = 12
DESC_MIN = 40
DESC_BLOAT = 600

DESC_RE = re.compile(r"^description:\s*(.*?)(?=\n[a-z_]+:|\n---)", re.M | re.S)


def check_inflight_cap(memory_path, cap=INFLIGHT_CAP):
    """'## In flight now' is capped and cross-folder only; a closed item left
    there is a defect. REPORT ONLY — the script cannot know which items closed."""
    memory_path = Path(memory_path)
    try:
        text = memory_path.read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r"^## In flight now.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        return []
    entries = [ln for ln in m.group(1).splitlines() if re.match(r"^[-*]\s", ln.strip())]
    if len(entries) <= cap:
        return []
    return [Finding(
        WARN, "inflight-over-cap", str(memory_path),
        f"{len(entries)} entries against a cap of {cap}. Remove the closed items — "
        f"every line must still be OPEN and cross-folder.",
    )]


def check_moc_only_inbound(pages):
    """A page whose ONLY inbound link is its folder MOC is reachable by
    traversal but is connected to no idea. It is not an orphan and may be
    correctly peripheral, so this is REPORT ONLY — it flags candidates for a
    real cross-link, which is what makes a page findable by association."""
    inbound = {stem: set() for stem in pages}
    for page in pages.values():
        for target in link_targets(page.text):
            if target in inbound and target != page.stem:
                inbound[target].add(page.stem)
    findings = []
    for stem, sources in sorted(inbound.items()):
        if stem.endswith("_moc") or stem == "MEMORY":
            continue
        real = {s for s in sources if not s.endswith("_moc") and s != "MEMORY"}
        if sources and not real:
            findings.append(Finding(
                WARN, "moc-only-inbound", str(pages[stem].path),
                "only inbound link is a MOC — no page references this idea. "
                "Consider a cross-link from a page that actually depends on it.",
            ))
    return findings


def check_descriptions(pages):
    """The description is the single highest-leverage retrieval field: it is
    what ranking sees and what a result list shows. Missing means the page is
    illegible in results (measured: 3 of the top 4 hits on a real query were
    '(no description)'). Bloat dilutes the page-level embedding toward a
    centroid of many topics. Both REPORT ONLY — the fix is a semantic
    rewrite."""
    findings = []
    for page in sorted(pages.values(), key=lambda p: p.stem):
        m = DESC_RE.search(page.text)
        desc = m.group(1).strip().strip("\"'") if m else ""
        if len(desc) < DESC_MIN:
            findings.append(Finding(
                WARN, "description-missing", str(page.path),
                f"description is {len(desc)} chars (min {DESC_MIN}). The page cannot be "
                f"judged from a result list, and ranking has almost nothing to match.",
            ))
        elif len(desc) > DESC_BLOAT:
            findings.append(Finding(
                WARN, "description-bloat", str(page.path),
                f"description is {len(desc)} chars (>{DESC_BLOAT}). It consumes the ~1.4k "
                f"injection window and dilutes the page embedding. Review by hand — some "
                f"long descriptions are deliberate compression, not defects.",
            ))
    return findings


def write_worklist(findings, md_path=None, json_path=None, stats=None):
    """Overwrite the worklist. Newest run wins — this is current state, not a
    log. Grouped by rule, ERROR groups first, so the doctor skill and a human
    reading it see the same order.

    md_path/json_path default to the *current* WORKLIST_MD/WORKLIST_JSON
    module globals, resolved at call time rather than baked in as normal
    Python default-argument values — a default bound at def-time would be
    immune to tests (or callers) monkeypatching those globals."""
    stats = stats or {}
    md_path = Path(md_path) if md_path is not None else Path(WORKLIST_MD)
    json_path = Path(json_path) if json_path is not None else Path(WORKLIST_JSON)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    ordered = sorted(findings, key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.rule, f.path))

    lines = [f"# Memory worklist — {date.today().isoformat()}", ""]
    if stats:
        lines.append("  ".join(f"{k}: {v}" for k, v in sorted(stats.items())))
        lines.append("")
    if not ordered:
        lines.append("No findings. The corpus is clean.")
    else:
        current = None
        for f in ordered:
            if f.rule != current:
                current = f.rule
                count = sum(1 for x in ordered if x.rule == current)
                lines.append(f"## {f.severity} {current} ({count})")
                lines.append("")
                shown = [x for x in ordered if x.rule == current]
                for f2 in shown[:20]:
                    lines.append(f"- `{Path(f2.path).name}` — {f2.message}")
                if len(shown) > 20:
                    lines.append(f"- … and {len(shown) - 20} more (full list in worklist.json)")
        lines.append("")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    json_path.write_text(json.dumps({
        "generated": date.today().isoformat(),
        "stats": stats,
        "findings": [f._asdict() for f in ordered],
    }, indent=2) + "\n", encoding="utf-8")


@contextmanager
def reconcile_lock():
    """Exclusive non-blocking lock so two sessions starting together cannot
    both apply fixes. Mirrors build_wrapper.build_lock(); a separate lock file
    so a reconcile never contends with an index build.

    Yields True if the lock was taken, False if another reconcile already
    holds it.
    """
    Path(LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
    fd = open(LOCK_PATH, "a+")
    try:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        fd.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reconcile.py")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--vault-root")
    ap.add_argument("--log", help="append output to this file (keeps it out of the session)")
    args = ap.parse_args(argv)
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(args.log, "a", encoding="utf-8")

    try:
        root = Path(args.vault_root) if args.vault_root else _vault_root()
        if root is None or not Path(root).is_dir():
            print("No vault configured or vault missing.", file=sys.stderr)
            return 0

        # --check always wins if both flags are passed: an explicit request
        # for a report-only run must never silently apply fixes. With
        # neither flag, apply is also False — the safe default.
        apply = args.apply and not args.check

        with reconcile_lock() as acquired:
            if not acquired:
                print("Another reconcile is running — skipped.", file=sys.stderr)
                return 0

            findings = []
            pages = load_pages(root, findings=findings)
            findings += check_broken_links(
                pages, extra_targets=resolvable_non_pages(root))
            findings += check_moc_coverage(pages, apply=apply)
            # The home CLAUDE.md is only in scope when root came
            # from the configured default -- an explicit --vault-root means
            # "operate on this tree", and reaching outside it is wrong by
            # definition. See claim_files_for's docstring. allow_outside is
            # the matching authorization check_self_claims enforces on
            # itself at the write site.
            include_home = args.vault_root is None
            claim_files = claim_files_for(root, include_home=include_home)
            allow_outside = [Path.home() / ".claude" / "CLAUDE.md"] if include_home else []
            findings += check_self_claims(pages, root, claim_files, apply=apply,
                                           allow_outside=allow_outside)
            findings += check_inflight_cap(Path(root) / "MEMORY.md")
            findings += check_descriptions(pages)
            findings += check_moc_only_inbound(pages)

            stats = {"pages": len(pages), "findings": len(findings)}
            write_worklist(findings, stats=stats)
            # Summary only. stdout is appended to reconcile.log by the
            # SessionStart hook with no rotation, so printing every finding
            # would grow it on each session start. The worklist files carry
            # the detail.
            for rule, n in sorted(Counter(f.rule for f in findings).items()):
                print(f"{n:6d}  {rule}")
            print(f"\n{len(findings)} finding(s) over {len(pages)} pages -> {WORKLIST_MD}")
    except Exception as exc:  # never block a session
        print(f"reconcile: soft-failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
