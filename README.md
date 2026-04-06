# Restart Resume

MindRoom plugin that re-activates tagged threads after a bot restart.

MindRoom already resumes in-progress work automatically — if an agent was mid-reply or had a scheduled task pending, it picks back up after restart. But sometimes a thread is idle (no pending reply, no scheduled task) and you still want the agent to continue working in it after a restart. That's what this plugin is for.

Tag the thread with `pending-restart`, restart MindRoom, and the plugin sends a message into that thread to wake the agent up.

## When to use it

- You need to restart MindRoom (e.g., to test a new feature or deploy a config change)
- An agent was working in a thread but has finished its current turn — nothing is actively in progress
- You want the agent to pick up where it left off after the restart, without manually messaging the thread

## How it works

1. Tag a thread with `pending-restart` (via the thread tagging system)
2. Restart MindRoom
3. On `bot:ready`, the plugin scans all rooms for threads tagged `pending-restart`
4. Sends a notification to each tagged thread with `trigger_dispatch=True` (wakes the agent)
5. Removes the `pending-restart` tag

## Safety

- **Atomic claim file** (`state_root/.restart-claim`) prevents duplicate notifications when multiple agents start simultaneously
- Stale claim files are auto-cleaned
- Tag name is configurable via plugin settings

## Setup

1. Copy to `~/.mindroom/plugins/restart-resume`
2. Add to `config.yaml`:
   ```yaml
   plugins:
     - path: plugins/restart-resume
   ```
3. Restart MindRoom

No agent tools needed — this plugin is hooks only.