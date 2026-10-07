---
description: Search the memory wiki on demand — ranked recall of pages mid-session. Use when the auto-injected memory doesn't cover the topic, when asked "what do I know about X" or "did we decide X", or before saying "I don't have context on X".
argument-hint: "<query>"
---

# my-memory:recall

Pull-based memory search. Invoked as:
`/my-memory:recall <query>`

A hook already injects the best-matching pages for each prompt. This skill asks memory a *new* question mid-session, with the same ranking (meaning-based match + keyword match + linked pages + recency).

## Steps

1. Run the search (ranked list, descriptions only):

```bash
"$(cat ~/.claude/memory_index/python.txt)" -X utf8 \
  "$HOME/.claude/memory_index/search.py" --query "{query}" --top 8
```

2. If one or two pages clearly answer the question, fetch their full text (keep `--top` small — full pages are long):

```bash
"$(cat ~/.claude/memory_index/python.txt)" -X utf8 \
  "$HOME/.claude/memory_index/search.py" --query "{query}" --top 2 --full
```

   Or read the page straight from the wiki folder (path in `~/.claude/memory_index/vault.txt`):

```bash
find "$(cat ~/.claude/memory_index/vault.txt)" -name "{page_basename}" -not -path "*/.*"
```

3. Answer from the retrieved pages, citing page names (`[[slug]]`) so the user can open them.

## Notes

- Zero results usually means the index is missing — offer `/my-memory:rebuild`.
- Use this proactively before saying "I don't have context on X".
