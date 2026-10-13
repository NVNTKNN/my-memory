---
description: Session-end review of the memory wiki — extracts facts worth keeping from the conversation, checks them against existing pages, and proposes new pages or updates.
---

# my-memory:wrap

Session-end memory review. Invoked as:
`/my-memory:wrap`

Reviews the current conversation, picks out what is worth keeping, checks it against existing pages, and proposes new pages or updates. Nothing is written until the user confirms.

**On invocation: immediately begin the review. Do not ask what to save — decide what's worth keeping, then confirm with the user.**

## Steps

### 1. Read the wiki

Read the wiki path from `~/.claude/memory_index/vault.txt`. Read `{wiki}/MEMORY.md` (tag legend + "In flight now") and list the existing pages with their descriptions:

```bash
grep -rH -m1 "^description:" "$(cat ~/.claude/memory_index/vault.txt)" --include="*.md" | cut -c1-220
```

### 1a. Check log.md size (before any writes)

```bash
"$(cat ~/.claude/memory_index/python.txt)" -X utf8 \
  "$HOME/.claude/memory_index/archive_log.py" --check
```

If it reports `log.md` is over the limit, add a line to the step-4 summary: `ARCHIVE: {N} oldest log entries → log-archive.md`. On confirmation, run the same command with `--apply` as the FIRST write in step 5. It backs up `log.md` and moves the oldest entries; nothing is lost.

### 2. Review the session

Memory checks may already have saved facts from earlier in this session. Look for lines like `Saving to memory: …` and `Memory check:` lines in `log.md`. Those facts already have pages. Update them only if something changed since, and don't propose them again.

Look for:
- **Decisions made** — choices, plans, calls the user made
- **Surprises** — things that weren't obvious and could trip up a future session
- **Preferences and corrections** — what the user liked, disliked, or corrected
- **Reference facts** — names, numbers, links, locations that took effort to find
- **State changes** — something finished, started, got blocked, got a date

Skip:
- Step-by-step details of the task in progress (stale tomorrow)
- Anything already saved, or easy to look up again
- Anything the user said not to keep

### 3. Match against existing pages

For each item:
- **Same topic already has a page** → propose an **update** to it
- **Related page exists** → propose a **new page** that links to it (`See also: [[slug]]`)
- **Nothing related** → propose a **new page**

### 4. Present a short summary and wait

```
Session wrap — here's what I'd save:

NEW pages (2):
  feedback_short_answers.md     — prefers bullet points and short answers over long paragraphs
  project_kitchen_renovation.md — contractor booked for 12 Nov; budget capped at ₹4L

UPDATE pages (1):
  project_thesis.md             — chapter 3 draft done; next is the methods review

SKIP:
  - the commands run while debugging (one-off)

Save all? Or say "nothing", or tell me what to skip or change.
```

### 5. Write on confirmation

**New page** — pick exactly one top-level tag from `## Closed top-level tag legend` in MEMORY.md. If a folder named after that tag exists (`{wiki}/{tag}/`), write there; otherwise write to the wiki root.

```markdown
---
name: {slug}
description: {one-line, specific summary}
type: {user|project|feedback|reference}
tags:
  - {top-level tag}
  - {optional sub-tags}
updated: {today YYYY-MM-DD}
---
# {Title}

## Current state

{the current truth}

## History

- {today} {what happened}
```

**Pages are current-state-first.** Search shows only the first ~1,400 characters of a page, so the top must be the current truth, not a diary. Never add a dated section at the top of a page.

**Update** — use Edit, not Write. Rewrite the `## Current state` block in place, bump `updated:`, and add a dated bullet under `## History` only if the change needs a paper trail.

The index rebuilds itself after each write.

### 6. MEMORY.md — keep it short

Do not list new pages in MEMORY.md. Touch it only to:
1. **Prune `## In flight now`** — delete any line whose item closed this session. Do this every wrap.
2. **Add an open item** that cuts across areas. Cap 12 lines.

Never rename the `## Closed top-level tag legend` heading.

If a folder has an index page `{tag}/{tag}_moc.md`, add each new page in that folder to it: `- [[slug]] — short gloss`. (Missed ones are added automatically at the next session start.)

### 7. Log and confirm

Append one line per wrap to `{wiki}/log.md`, under 300 characters:
`- {today} Wrap: added [[a]], [[b]]; updated [[c]].`

Then: `Done. {N} new pages, {M} updates.`

## What makes a good page

- **description:** is what search matches on. "Misc notes" is useless. "Prefers bullet points; dislikes long paragraphs" is useful.
- **feedback pages:** lead with the rule, then **Why:** and **How to apply:**.
- **project pages:** lead with the current fact or decision, then **Why:** and **How to apply:**.
- **reference pages:** exact values, not approximations.
