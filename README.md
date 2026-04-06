# Restart Resume

[![License](https://img.shields.io/github/license/mindroom-ai/restart-resume-plugin)](https://github.com/mindroom-ai/restart-resume-plugin/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<img src="https://media.githubusercontent.com/media/mindroom-ai/mindroom/refs/heads/main/frontend/public/logo.png" alt="MindRoom Logo" align="right" width="120" />

Re-activate idle threads after a [MindRoom](https://github.com/mindroom-ai/mindroom) restart.

MindRoom already resumes in-progress work automatically — if an agent was mid-reply or had a scheduled task pending, it picks back up after restart. But sometimes a thread is idle (no pending reply, no scheduled task) and you still want the agent to continue working in it after a restart. That's what this plugin is for. Tag the thread, restart MindRoom, and the plugin sends a message to wake the agent up.

## How it works

1. Tag a thread with `pending-restart` (via the thread tagging system)
2. Restart MindRoom
3. On `bot:ready`, the plugin scans all rooms for threads tagged `pending-restart`
4. Sends a notification to each tagged thread with `trigger_dispatch=True` (wakes the agent)
5. Removes the `pending-restart` tag

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `restart_resume` | `bot:ready` | Scan for tagged threads and send wake notifications (priority 100, 30s timeout) |

## Configuration

The tag name (`pending-restart`) is configurable via plugin settings. An atomic claim file (`state_root/.restart-claim`) prevents duplicate notifications when multiple agents start simultaneously.

## Setup

1. Copy to `~/.mindroom/plugins/restart-resume`
2. Add to `config.yaml`:
   ```yaml
   plugins:
     - path: plugins/restart-resume
   ```
3. Restart MindRoom

No agent tools needed — this plugin is hooks only.