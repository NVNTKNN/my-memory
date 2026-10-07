# my-memory

A memory for Claude Code, on **Mac and Windows**. Claude saves what matters from your sessions into a folder of notes on your computer, and finds the right notes again on its own in later sessions.

Everything stays on your computer. Nothing is shared or synced anywhere.

**Where it works:** Claude Code — in a terminal, or in the **Code** tab of the Claude desktop app. It does **not** work in ordinary Claude chat (claude.ai or the desktop app's Chat tab), because chat cannot run the small programs that do the remembering.

## Install the easy way (Mac or Windows)

You need **Claude Code** installed and signed in. Claude does everything else.

1. Put `my-memory.zip` in your **Downloads** folder.
   (Downloaded it from GitHub as `my-memory-main.zip`? That's fine too.)
2. Open Claude Code, paste the prompt below, and press Enter.
3. Say **Yes** when Claude asks permission to run things, and when your computer asks to allow an installer.
4. Answer Claude's four short questions. The defaults are fine.
5. The first install takes 10–20 minutes. When Claude says it's done, **close Claude Code and open it again**. Memory is on from then.

```text
Please install the "my-memory" plugin for me. I'm not technical, so do every step yourself, tell me in one plain sentence what you're doing at each step, and only ask me something when you need my answer.

1. Work out whether this computer is a Mac or Windows.

2. Find my-memory.zip or my-memory-main.zip (probably in my Downloads folder). Extract it so the folder ends up at "my-memory" inside my home folder (rename it if it comes out as my-memory-main). If that folder already exists, replace it. On Windows use PowerShell's Expand-Archive (Git Bash has no unzip); on a Mac use unzip.

3. Make sure the right Python is installed:
   - Windows: Python 3.11 exactly, plus Git. Install whichever is missing with
       winget install --id Git.Git -e
       winget install --id Python.Python.3.11 -e --scope user
     If you just installed Python, this session won't see it yet, so use its full path
     (usually %LOCALAPPDATA%\Programs\Python\Python311\python.exe).
   - Mac: Python 3.10, 3.11 or 3.12 (the python3 that comes with macOS is too old).
     If none is installed, download the official Python 3.12 macOS installer from
     python.org and open it so I can click through it. Then use python3.12.

4. Install the plugin:
     claude plugin marketplace add "<the my-memory folder>"
     claude plugin install my-memory@my-memory-local

5. Open skills/setup/SKILL.md inside the my-memory folder and follow its steps exactly, including asking me its questions. Its last step takes 5 to 15 minutes, so tell me before it starts and run it in the background.

6. When everything is done, tell me where my notes folder is, and tell me to close and reopen Claude Code. If anything fails, explain the error in plain words.
```

## Everyday use

| Type this | What happens |
|---|---|
| *(nothing)* | On every message, Claude quietly looks up your most relevant notes and reads them. |
| `/my-memory:add <something>` | Claude drafts a note about it and saves it once you say yes. |
| `/my-memory:wrap` | At the end of a session, Claude proposes what to save from the chat. You pick. |
| `/my-memory:recall <question>` | Search your notes directly. |
| `/my-memory:tasks` | List open to-dos found in your notes. |
| `/my-memory:doctor` | Fix broken links and other tidy-up items Claude found. |
| `/my-memory:rebuild` | Rebuild the search index if results look stale (rarely needed). |

Your notes are plain text files (Markdown) in the folder you picked during setup. By default that's a `Memory` folder in your home folder. Open and edit them in any text editor, or in Obsidian. `MEMORY.md` is the start page.

## Install by hand

If you'd rather type the commands yourself.

### What you need

| | Mac | Windows |
|---|---|---|
| Claude Code | `claude --version` works | `claude --version` works |
| Python | 3.10, 3.11 or 3.12 — check `python3.12 --version`. Get it from [python.org](https://www.python.org/downloads/macos/) | **3.11 exactly** — check `py -3.11 --version`. Get it: `winget install Python.Python.3.11` |
| Git | not needed | needed — check `git --version`. Get it: `winget install Git.Git` |
| Disk space | about 2 GB | about 2 GB |

Windows needs exactly 3.11 because one search library has no Windows build for newer versions.

### Steps

1. **Unzip** so the folder sits at `my-memory` in your home folder.
   - Mac (Terminal): `unzip ~/Downloads/my-memory.zip -d ~`
   - Windows (PowerShell): `Expand-Archive -Path "$HOME\Downloads\my-memory.zip" -DestinationPath "$HOME"`

   **Leave the folder there.** Claude Code installs the plugin from it.

2. **Install the plugin:**
   - Mac: `claude plugin marketplace add ~/my-memory`
   - Windows: `claude plugin marketplace add "$HOME\my-memory"`

   Then, on both: `claude plugin install my-memory@my-memory-local`

3. **Start Claude Code** and type `/my-memory:setup`. It asks four short questions, then installs everything (5–15 minutes the first time).

4. **Close Claude Code and start a new session.**

## Good to know

- Setup turns off Claude Code's built-in auto-memory, so notes don't land in two places.
- Setup adds five small hooks to `~/.claude/settings.json` (on Windows, `C:\Users\<you>\.claude\settings.json`), after saving a backup next to it (`settings.json.bak-my-memory`). Running setup again is safe.
- Keep the notes folder out of iCloud Drive and OneDrive. Files kept "online-only" look empty to the search index.
- Logs, if something seems off: `~/.claude/memory_index/logs/`.

## If setup fails

- **"needs Python 3.11" / "needs Python 3.10 to 3.12"** — install the version in the table above, reopen Claude Code, run `/my-memory:setup` again.
- **Windows: "DLL load failed" or "c10.dll"** — install the [Microsoft Visual C++ Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe), then run setup again.
- **"settings.json is not valid JSON"** — open that file, fix the error it points to (or ask Claude to), run setup again.

## Uninstall

```
claude plugin uninstall my-memory@my-memory-local
claude plugin marketplace remove my-memory-local
```

Then ask Claude to remove the hooks that mention `memory_index` from `~/.claude/settings.json`, and delete the folders `~/.claude/memory_index` and `~/.claude/memory_chroma`. Your notes folder is never touched.
