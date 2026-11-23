# Restart Resume

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/mindroom-ai/restart-resume-plugin/actions/workflows/plugin-ci.yml/badge.svg)](https://github.com/mindroom-ai/restart-resume-plugin/actions/workflows/plugin-ci.yml)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark-animated.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark.svg" alt="MindRoom Logo" align="right" width="120" />
</picture>

A [MindRoom](https://github.com/mindroom-ai/mindroom) hook plugin that resumes idle threads tagged before a restart once the bot comes back online.

MindRoom already resumes active or interrupted turns natively. Use this plugin for idle threads—for example, work waiting on a config change or deploy—so the agent picks them back up automatically after the next restart:

```text
🔄 Restart completed — this thread's `pending-restart` changes are now live.
```

- **Tag before restarting:** mark any idle thread with `pending-restart` (or `restart-pending`, or a custom tag).
- **Wakes automatically on startup:** once MindRoom is ready, each matching thread gets one wake-up message that triggers agent dispatch.
- **Cleans up after itself:** matched restart tags are cleared after the message is sent; other thread tags stay untouched.
- **No duplicate notices:** a startup lock prevents multiple workers from notifying the same thread twice.

## Quick start

```bash
mindroom plugins install restart-resume-plugin
```

```yaml
plugins:
  - path: plugins/restart-resume-plugin
    # settings:
    #   tag: pending-restart  # optional; replaces both default tags
```

Update later with `mindroom plugins update restart-resume-plugin`.
Needs MindRoom v2026.11.491 or later (plugin API 1.33).

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `tag` | `pending-restart`, `restart-pending` | Thread tag to watch on restart. Omitting `tag` matches **both** `pending-restart` and `restart-pending` (and clears both on a matching thread). Setting `tag` replaces both defaults with that single stripped, lowercased tag. |

<details>
<summary><b>How it works</b></summary>

1. At service startup (`bot:ready`), `notify-after-restart` acquires a non-blocking exclusive POSIX `fcntl.flock` lock on `state_root/.restart-claim` (skipping quietly if another worker already holds it).
2. It scans joined rooms up to 3 at a time (`asyncio.TaskGroup`) for `com.mindroom.thread.tags` state events, allowing 10 seconds per room for the initial read and retrying timed-out rooms once with a 60-second read timeout—all within the hook's 5-minute (`300_000` ms) overall cap.
3. Sending notices (`trigger_dispatch=True`) and clearing tags run outside the per-room read deadline so an in-progress room isn't cut short.
4. Empty-content (`{}`) tombstones win over live records, and legacy per-thread tag records are ignored. Each thread with one or more live restart tags receives one wake-up message.
5. After the message is sent, each matched restart tag is re-read at its canonical state key; if unchanged since the scan, it is removed with an empty `{}` tombstone (matching MindRoom's own tag removal), leaving other tags and concurrently updated restart tags untouched.

Notes:

- **Manifest name:** `mindroom.plugin.json` registers the plugin name as `restart-notify` (hooks-only; no agent tools required).
- **Internal helper import:** `hooks.py` imports `parse_thread_tag_record` and `parse_thread_tag_state_key` from `mindroom.thread_tags` (outside the stable `mindroom.plugin_api` surface), so `mindroom plugins check` warns about it; re-run `mindroom plugins check` after MindRoom upgrades.
- **Manual install:** instead of `mindroom plugins install`, clone this repository into `plugins/restart-resume-plugin` next to your `config.yaml` and reference `path: plugins/restart-resume-plugin`.

</details>

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `notify-after-restart` | `bot:ready` (service) | Scan joined rooms for tagged threads, send one wake-up message per thread, and clear matched restart tags |
