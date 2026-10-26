---
description: Automatic mid-session memory save, run by a background helper after the memory check finds facts worth keeping. Saves without asking. To save something by hand, use /my-memory:add or /my-memory:wrap.
argument-hint: "session <id>: <facts, one per line>"
---

# my-memory:checkpoint

A background helper starts this after the memory check (`checkpoint_hook.py`) finds something worth keeping. It saves the facts it was handed to the wiki.

**Nobody is watching this helper.** Never ask the user anything and never wait for confirmation. The search index rebuilds itself after each write.

**Shell commands: read-only ones only** (`grep`, `find`, `cat`, `ls`, `head`), with the wiki path typed out in full. Claude Code runs those without asking. Anything else stops at a permission prompt that nobody will answer: `$(...)`, `$VARIABLES`, loops, `>` redirects, or any command that writes or runs a program. Read and change files with the Read, Edit and Write tools.

## Rules

- **Save only what is settled:**
  - a decision the user made or confirmed
  - a surprise shown by what actually happened
  - a correction or a lasting preference
  - a name, number, link or place that took effort to find
  - something finished, started, blocked or given a date
- A suggestion of Claude's counts only once the user took it up, or a result proved it.
- **Drop** guesses, half-finished work, and one-off "just this time" instructions.
- **Write only inside the wiki.** Reading and editing there is pre-approved.
- Never edit `~/.claude/CLAUDE.md`.

## Steps

1. **Wiki path:** Read `~/.claude/memory_index/vault.txt`.

2. **Find where each fact belongs.** See what every page covers, then search for words from each fact:
   ```bash
   grep -r -m1 "^description:" "<wiki path>" --include="*.md"
   grep -rli "<two or three words from the fact>" "<wiki path>" --include="*.md"
   ```
   Read the most likely page or two.

3. **Choose one action per fact:**
   - **skip:** a page already says this.
   - **update:** a page covers the topic. Edit its `## Current state` in place and set `updated:` to today. If the fact replaces an older one, add `- YYYY-MM-DD: was <old>; now <new>` under `## History`, newest first. Never add a dated section at the top of a page.
   - **merge:** several facts belong on one page. Make one edit.
   - **new:** no page fits. Pick exactly one top-level tag from `## Closed top-level tag legend` in `{wiki}/MEMORY.md`. If no tag fits, use `meta`. Name the file `{type}_{slug}.md`: lowercase, words joined by underscores, 2 to 4 words, in plain English or romaji letters even when the note is in Japanese or another language. Write the note itself in the language the user writes in. Wherever you link to a page, use its file name without `.md`: `[[project_goa_trip]]`, never `[[goa_trip]]`. Write it to `{wiki}/{tag}/` if that folder exists, otherwise to `{wiki}/`:
     ```markdown
     ---
     name: {slug}
     description: {one specific, self-contained line; this is what search matches on}
     type: {user|project|feedback|reference}
     tags:
       - {top-level tag}
       - {optional sub-tags}
     updated: {YYYY-MM-DD}
     originSessionId: {the session id from your task}
     ---
     # {Title}

     ## Current state

     {the fact}
     ```
     For a `feedback` or `project` page, add a **Why:** line and a **How to apply:** line after the lead. If `{wiki}/{tag}/{tag}_moc.md` exists, add `- [[{type}_{slug}]] — {gloss of 10 words or fewer}` to it.

4. **Never overwrite the live session's work.** Read a page again right before each Edit, because the main session may have changed it. If an Edit fails because the file changed, Read it and retry once. Never use Write on a page that already exists.

5. **`MEMORY.md`, section `## In flight now`:** if a fact closes an item listed there, delete that line. Add a line only for something new the user is in the middle of. Never rename any `MEMORY.md` heading.

6. **`log.md`:** Read its last few lines, then append one line with Edit, under 300 characters:
   `- {YYYY-MM-DD} Memory check: {what changed} [[file_name]] [[file_name]]`
   If the tidy-check refuses it because `log.md` is too long, skip the log line. The memory check trims the log before its next reminder.

7. **Finish** with one line: `Saved: page_a (new), page_b (updated)`, or `Nothing saved: <why>`.

If the tidy-check rejects a page write, fix the content and retry.
