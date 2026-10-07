---
description: First-time setup of the my-memory plugin — creates (or links) the memory wiki folder, writes a starter wiki and profile, installs the search index, and registers the hooks.
---

# my-memory:setup

First-time setup. Safe to run again later: every step skips what already exists.

**On invocation: immediately run the checks in Step 0, then ask the questions in Step 1. Do not wait for the user to say anything else.**

## Steps

### 0. Checks (run before asking anything)

Find a usable Python (Windows needs 3.11 exactly; macOS takes 3.10–3.12):
```bash
for c in "py -3.11" python3.11 python3.12 python3.10; do
  $c -c "import sys; print(sys.version_info[:2])" >/dev/null 2>&1 && { echo "PYTHON: $c"; break; }
done || echo "MISSING: python"
```
- On Windows, if `py -3.11` was not found → stop. Tell the user to run this in PowerShell, then **close and reopen Claude Code** and run `/my-memory:setup` again:
  `winget install Python.Python.3.11`
  (Or download Python 3.11 from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH".) A newer Python such as 3.12 or 3.13 will NOT work on Windows: one search library has no Windows build for it.
- On macOS, if nothing was found → download the official Python 3.12 macOS installer from python.org and `open` it so the user can click through it (or, if they use Homebrew, `brew install python@3.12`). Then use `python3.12`.

Already configured?
```bash
cat "$HOME/.claude/memory_index/vault.txt" 2>/dev/null
```
If this prints a folder, say so and ask: "Keep using this folder, or set up a different one?" If kept, skip to Step 3.

Existing wiki folders (a `MEMORY.md` marks one):
```bash
find "$HOME/Memory" "$HOME/Documents" "$HOME/OneDrive/Documents" -maxdepth 5 -name "MEMORY.md" 2>/dev/null | head -5
```
Ignore permission errors.

### 1. Questions (ask all at once, in one message)

```
A few quick questions to set up your memory wiki:

1. What's your first name?
2. In one line, what do you mainly use Claude for?
3. Where should the wiki live?
   a) {home}/Memory   (default — e.g. C:\Users\{Name}\Memory on Windows)
   b) {existing folder with MEMORY.md}   (only offer if one was found)
   c) another folder (give the path)
4. Pick the top-level areas your notes will be sorted into (every note gets exactly one).
   Default: work, projects, personal, learning. Keep these, or list your own.
```

`meta` (notes about the wiki itself) is always added — don't ask about it.

### 2. Create the starter wiki (skip any file that already exists)

Create the folder if needed. Avoid OneDrive and iCloud folders: files kept online-only there look empty to the indexer. Then write these three files, with `{today}` as YYYY-MM-DD and `{first}` as the lowercased first name.

**`{wiki}/MEMORY.md`** — the start page. Keep every line under 150 characters.
```markdown
---
name: MEMORY
description: Start page of {Name}'s memory wiki — how to find things, the list of top-level tags, and what is in flight right now.
type: reference
updated: {today}
---
# {Name}'s memory — start here

## How to find things

- Claude searches this wiki on every prompt and quietly reads the best matches.
- Ask it directly mid-session: `/my-memory:recall <question>`.
- Save one fact: `/my-memory:add <fact>`. At the end of a session: `/my-memory:wrap`.
- Every change is listed, one line each, in [[log]].

## Closed top-level tag legend

Every page carries exactly one of these tags. Add a row here before using a new one.

| Tag | What goes here |
|---|---|
| `{area}` | {one-line gloss for that area} |
| `meta` | Notes about this wiki itself |

## In flight now

Open items that cut across areas. 12 lines at most. Delete a line when it closes.

- (nothing yet)
```
One table row per area chosen in question 4, then `meta` last. Do not rename the `## Closed top-level tag legend` heading — the add and wrap skills find the tag list by that exact text.

**`{wiki}/log.md`** — the changelog, one line per change:
```markdown
---
name: log
description: One-line changelog of every change made to this memory wiki, oldest first.
type: reference
updated: {today}
---
# Log

- {today} Wiki created by /my-memory:setup: [[MEMORY]], [[user_{first}]].
```

**`{wiki}/user_{first}.md`** — the profile:
```markdown
---
name: user_{first}
description: {Name}'s profile — who {Name} is, what {Name} uses Claude for, and how {Name} likes to work.
type: user
tags:
  - meta
updated: {today}
---
# {Name}

**Uses Claude for:** {answer to question 2}

## How {Name} likes to work

- (add preferences here as they come up)
```

If the folder already had a `MEMORY.md`, leave it alone and only create what is missing.

### 3. Run setup.py

`setup.py` sits at the plugin's root, two folders above this skill. Use the "Base directory for this skill" shown when this skill loaded:
```bash
SETUP_PY="{skill base directory}/../../setup.py"
[ -f "$SETUP_PY" ] || SETUP_PY="$(find "$HOME/my-memory" "$HOME/.claude/plugins" "$HOME/Downloads" "$HOME/Desktop" -maxdepth 6 -name setup.py -path "*my-memory*" 2>/dev/null | head -1)"
echo "$SETUP_PY"
{python found in Step 0} "$SETUP_PY" "{wiki}" 2>&1 | tail -12
```

Run it with a 10-minute timeout (use run_in_background if it may take longer, then wait for it). **Tell the user before starting:** the first run downloads about 1 GB of search libraries and a small language model, so it can take 5–15 minutes. Later runs take seconds.

If it fails, show the full output and stop. Common fixes:
- `settings.json is not valid JSON` → open `~/.claude/settings.json`, fix the syntax error it names, run setup again.
- An error mentioning `c10.dll` or `DLL load failed` (Windows) → install the Microsoft Visual C++ Redistributable (x64) from https://aka.ms/vs/17/release/vc_redist.x64.exe, then run setup again.

### 4. Confirm

```
Setup complete.
  Wiki: {wiki}
  Search index: ~/.claude/memory_index/ and ~/.claude/memory_chroma/

Start a NEW Claude Code session — memory recall switches on there.

Day to day:
  /my-memory:add <fact>        save one thing
  /my-memory:wrap              end of a session: Claude proposes what to save
  /my-memory:recall <question> search the wiki
  /my-memory:tasks             open to-dos found in the wiki
```

## Notes

- `setup.py` turns off Claude Code's built-in auto-memory (`autoMemoryEnabled: false`) so the two don't write to different places. Mention this once in the confirmation.
- Nothing leaves this computer. There is no sharing or sync in this edition.
- `setup.py` backs up `~/.claude/settings.json` to `settings.json.bak-my-memory` before changing it.
