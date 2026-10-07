---
description: List open to-dos and pending items found in the memory wiki, grouped by area.
argument-hint: "[area]"
---

# my-memory:tasks

Lists open work found in the wiki. Invoked as:
`/my-memory:tasks` or `/my-memory:tasks <area>`

**On invocation: immediately scan and present. Do not ask questions first.**

## Steps

1. Read the wiki path from `~/.claude/memory_index/vault.txt`. Read `{wiki}/MEMORY.md` — its `## In flight now` list and its tag legend (the areas).

2. Read every `project_*.md` page, plus any folder index pages (`*_moc.md`).

3. Pick out task signals:
   - **Blocked:** "blocked on", "waiting for", "waiting on"
   - **Next steps:** "next:", "TODO", "remaining", "to do"
   - **Open questions:** "open question", "undecided", "need to decide"
   - **Dates:** "by {date}", "due", "deadline"

   Skip anything marked done, finished or ✅. If a page says both "blocked" and later "unblocked", trust the later one.

4. Group by the page's top-level tag and show the source page next to each item:

```
Open items in your memory wiki (as of {today}):

## projects
  [ ] Book the electrician before the 12 Nov start  → project_kitchen_renovation
  [ ] Decide between the two chapter-3 outlines  → project_thesis

## work
  [ ] Waiting on feedback from the review panel  → project_q4_review

(N items across M areas — /my-memory:tasks <area> to filter)
```

5. With an area argument, show only that group (loose, case-insensitive match).

6. If nothing is found: "No open items found. Run /my-memory:wrap after finishing work so the wiki stays current."

## Notes

- Never invent tasks — only show what the pages actually say.
- `feedback_*` pages are rules, not tasks; skip them.
