<div align="center">
  <img src="assets/logo.svg" alt="luaudit" width="160"/>

  # luaudit

  [![CI](https://github.com/TabooHarmony/luaudit/actions/workflows/ci.yml/badge.svg)](https://github.com/TabooHarmony/luaudit/actions/workflows/ci.yml)
  [![Release](https://img.shields.io/github/v/release/TabooHarmony/luaudit)](https://github.com/TabooHarmony/luaudit/releases/latest)
  [![License](https://img.shields.io/github/license/TabooHarmony/luaudit)](LICENSE)
</div>

**luaudit catches your AI agent's Luau mistakes before you do.**

After every edit your agent makes, luaudit type-checks and lints the Luau
and hands errors and warnings straight back to the agent, so it fixes its
own work instead of shipping it to you. Nothing to run by hand.

First check downloads the toolchain into `~/.luaudit`. After that it just works.

Binaries (luau-lsp, selene, stylua) are SHA256-pinned and verified before
extraction. Type definitions update weekly over HTTPS by design, since they
track the live Luau language surface.

## Requirements

- Any Python 3 on PATH. On Windows the hook launcher also checks the
  standard install locations (`C:\Program Files\Python312`, `C:\Python312`)
  before giving up.
- Works wherever Claude Code or Codex runs: Windows, macOS, Linux.
- Installing the CLI itself with pip or uvx needs Python 3.11+.

## Install

Claude Code:

```
/plugin marketplace add TabooHarmony/luaudit
/plugin install luaudit
```

Codex:

```
codex plugin marketplace add TabooHarmony/luaudit
codex plugin add luaudit@luaudit
```

That's it. The plugin loads its skill and its post-edit hook on its own.

## Updating

Both harnesses copy the plugin into their own cache at install time, so a
`git pull` alone never updates an installed plugin.

Claude Code:

```
/plugin marketplace update luaudit
/plugin update luaudit
```

Codex:

```
codex plugin marketplace upgrade luaudit
codex plugin remove luaudit@luaudit
codex plugin add luaudit@luaudit
```

Third-party marketplaces do not auto-update on session start by default in
either harness. If the hook stops reporting diagnostics after you push new
commits, run the refresh above; a stale cache copy is the first suspect.

## What it does

- Type-checks, lints, and checks formatting after every Write/Edit
- Hands errors and warnings back to the agent as context
- Stays silent on clean edits
- Never blocks your work
- Uses your existing `.luaurc`, `selene.toml`, and `.stylua.toml` if it finds
  them, so existing configs keep working
- Resolves `require()`s across files when it finds a sourcemap (a
  `sourcemap.json`, or a Rojo `default.project.json` it can generate one from),
  so cross-file type errors are caught too. Without a sourcemap it falls back
  to per-file checking.

## What an agent sees

An agent writes a broken file:

```luau
local x: number = "boom"
local unused = 1
```

luaudit fires on its own. The agent sees errors inline immediately, in this
format (same layout as `luaudit check`, shown here for a codex run):

```
broken2.luau:1:1: ERROR [luau-lsp/TypeError] Expected this to be 'number', but got 'string'
broken2.luau:1:7: WARNING [luau-lsp/LocalUnused] Variable 'x' is never used; prefix with '_' to silence
broken2.luau:1:7: WARNING [selene/unused_variable] x is assigned a value, but never used
broken2.luau:2:7: WARNING [luau-lsp/LocalUnused] Variable 'unused' is never used; prefix with '_' to silence
broken2.luau:2:7: WARNING [selene/unused_variable] unused is assigned a value, but never used
summary: 1 errors, 4 warnings, 5 total
```

The agent follows luaudit's own hints: fixes the type, renames the dead
variables to `_x` and `_unused`, re-checks, gets silence, and only then
reports done. Nobody ran a linter by hand at any point.

The example above shows error lines; warnings are held to the agent's turn
end, then delivered in one sweep with a per-file repeat line ("3 previously
reported warning(s) still present, unchanged") so a file that still has the
same problems can't slip through silently across turns. Verified live in
both Claude Code and Codex sessions.

## Noise control

- Errors always show inline immediately.
- New warnings arrive once at turn end; identical repeats collapse to one
  summary line.
- After several unchanged repeats a warning auto-mutes itself; `luaudit
  unmute` restores it. `--warnings` on `check` fails hard when you want
  strictness instead of advisory output.

## Studio mirror

For workflows where scripts live only inside Studio (MCP bridges and
similar), luaudit ships a silent companion plugin that mirrors the script
tree to disk every few seconds so the hook can check it. One-way: it never
writes back into Studio.

Already syncing with Rojo, Argon, Azul, or Script Sync? Nothing changes.
`luaudit plugin install` detects your setup and installs the mirror in an
idle mode, so it never forks your project into two copies; the hook checks
your real files directly. Disagree with the detection? Force it:

```
luaudit plugin install --yes --mirror-mode mirror   # or external / ask
```

Using plain Script Sync? Your files are on disk but no sourcemap comes with
them, so cross-file type checking stays off by default. Generate one
without adopting Rojo:

```
luaudit sourcemap path/to/synced/tree
```

That writes a `sourcemap.json` next to your scripts and require()s start
resolving across files.

Install it with the engine (keeps versions matched):

```
luaudit plugin install --yes
```

Then restart Roblox Studio; plugins load at launch only. `luaudit doctor`
reports whether the installed mirror matches your engine build. Remove with
`luaudit plugin remove`. Prefer to manage it by hand? Copy
`plugins/luaudit/studio/luaudit-mirror.rbxmx` from the repo into
`%LOCALAPPDATA%\Roblox\Plugins\` (Windows) or `~/Documents/Roblox/Plugins/`
(macOS) yourself.

## If something breaks

Run:

```
luaudit doctor --bug-report
```

and paste the output into a new issue. It collects the luaudit and Python
versions, platform, installed tool paths, mirror status, and the tail of
`~/.luaudit/luaudit.log`, where every toolchain download and engine failure
is recorded. That is everything needed to reproduce and fix a failure.

## Credit

luaudit depends on the following projects:

- [luau-lsp](https://github.com/JohnnyMorganz/luau-lsp) by
  [JohnnyMorganz](https://github.com/JohnnyMorganz): Luau language server and
  type checker. MIT.
- [selene](https://github.com/Kampfkarren/selene) by
  [Kampfkarren](https://github.com/Kampfkarren): Luau linting. MPL-2.0.
- [StyLua](https://github.com/JohnnyMorganz/StyLua) by
  [JohnnyMorganz](https://github.com/JohnnyMorganz): Luau formatting. MPL-2.0.

Type checking also relies on the Roblox type definitions generated from
Roblox's API dumps and hosted at
[luau-lsp.pages.dev](https://luau-lsp.pages.dev).

## License

MPL-2.0. Independent project, not affiliated with or endorsed by Roblox or the
projects above or their maintainers. Luau is a trademark of Roblox
Corporation.
