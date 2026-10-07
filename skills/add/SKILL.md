---
description: Save a single fact to the memory wiki mid-session — drafts a memory page from the description and writes after confirmation.
argument-hint: "<what to remember>"
---

# my-memory:add

On-demand memory add. Invoked as:
`/my-memory:add <what to remember>`

Example: `/my-memory:add I prefer short answers with bullet points, not long paragraphs`

**On invocation: immediately draft the memory page and show it for confirmation. Do not ask clarifying questions first unless the description is completely ambiguous.**

## Steps

1. Read the wiki path from `~/.claude/memory_index/vault.txt` (written by setup). If it is missing, tell the user to run `/my-memory:setup` first.

2. Classify the memory:
   - `user` — about the user: role, preferences, expertise
   - `project` — ongoing work, decisions, goals, blockers, timelines
   - `feedback` — how Claude should behave (do/don't patterns, lessons from mistakes)
   - `reference` — pointers to resources, names, IDs, locations

3. Pick the top-level tag. Read `{wiki}/MEMORY.md` and find the section `## Closed top-level tag legend`. The first column of its table (in backticks) is the closed set of allowed top-level tags. The page gets:
   - **exactly one** tag from that set, and
   - zero or more free-form sub-tags after it.

   If it fits two areas, pick the one a future reader would look under. If none fits, say so and ask — do not invent a top-level tag. The user can add a row to the legend.

4. Draft the page:
```markdown
---
name: {slug}
description: {one-line summary — this is what search matches on}
type: {user|project|feedback|reference}
tags:
  - {top-level tag}
  - {optional sub-tags}
updated: {today YYYY-MM-DD}
---
# {Title}

## Current state

{the fact, decision or rule}
```

   For `feedback` and `project` pages, after the lead line add:
   - **Why:** the reason or the incident behind it
   - **How to apply:** when this should kick in

   If the rule is one whose violation causes silent, costly damage, set `priority: critical`, keep the whole file under 2000 bytes, and offer to add a one-line pointer to it in `~/.claude/CLAUDE.md` so it is always loaded.

5. Filename: `{type}_{slug}.md`, lowercase, words joined by underscores, 2–4 words.

6. Check for an existing page on the same topic:
```bash
grep -rli "{keyword}" "{wiki}" --include="*.md" 2>/dev/null | head -3
```
   If one matches, read it and propose an **update** to it instead of a new page.

7. Show the draft and wait:
```
Here's what I'll add as `{filename}.md`:

[frontmatter and content]

Add it? (or tell me what to change)
```

8. On confirmation, write the file:
   - If a folder named exactly like the top-level tag exists (`{wiki}/{tag}/`), write it there.
   - Otherwise write it to `{wiki}/{filename}.md`.

   If that folder has an index page `{tag}/{tag}_moc.md`, add a line to it: `- [[{slug}]] — {≤10-word gloss}`.

9. Append one line to `{wiki}/log.md`, under 300 characters:
   `- {today} Added [[{slug}]]: {what it says, briefly}.`

10. Confirm: "`{filename}.md` added and indexed." The search index rebuilds itself after the write; no need to run rebuild.

## Notes

- Never overwrite an existing page without showing the change first. Use Edit, not Write, on existing pages.
- The `description` line matters most — search uses it to decide when to surface the page. Make it specific and self-contained.
- Save only things that can't be recovered elsewhere: decisions, preferences, lessons, facts that took effort to find.
