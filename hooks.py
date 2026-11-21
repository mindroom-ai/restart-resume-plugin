# ruff: noqa: INP001
"""Notify threads tagged pending-restart after a bot restart."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.hooks import EVENT_BOT_READY, hook
from mindroom.thread_tags import parse_thread_tag_record, parse_thread_tag_state_key

if TYPE_CHECKING:
    from mindroom.hooks import AgentLifecycleContext

THREAD_TAGS_EVENT_TYPE = "com.mindroom.thread.tags"
PENDING_RESTART_TAGS = ("pending-restart", "restart-pending")
_ROOM_WORKERS = 3
_ROOM_TIMEOUT_SECONDS = 10
_ROOM_RETRY_TIMEOUT_SECONDS = 60


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


async def _clear_restart_tag(
    ctx: AgentLifecycleContext,
    room_id: str,
    thread_id: str,
    state_key: str,
    observed: dict[str, object],
) -> bool:
    """Tombstone a tag at its canonical key, as MindRoom does, unless that key changed since the scan."""
    try:
        canonical_key = json.dumps(parse_thread_tag_state_key(state_key), separators=(",", ":"))
        current = await ctx.query_room_state(room_id, THREAD_TAGS_EVENT_TYPE, state_key=canonical_key)
        # An empty/missing canonical key is expected when the scanned record lives under a noncanonical spelling.
        if current != observed and (current or state_key == canonical_key):
            if current is None:
                ctx.logger.info("Could not re-read restart tag; keeping it", room_id=room_id, thread_id=thread_id)
            else:
                ctx.logger.info("Restart tag changed since scan; leaving it", room_id=room_id, thread_id=thread_id)
            return True
        state_cleared = await ctx.put_room_state(room_id, THREAD_TAGS_EVENT_TYPE, state_key=canonical_key, content={})
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
    *,
    timeout_seconds: float = _ROOM_TIMEOUT_SECONDS,
) -> int | None:
    """Return the count of notified threads, or None if the initial read times out."""
    try:
        # Time out only discovery; don't interrupt a notice or its tag cleanup mid-room.
        async with asyncio.timeout(timeout_seconds):
            tag_states = await ctx.query_room_state(room_id, THREAD_TAGS_EVENT_TYPE)
    except TimeoutError:
        ctx.logger.warning(
            "Restart notice discovery timed out for room", room_id=room_id, timeout_seconds=timeout_seconds
        )
        return None
    except Exception:
        ctx.logger.warning("Failed to query room state", room_id=room_id, exc_info=True)
        return 0
    if not tag_states:
        return 0

    # Mirror MindRoom's room snapshot: valid records per parsed (thread, tag); any `{}` tombstone wins.
    records: dict[tuple[str, str], tuple[str, dict[str, object]]] = {}
    tombstones: set[tuple[str, str]] = set()
    for state_key, content in tag_states.items():
        parsed_state_key = parse_thread_tag_state_key(state_key)
        if not isinstance(content, dict) or parsed_state_key is None or parsed_state_key[1] not in pending_tags:
            continue
        if not content:
            tombstones.add(parsed_state_key)
        elif parse_thread_tag_record(parsed_state_key[1], content) is not None:
            records[parsed_state_key] = (state_key, content)
    pending_by_thread: dict[str, dict[str, tuple[str, dict[str, object]]]] = {}
    for (thread_id, tag), seen in records.items():
        if (thread_id, tag) not in tombstones:
            pending_by_thread.setdefault(thread_id, {})[tag] = seen

    notified = 0
    for thread_id, pending_by_tag in pending_by_thread.items():
        tag = next(tag for tag in pending_tags if tag in pending_by_tag)
        event_id = await ctx.send_message(
            room_id=room_id,
            text=f"🔄 Restart completed — this thread's `{tag}` changes are now live.",
            thread_id=thread_id,
            trigger_dispatch=True,
        )
        if not event_id:
            ctx.logger.warning("Failed to notify thread", room_id=room_id, thread_id=thread_id)
            continue
        cleared = [await _clear_restart_tag(ctx, room_id, thread_id, *seen) for seen in pending_by_tag.values()]
        if all(cleared):
            ctx.logger.info("Notified pending-restart thread", room_id=room_id, thread_id=thread_id)
            notified += 1
    return notified


@hook(EVENT_BOT_READY, name="notify-after-restart", scope="service", priority=100, timeout_ms=300_000)
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

        async def scan_pass(rooms: tuple[str, ...], timeout_seconds: float) -> tuple[int, tuple[str, ...]]:
            # Share one iterator so each worker takes the next room as soon as it is free.
            room_ids = iter(rooms)
            timed_out: list[str] = []

            async def scan_rooms() -> int:
                notified = 0
                for room_id in room_ids:
                    try:
                        count = await _notify_room_threads(ctx, room_id, pending_tags, timeout_seconds=timeout_seconds)
                    except Exception:
                        ctx.logger.warning("Failed to process restart notices for room", room_id=room_id, exc_info=True)
                        continue
                    if count is None:
                        timed_out.append(room_id)
                    else:
                        notified += count
                return notified

            async with asyncio.TaskGroup() as group:
                scans = [group.create_task(scan_rooms()) for _ in range(min(_ROOM_WORKERS, len(rooms)))]
            return sum(scan.result() for scan in scans), tuple(timed_out)

        notified, timed_out = await scan_pass(ctx.joined_room_ids, _ROOM_TIMEOUT_SECONDS)
        if timed_out:
            retried, _ = await scan_pass(timed_out, _ROOM_RETRY_TIMEOUT_SECONDS)
            notified += retried
        if notified:
            ctx.logger.info("Restart-notify complete", notified_count=notified)
    finally:
        os.close(claim_fd)
