# luaudit v1.0.0

First public release.

**luaudit catches your AI agent's Luau mistakes before you do.** After every
edit your agent makes in Claude Code or Codex, luaudit type-checks and lints
the Luau (luau-lsp + selene + StyLua) and hands findings straight back to the
agent as context, so it fixes its own work instead of shipping it to you.

## Highlights

- **Zero-touch hook loop.** Errors appear inline right after the edit; new
  warnings are held to turn end so they never derail a task mid-flight, then
  delivered in one sweep. Clean edits stay completely silent.
- **Repeat collapse and auto-muting.** Identical warnings across turns
  collapse to one line ("3 previously reported warning(s) still present,
  unchanged") and auto-mute after several repeats; `luaudit unmute` restores
  them.
- **Cross-file type checking.** Resolves `require()`s through a
  `sourcemap.json` generated automatically from a Rojo project, or on demand
  via `luaudit sourcemap` for Script Sync trees.
- **Studio mirror for MCP-only workflows.** Ships a companion Studio plugin
  that mirrors scripts to disk so agents working through bridges get the same
  checking; coexists with Rojo/Argon/Script Sync without forking your project
  (`luaudit plugin install` detects your setup).
- **Trustworthy toolchain.** luau-lsp/selene/StyLua binaries are SHA256-pinned
  and verified before extraction; nothing runs that wasn't checked.
- **Debuggable failures.** Every download/tool failure lands in
  `~/.luaudit/luaudit.log`; `luaudit doctor --bug-report` emits a paste-ready
  issue report.

## Verified before release

- Full pytest suite (158 tests) green on Ubuntu and Windows CI.
- Live end-to-end sessions on Windows: Claude Code and Codex both receive
  inline diagnostics, end-of-turn sweeps, and repeat summaries; the Stop-hook
  digest was confirmed to reach the agent and not loop.
- Roblox Studio mirror exercised live against a real Studio instance,
  including Rojo coexistence detection.

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

Requirements: any Python 3 on PATH (the CLI itself wants 3.11+). Works on
Windows, macOS, Linux.

## Notes

- On Codex 0.147, plugin hooks run under `codex exec` only when hook trust is
  bypassed for the invocation (`--dangerously-bypass-hook-trust`); interactive
  sessions prompt for trust once and remember it. This is tracked upstream;
  luaudit works either way.
- MPL-2.0. Not affiliated with or endorsed by Roblox. Luau is a trademark of
  Roblox Corporation.
