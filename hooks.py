# ruff: noqa: INP001
"""Notify threads tagged pending-restart after a bot restart."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks import EVENT_BOT_READY, hook

if TYPE_CHECKING:
    from mindroom.hooks import AgentLifecycleContext

THREAD_TAGS_EVENT_TYPE = "com.mindroom.thread.tags"


async def _notify_room_threads(ctx: AgentLifecycleContext, room_id: str, tag: str) -> int:
    """Notify pending-restart threads in one room and return the count."""
    try:
        tags_by_thread = await ctx.query_room_state(room_id, THREAD_TAGS_EVENT_TYPE)
    except Exception:
        ctx.logger.warning("Failed to query room state", room_id=room_id, exc_info=True)
        return 0
    if not tags_by_thread:
        return 0

    notified = 0
    for thread_id, content in tags_by_thread.items():
        thread_tags = content.get("tags", {})
        if tag not in thread_tags:
            continue

        event_id = await ctx.send_message(
            room_id=room_id,
            text=f"🔄 Restart completed — this thread's `{tag}` changes are now live.",
            thread_id=thread_id,
            trigger_dispatch=True,
        )
        if event_id:
            current_tags = dict(thread_tags)
            current_tags.pop(tag, None)
            await ctx.put_room_state(
                room_id,
                THREAD_TAGS_EVENT_TYPE,
                state_key=thread_id,
                content={"tags": current_tags},
            )
            ctx.logger.info("Notified pending-restart thread", room_id=room_id, thread_id=thread_id)
            notified += 1
        else:
            ctx.logger.warning("Failed to notify thread", room_id=room_id, thread_id=thread_id)
    return notified


@hook(EVENT_BOT_READY, name="notify-after-restart", agents=(ROUTER_AGENT_NAME,), priority=100, timeout_ms=30000)
async def notify_after_restart(ctx: AgentLifecycleContext) -> None:
    """Scan rooms for pending-restart tagged threads and notify them."""
    claim_path = Path(ctx.state_root) / ".restart-claim"
    claim_path.unlink(missing_ok=True)

    try:
        fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return

    tag = ctx.settings.get("tag", "pending-restart")

    if ctx.room_state_querier is None:
        ctx.logger.warning("No room state querier — cannot scan for pending-restart threads")
        claim_path.unlink(missing_ok=True)
        return

    try:
        notified = 0
        for room_id in ctx.joined_room_ids:
            notified += await _notify_room_threads(ctx, room_id, tag)
        if notified:
            ctx.logger.info("Restart-notify complete", notified_count=notified)
    finally:
        claim_path.unlink(missing_ok=True)
