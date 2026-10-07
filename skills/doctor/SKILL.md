---
description: Work the memory wiki's outstanding tidy-up findings with the user. Use when asked to "fix the memory system", "clean up the wiki", "fix broken links", or after a session reports outstanding findings.
---

# my-memory:doctor

Works through the tidy-up findings that need a human decision.

At every session start, `~/.claude/memory_index/reconcile.py --apply` checks the whole wiki. It fixes two mechanical problems on its own (a page missing from its folder index, and a stale "N pages" count). Everything else it writes to a worklist instead of fixing. This skill works that worklist with the user.

**On invocation: read the worklist and present a summary grouped by rule. Do not edit pages until the user picks what to work on.**

## Read the worklist

```bash
cat ~/.claude/memory_index/worklist.md
```

If it is missing or looks old, regenerate it (report only, changes nothing):

```bash
"$(cat ~/.claude/memory_index/python.txt)" -X utf8 ~/.claude/memory_index/reconcile.py --check
```

`worklist.md` shows at most 20 findings per rule. The full list is in `~/.claude/memory_index/worklist.json`.

## How to handle each rule

**`broken-wikilink`** — a `[[link]]` points to a page that doesn't exist. Three possible fixes, and the user picks:
1. *Typo or rename* — retarget the link to the real page (check the filename with `ls` first).
2. *Page never written* — offer to write it. Don't write it unprompted.
3. *Dead idea* — remove the link, keeping the sentence readable.

Work the most-linked missing pages first. Never bulk-fix.

**`moc-only-inbound`** — the only link to a page is its folder index. Often fine. Read the page and suggest a cross-link from a related page only if one genuinely fits.

**`description-missing`** — the `description:` line is under 40 characters, so search barely finds the page. Read the page, propose a specific one-line description, confirm, save.

**`description-bloat`** — over 600 characters. Usually fine. Only raise it if the description just repeats the page's `## Current state`.

**`moc-missing`** — a folder has pages but no `{folder}_moc.md` index. Write one together with the user: one line per page, grouped under short headings.

**`inflight-over-cap`** — `## In flight now` in MEMORY.md has more than 12 lines. Show the list, ask which items have closed, delete those lines.

**`claim-file-outside-vault`** — informational. Don't change the script to silence it; just tell the user.

**`moc-gap`, `stale-page-count`** — normally auto-fixed. If they keep appearing, check `~/.claude/memory_index/logs/reconcile.log` for why.

## Rules

- Never delete a page without the user's OK.
- Show each proposed edit before writing it.
- After any wiki write, append one line (under 300 characters) to `log.md`.
- The wiki has no version history. Copy a file before a risky edit.
- At the end, re-run `reconcile.py --check` and report what remains.
