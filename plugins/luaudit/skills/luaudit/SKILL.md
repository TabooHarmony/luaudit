---
name: luaudit
description: Run Luau type checking and linting (luau-lsp + selene) on Luau files when working on Roblox or Luau code. Use after editing .luau or .lua files to catch type errors and lint issues early.
---

# luaudit

Advisory Luau diagnostics for Roblox and Luau development. After editing
Luau code, verify it before declaring work complete.

## How to use

Run on a file or directory:

```bash
luaudit check <file-or-directory>
```

Prefer paths relative to the current directory. Windows shells (git-bash,
MSYS) strip backslashes from unquoted absolute paths, and the tool then
honestly reports the mangled path as missing.

If the `luaudit` CLI is not installed, run the hook engine directly:

```bash
python "<PLUGIN_ROOT>/scripts/luaudit_hook.py" check <file-or-directory>
```

(`python3` on macOS/Linux, `python` or `py` on Windows.)

- Exit 0 with no output means the code is clean.
- Non-zero exit (or diagnostics printed) means errors that should be fixed.
- Add `--warnings` to also fail on warnings (strict mode).

## Hook

This plugin also installs hooks that run on their own; you do not need to
invoke anything.

- After each Write/Edit of a `.luau`/`.lua` file, errors are shown inline
  immediately (errors compound, so they are never held back).
- Warnings are held until your turn ends. At turn end a sweep delivers new
  warnings plus one line per file whose warnings repeat unchanged, so you
  can fix or acknowledge them without mid-task noise.
- Clean edits stay completely silent.
- Repeated identical warnings collapse to a single line instead of piling up;
  they auto-mute after several repeats and `luaudit unmute` brings them back.

If the sweep reports findings at your turn end, address them (or explain why
they are intentional) before finishing.

## What it checks

- Type errors and warnings via luau-lsp (Roblox platform, strict mode)
- Lint issues via selene (Roblox standard)
- Formatting via StyLua (advisory warning)

The toolchain is downloaded once on first use into `~/.luaudit/`.

Useful CLI commands: `luaudit check`, `format` (StyLua in place), `init`
(default configs), `sourcemap`, `doctor` (`--bug-report` collects everything
needed for a bug report), `unmute`, `plugin` (Studio mirror management).

## Script Sync users

If the project's Luau files come from Roblox Studio Script Sync there is no
sourcemap, so `require()`s cannot resolve across files. One command fixes
that without installing Rojo:

```bash
luaudit sourcemap path/to/synced/tree
```

Run it once per session after syncing; it writes a `sourcemap.json` next to
the scripts.
