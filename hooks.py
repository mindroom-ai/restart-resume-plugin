# ruff: noqa: INP001
"""Notify threads tagged pending-restart after a bot restart."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks import EVENT_BOT_READY, hook
from mindroom.thread_tags import parse_thread_tag_state_key

if TYPE_CHECKING:
    from mindroom.hooks import AgentLifecycleContext

THREAD_TAGS_EVENT_TYPE = "com.mindroom.thread.tags"
PENDING_RESTART_TAGS = ("pending-restart", "restart-pending")


def _pending_restart_tags(settings: dict[str, object]) -> tuple[str, ...]:
    """Return configured restart tag, or supported default aliases."""
    configured_tag = settings.get("tag")
    if isinstance(configured_tag, str) and configured_tag.strip():
        return (configured_tag.strip().lower(),)
    return PENDING_RESTART_TAGS


def _acquire_restart_claim(claim_path: Path) -> int | None:
    """Acquire startup claim without removing another worker's claim."""
    fd = os.open(str(claim_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


async def _clear_restart_tag(ctx: AgentLifecycleContext, room_id: str, thread_id: str, state_key: str) -> bool:
    """Write MindRoom's empty-content tombstone over one per-tag state event."""
    try:
        state_cleared = await ctx.put_room_state(room_id, THREAD_TAGS_EVENT_TYPE, state_key=state_key, content={})
    except Exception:
        ctx.logger.warning(
            "Failed to clear restart tag after notification",
            room_id=room_id,
            thread_id=thread_id,
            reason="exception",
            exc_info=True,
        )
        return False
    if not state_cleared:
        ctx.logger.warning(
            "Failed to clear restart tag after notification",
            room_id=room_id,
            thread_id=thread_id,
            reason="false_result",
        )
        return False
    return True


async def _notify_room_threads(
    ctx: AgentLifecycleContext,
    room_id: str,
    pending_tags: tuple[str, ...] = PENDING_RESTART_TAGS,
) -> int:
    """Notify pending-restart threads in one room and return the count."""
    try:
        # Each tag is its own state event keyed by json [thread_root_id, tag].
        tag_states = await ctx.query_room_state(room_id, THREAD_TAGS_EVENT_TYPE)
    except Exception:
        ctx.logger.warning("Failed to query room state", room_id=room_id, exc_info=True)
        return 0
    if not tag_states:
        return 0

    pending_keys_by_thread: dict[str, dict[str, str]] = {}
    for state_key, content in tag_states.items():
        parsed_state_key = parse_thread_tag_state_key(state_key)
        # Empty content is MindRoom's removal tombstone for this tag.
        if parsed_state_key is None or not content:
            continue
        thread_id, tag = parsed_state_key
        if tag in pending_tags:
            pending_keys_by_thread.setdefault(thread_id, {})[tag] = state_key

    notified = 0
    for thread_id, state_keys_by_tag in pending_keys_by_thread.items():
        tag = next(tag for tag in pending_tags if tag in state_keys_by_tag)
        event_id = await ctx.send_message(
            room_id=room_id,
            text=f"🔄 Restart completed — this thread's `{tag}` changes are now live.",
            thread_id=thread_id,
            trigger_dispatch=True,
        )
        if not event_id:
            ctx.logger.warning("Failed to notify thread", room_id=room_id, thread_id=thread_id)
            continue
        cleared = [await _clear_restart_tag(ctx, room_id, thread_id, key) for key in state_keys_by_tag.values()]
        if all(cleared):
            ctx.logger.info("Notified pending-restart thread", room_id=room_id, thread_id=thread_id)
            notified += 1
    return notified


@hook(EVENT_BOT_READY, name="notify-after-restart", agents=(ROUTER_AGENT_NAME,), priority=100, timeout_ms=30000)
async def notify_after_restart(ctx: AgentLifecycleContext) -> None:
    """Scan rooms for pending-restart tagged threads and notify them."""
    if ctx.room_state_querier is None:
        ctx.logger.warning("No room state querier — cannot scan for pending-restart threads")
        return

    claim_path = Path(ctx.state_root) / ".restart-claim"
    claim_fd = _acquire_restart_claim(claim_path)
    if claim_fd is None:
        return

    try:
        pending_tags = _pending_restart_tags(ctx.settings)
        notified = 0
        for room_id in ctx.joined_room_ids:
            notified += await _notify_room_threads(ctx, room_id, pending_tags)
        if notified:
            ctx.logger.info("Restart-notify complete", notified_count=notified)
    finally:
        os.close(claim_fd)
