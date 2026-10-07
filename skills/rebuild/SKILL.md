---
description: Manually rebuild the search index over the memory wiki.
---

# my-memory:rebuild

Manual index rebuild. Invoked as:
`/my-memory:rebuild`

## Steps

1. Run the rebuild (it reads the wiki path from `~/.claude/memory_index/vault.txt` itself):
```bash
"$(cat ~/.claude/memory_index/python.txt)" -X utf8 ~/.claude/memory_index/build_wrapper.py
```

2. Show the result briefly: how many pages were indexed and how many were unchanged.

## When to use

- Search results seem stale or miss a page you know exists.
- After moving or renaming many pages by hand.

## Notes

- You rarely need this. The index rebuilds itself after every wiki edit made through Claude, and at the start of every session (which catches edits made in Obsidian or Finder).
- Unchanged pages are skipped, so a rebuild is quick.
