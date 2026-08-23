# luaudit v1.1.0

fixed
- codex on windows: `hooks.json` had a duplicate `commandWindows` key, so codex's strict parser rejected the whole file and no luaudit hooks ever loaded
- posix launcher dropped cli arguments (`--warnings` etc.) instead of forwarding them
- posttooluse timeout raised 60s → 300s, first-run toolchain downloads no longer kill the hook
- bootstrap retries with backoff and prints a visible error naming the missing pinned tool instead of failing silent

added
- turn-end sweep in both claude code and codex: type errors still fire inline right after an edit, new warnings are held and delivered once when the agent finishes its turn
- identical warnings across turns collapse to a single line; warnings left unfixed across several sweeps auto-mute, `luaudit unmute` restores them
- shipped `selene.toml` defaults disable three noisy style lints
