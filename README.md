# Restart Notify

MindRoom plugin that automatically resumes interrupted agent work after a bot restart.

When MindRoom restarts, all active conversations stop mid-turn. This plugin lets agents tag a thread as "waiting for restart" before the restart happens, then automatically sends a notification to those threads once the bot is back online — waking the agent to pick up where it left off.

## How it works

1. Before restart, tag a thread with `pending-restart` (via the thread tagging system)
2. Restart MindRoom
3. On `bot:ready`, the plugin scans all rooms for threads tagged `pending-restart`
4. Sends a notification to each tagged thread with `trigger_dispatch=True` (wakes the agent)
5. Removes the `pending-restart` tag

## Safety

- **Atomic claim file** (`state_root/.restart-claim`) prevents duplicate notifications when multiple agents start simultaneously
- Stale claim files are auto-cleaned
- Tag name is configurable via plugin settings

## Setup

1. Copy to `~/.mindroom-chat/plugins/restart-notify`
2. Add to `config.yaml`:
   ```yaml
   plugins:
     - path: plugins/restart-notify
   ```
3. Restart MindRoom

No agent tools needed — this plugin is hooks only.