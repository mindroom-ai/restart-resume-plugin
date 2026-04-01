# restart-notify-plugin

A [MindRoom](https://github.com/mindroom-ai/mindroom) plugin that automatically notifies threads tagged `pending-restart` after the bot restarts, then clears the tags.

## What it does

When you're working with an agent and need to restart the bot (for a code deploy, config change, etc.), you tag the thread with `pending-restart`. After restart, the plugin automatically:

1. Scans all rooms for threads with the `pending-restart` tag
2. Sends a notification message to each tagged thread
3. Triggers the agent to respond (so it can resume work)
4. Removes the `pending-restart` tag

This closes the loop — you tag, restart, and the agent picks up where it left off without manual poking.

## Features

- **Automatic detection** — scans all joined rooms for `pending-restart` tagged threads via Matrix room state
- **Agent dispatch** — notification uses `trigger_dispatch=True` so the thread's agent actually responds
- **Single notification** — atomic claim file prevents duplicate notifications from multiple agents in the same process
- **Crash-safe** — stale claim files from previous crashes are cleaned up automatically
- **Tag cleanup** — removes the `pending-restart` tag after successful notification via `put_room_state()`
- **Configurable** — tag name can be overridden via plugin settings

## Requirements

- MindRoom with `bot:ready` hook event support (added in ISSUE-073)
- MindRoom with `query_room_state()` and `put_room_state()` on `HookContext` (added in ISSUE-072)
- MindRoom with `trigger_dispatch` parameter on `HookContext.send_message()` (added in ISSUE-072)

## Installation

1. Copy the plugin to your MindRoom plugins directory:

```bash
cp -r restart-notify-plugin ~/.mindroom/plugins/restart-notify
```

2. Add it to your `config.yaml`:

```yaml
plugins:
  - path: plugins/restart-notify
```

No agent tools are needed — this plugin only uses hooks.

## Usage

1. Tag a thread with `pending-restart` (via the `tag_thread` tool or Matrix room state)
2. Restart MindRoom
3. The plugin fires on `bot:ready` and notifies all tagged threads
4. The agent in each thread receives the notification and can respond

## How it works

The plugin registers a single hook on `bot:ready` (priority 100, 30s timeout):

1. **Claim** — Creates an atomic claim file (`state_root/.restart-claim`) via `O_CREAT | O_EXCL` to ensure only one agent processes notifications per restart
2. **Scan** — Queries `com.mindroom.thread.tags` state events across all joined rooms
3. **Notify** — Sends "🔄 Restart completed" message to each thread with `trigger_dispatch=True`
4. **Cleanup** — Removes the `pending-restart` tag via `put_room_state()`, then deletes the claim file

## License

MIT License — see [LICENSE](LICENSE).