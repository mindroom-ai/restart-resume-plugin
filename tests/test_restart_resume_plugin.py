# ruff: noqa: INP001
"""Behavior tests for restart-resume plugin."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sys
from importlib import util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from mindroom.hooks.decorators import get_hook_metadata
from mindroom.plugin_api import EVENT_BOT_READY


def _load_hooks_module() -> ModuleType:
    hooks_path = Path(__file__).resolve().parents[1] / "hooks.py"
    module_name = "mindroom_test_restart_resume_hooks"
    spec = util.spec_from_file_location(module_name, hooks_path)
    assert spec is not None
    assert spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


hooks = _load_hooks_module()


def _tag_key(thread_id: str, tag: str) -> str:
    """Build MindRoom's per-tag thread-tag state key."""
    return json.dumps([thread_id, tag], separators=(",", ":"))


_RECORD = {"set_by": "code", "set_at": "2026-11-19T00:00:00Z"}


def _room_state(states: dict[str, dict[str, object]]) -> AsyncMock:
    """Mock the hook querier: all events without a state key, one event's content with it."""

    async def query(_room_id: str, _event_type: str, state_key: str | None = None) -> object:
        return states if state_key is None else states.get(state_key)

    return AsyncMock(side_effect=query)


def test_hook_metadata_is_service_scoped() -> None:
    """Restart scan should run once at service startup."""
    metadata = get_hook_metadata(hooks.notify_after_restart)

    assert metadata is not None
    assert metadata.event_name == EVENT_BOT_READY
    assert metadata.hook_name == "notify-after-restart"
    assert metadata.scope == "service"
    assert metadata.agents is None
    assert metadata.priority == 100
    assert metadata.timeout_ms == 300_000


@pytest.mark.asyncio
async def test_pending_thread_is_notified_and_both_aliases_are_cleared() -> None:
    """One notification per thread; each restart alias is tombstoned and unrelated tags are untouched."""
    ctx = SimpleNamespace(
        query_room_state=_room_state(
            {
                _tag_key("$pending", "pending-restart"): {"set_by": "code", "set_at": "2026-11-19T00:00:00Z"},
                _tag_key("$pending", "restart-pending"): {"set_by": "code", "set_at": "2026-11-19T00:00:00Z"},
                _tag_key("$pending", "keep-me"): {"set_by": "user", "set_at": "2026-11-19T00:00:00Z"},
                _tag_key("$untouched", "keep-me"): {"set_by": "user", "set_at": "2026-11-19T00:00:00Z"},
            },
        ),
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(return_value=True),
        logger=MagicMock(),
    )

    notified = await hooks._notify_room_threads(ctx, "!room:localhost")

    assert notified == 1
    ctx.send_message.assert_awaited_once_with(
        room_id="!room:localhost",
        text="🔄 Restart completed — this thread's `pending-restart` changes are now live.",
        thread_id="$pending",
        trigger_dispatch=True,
    )
    assert ctx.put_room_state.await_args_list == [
        call(
            "!room:localhost",
            hooks.THREAD_TAGS_EVENT_TYPE,
            state_key=_tag_key("$pending", "pending-restart"),
            content={},
        ),
        call(
            "!room:localhost",
            hooks.THREAD_TAGS_EVENT_TYPE,
            state_key=_tag_key("$pending", "restart-pending"),
            content={},
        ),
    ]


@pytest.mark.asyncio
async def test_tag_re_added_after_scan_is_not_erased() -> None:
    """A restart tag re-set between scan and clear (new set_at) must survive for the next restart."""
    key = _tag_key("$pending", "pending-restart")
    scanned = {key: {"set_by": "code", "set_at": "2026-11-19T00:00:00Z"}}
    re_added = {"set_by": "agent", "set_at": "2026-11-19T00:05:00Z"}

    async def query(_room_id: str, _event_type: str, state_key: str | None = None) -> object:
        return scanned if state_key is None else re_added

    ctx = SimpleNamespace(
        query_room_state=AsyncMock(side_effect=query),
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(return_value=True),
        logger=MagicMock(),
    )

    notified = await hooks._notify_room_threads(ctx, "!room:localhost")

    assert notified == 1
    ctx.send_message.assert_awaited_once()
    ctx.put_room_state.assert_not_awaited()
    ctx.logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_record_mindroom_rejects_is_not_a_live_tag() -> None:
    """A non-empty record MindRoom would drop (no set_at) must not wake or clear the thread."""
    ctx = SimpleNamespace(
        query_room_state=_room_state({_tag_key("$pending", "pending-restart"): {"set_by": "code"}}),
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(return_value=True),
        logger=MagicMock(),
    )

    assert await hooks._notify_room_threads(ctx, "!room:localhost") == 0
    ctx.send_message.assert_not_awaited()
    ctx.put_room_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_tombstone_matches_parsed_pair_not_raw_key() -> None:
    """A `{}` at any key spelling of (thread, tag) removes the tag, whichever spelling holds the record."""
    ctx = SimpleNamespace(
        query_room_state=_room_state(
            {
                json.dumps(["$a", "pending-restart"]): _RECORD,
                _tag_key("$a", "pending-restart"): {},
                _tag_key("$b", "pending-restart"): _RECORD,
                json.dumps(["$b", "pending-restart"]): {},
            },
        ),
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(return_value=True),
        logger=MagicMock(),
    )

    assert await hooks._notify_room_threads(ctx, "!room:localhost") == 0
    ctx.send_message.assert_not_awaited()
    ctx.put_room_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_canonical_key_record_is_tombstoned_at_canonical_key() -> None:
    """Clearing re-reads the observed key and writes `{}` at the canonical key, as MindRoom's removal does."""
    spaced_key = json.dumps(["$pending", "pending-restart"])
    ctx = SimpleNamespace(
        query_room_state=_room_state({spaced_key: _RECORD}),
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(return_value=True),
        logger=MagicMock(),
    )

    assert await hooks._notify_room_threads(ctx, "!room:localhost") == 1
    ctx.query_room_state.assert_awaited_with(
        "!room:localhost",
        hooks.THREAD_TAGS_EVENT_TYPE,
        state_key=_tag_key("$pending", "pending-restart"),
    )
    ctx.put_room_state.assert_awaited_once_with(
        "!room:localhost",
        hooks.THREAD_TAGS_EVENT_TYPE,
        state_key=_tag_key("$pending", "pending-restart"),
        content={},
    )


@pytest.mark.asyncio
async def test_fresh_canonical_re_tag_is_not_erased_by_noncanonical_cleanup() -> None:
    """A re-tag MindRoom writes at the canonical key after a noncanonical scan must survive the clear."""
    scanned = {json.dumps(["$pending", "pending-restart"]): _RECORD}
    fresh = {"set_by": "agent", "set_at": "2026-11-19T00:05:00Z"}

    async def query(_room_id: str, _event_type: str, state_key: str | None = None) -> object:
        if state_key is None:
            return scanned
        return fresh if state_key == _tag_key("$pending", "pending-restart") else _RECORD

    ctx = SimpleNamespace(
        query_room_state=AsyncMock(side_effect=query),
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(return_value=True),
        logger=MagicMock(),
    )

    assert await hooks._notify_room_threads(ctx, "!room:localhost") == 1
    ctx.put_room_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_tag_is_kept_with_distinct_log() -> None:
    """A failed re-read keeps the tag and says so, rather than claiming it changed."""

    async def query(_room_id: str, _event_type: str, state_key: str | None = None) -> object:
        return {_tag_key("$pending", "pending-restart"): _RECORD} if state_key is None else None

    ctx = SimpleNamespace(
        query_room_state=AsyncMock(side_effect=query),
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(return_value=True),
        logger=MagicMock(),
    )

    await hooks._notify_room_threads(ctx, "!room:localhost")

    ctx.put_room_state.assert_not_awaited()
    ctx.logger.info.assert_any_call(
        "Could not re-read restart tag; keeping it",
        room_id="!room:localhost",
        thread_id="$pending",
    )


@pytest.mark.asyncio
async def test_tombstoned_and_legacy_tag_state_is_ignored() -> None:
    """Removed tags (empty content) and the old per-thread format should not wake threads."""
    ctx = SimpleNamespace(
        query_room_state=_room_state(
            {
                _tag_key("$removed", "pending-restart"): {},
                "$legacy": {"tags": {"pending-restart": {"set_by": "code"}}},
            },
        ),
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(return_value=True),
        logger=MagicMock(),
    )

    notified = await hooks._notify_room_threads(ctx, "!room:localhost")

    assert notified == 0
    ctx.send_message.assert_not_awaited()
    ctx.put_room_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_notification_keeps_pending_tag() -> None:
    """Failed send should leave state intact for next restart."""
    ctx = SimpleNamespace(
        query_room_state=_room_state(
            {_tag_key("$pending", "restart-pending"): _RECORD},
        ),
        send_message=AsyncMock(return_value=None),
        put_room_state=AsyncMock(return_value=True),
        logger=MagicMock(),
    )

    notified = await hooks._notify_room_threads(ctx, "!room:localhost")

    assert notified == 0
    ctx.put_room_state.assert_not_awaited()
    ctx.logger.warning.assert_called_once_with(
        "Failed to notify thread",
        room_id="!room:localhost",
        thread_id="$pending",
    )


@pytest.mark.asyncio
async def test_failed_state_clear_is_not_counted_as_notified() -> None:
    """Failed tag removal should not report a completed restart notification."""
    ctx = SimpleNamespace(
        query_room_state=_room_state(
            {_tag_key("$pending", "pending-restart"): _RECORD},
        ),
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(return_value=False),
        logger=MagicMock(),
    )

    notified = await hooks._notify_room_threads(ctx, "!room:localhost")

    assert notified == 0
    ctx.logger.info.assert_not_called()
    ctx.logger.warning.assert_called_once_with(
        "Failed to clear restart tag after notification",
        room_id="!room:localhost",
        thread_id="$pending",
        reason="false_result",
    )


@pytest.mark.asyncio
async def test_state_clear_exception_is_not_counted_as_notified() -> None:
    """State-write exceptions should preserve the pending notification for retry."""
    ctx = SimpleNamespace(
        query_room_state=_room_state(
            {_tag_key("$pending", "pending-restart"): _RECORD},
        ),
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(side_effect=RuntimeError("state unavailable")),
        logger=MagicMock(),
    )

    notified = await hooks._notify_room_threads(ctx, "!room:localhost")

    assert notified == 0
    ctx.logger.info.assert_not_called()
    ctx.logger.warning.assert_called_once_with(
        "Failed to clear restart tag after notification",
        room_id="!room:localhost",
        thread_id="$pending",
        reason="exception",
        exc_info=True,
    )


@pytest.mark.asyncio
async def test_ready_hook_scans_rooms_and_releases_claim_lock(tmp_path: Path) -> None:
    """Lifecycle entry point should visit all rooms and release its persistent claim lock."""
    ctx = SimpleNamespace(
        state_root=tmp_path,
        settings={},
        room_state_querier=object(),
        joined_room_ids=("!one:localhost", "!two:localhost"),
        logger=MagicMock(),
    )
    notify = AsyncMock(side_effect=[1, 0])

    with patch.object(hooks, "_notify_room_threads", notify):
        await hooks.notify_after_restart(ctx)

    assert notify.await_args_list == [
        call(ctx, "!one:localhost", hooks.PENDING_RESTART_TAGS, timeout_seconds=10),
        call(ctx, "!two:localhost", hooks.PENDING_RESTART_TAGS, timeout_seconds=10),
    ]
    claim_path = tmp_path / ".restart-claim"
    claim_fd = os.open(str(claim_path), os.O_RDWR)
    try:
        fcntl.flock(claim_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(claim_fd, fcntl.LOCK_UN)
        os.close(claim_fd)
    ctx.logger.info.assert_called_once_with("Restart-notify complete", notified_count=1)


@pytest.mark.asyncio
async def test_slow_room_does_not_block_later_restart_notices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled full-state read expires, and later rooms still get their notices."""
    monkeypatch.setattr(hooks, "_ROOM_TIMEOUT_SECONDS", 0.05)
    slow_room = "!slow:localhost"
    fast_rooms = tuple(f"!fast{i}:localhost" for i in range(5))
    monkeypatch.setattr(hooks, "_ROOM_RETRY_TIMEOUT_SECONDS", 0.1)
    stalled_reads_closed = 0
    key = _tag_key("$pending", "pending-restart")

    async def query(room_id: str, _event_type: str, state_key: str | None = None) -> object:
        nonlocal stalled_reads_closed
        if room_id == slow_room:
            try:
                await asyncio.Future()
            finally:
                stalled_reads_closed += 1
        return {key: _RECORD} if state_key is None else _RECORD

    ctx = SimpleNamespace(
        state_root=tmp_path,
        settings={},
        room_state_querier=object(),
        joined_room_ids=(slow_room, *fast_rooms),
        query_room_state=query,
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(return_value=True),
        logger=MagicMock(),
    )

    async with asyncio.timeout(1):
        await hooks.notify_after_restart(ctx)

    assert stalled_reads_closed == 2
    assert sorted(notice.kwargs["room_id"] for notice in ctx.send_message.await_args_list) == list(fast_rooms)
    assert sorted(clear.args[0] for clear in ctx.put_room_state.await_args_list) == list(fast_rooms)


@pytest.mark.asyncio
async def test_timed_out_discovery_is_retried_last_with_a_longer_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hooks, "_ROOM_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(hooks, "_ROOM_RETRY_TIMEOUT_SECONDS", 0.3)
    slow = "!slow:localhost"
    fast_rooms = [f"!fast{i}:localhost" for i in range(5)]
    reads: list[str] = []
    key = _tag_key("$pending", "pending-restart")

    async def query(room_id: str, _event_type: str, state_key: str | None = None) -> object:
        if state_key is not None:
            return _RECORD
        reads.append(room_id)
        if room_id == slow:
            if reads.count(slow) == 1:
                await asyncio.Future()
            await asyncio.sleep(0.06)  # Too slow for the initial deadline, fast enough for the retry.
        return {key: _RECORD}

    ctx = SimpleNamespace(
        state_root=tmp_path,
        settings={},
        room_state_querier=object(),
        joined_room_ids=(slow, *fast_rooms),
        query_room_state=query,
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(return_value=True),
        logger=MagicMock(),
    )
    async with asyncio.timeout(1):
        await hooks.notify_after_restart(ctx)

    assert reads == [slow, *fast_rooms, slow]
    assert [notice.kwargs["room_id"] for notice in ctx.send_message.await_args_list] == [*fast_rooms, slow]
    assert ctx.put_room_state.await_count == 6


@pytest.mark.asyncio
async def test_discovery_deadline_does_not_interrupt_cleanup_after_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Let the old per-room deadline expire during cleanup, then scan again without duplicate notices."""
    monkeypatch.setattr(hooks, "_ROOM_TIMEOUT_SECONDS", 0.05)
    threads = ["$first", "$second", "$third"]
    first_key = _tag_key("$first", "pending-restart")
    states = {_tag_key(thread, "pending-restart"): _RECORD for thread in threads}
    notices: list[str] = []
    stall_first_clear = True

    async def query(_room_id: str, _event_type: str, state_key: str | None = None) -> object:
        return dict(states) if state_key is None else states.get(state_key)

    async def send(*, thread_id: str, **_kwargs: object) -> str:
        notices.append(thread_id)
        return "$restart-notice"

    async def clear(_room_id: str, _event_type: str, *, state_key: str, content: dict[str, object]) -> bool:
        nonlocal stall_first_clear
        if state_key == first_key and stall_first_clear:
            stall_first_clear = False
            await asyncio.sleep(0.1)
        states[state_key] = content
        return True

    ctx = SimpleNamespace(
        state_root=tmp_path,
        settings={},
        room_state_querier=object(),
        joined_room_ids=("!room:localhost",),
        query_room_state=query,
        send_message=send,
        put_room_state=clear,
        logger=MagicMock(),
    )
    async with asyncio.timeout(1):
        await hooks.notify_after_restart(ctx)
        first_pass = notices.copy()
        await hooks.notify_after_restart(ctx)

    assert notices == threads
    assert first_pass == threads
    assert all(content == {} for content in states.values())


@pytest.mark.asyncio
async def test_ready_hook_bounds_parallel_room_reads(tmp_path: Path) -> None:
    """At startup several state reads can overlap, but not every room at once."""
    rooms = tuple(f"!room{i}:localhost" for i in range(8))
    completed = []
    active = peak = 0

    async def query(room_id: str, _event_type: str) -> dict[str, object]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        completed.append(room_id)
        return {}

    ctx = SimpleNamespace(
        state_root=tmp_path,
        settings={},
        room_state_querier=object(),
        joined_room_ids=rooms,
        query_room_state=query,
        logger=MagicMock(),
    )

    await hooks.notify_after_restart(ctx)

    assert sorted(completed) == list(rooms)
    assert 1 < peak <= 3


@pytest.mark.asyncio
async def test_ready_hook_does_not_steal_held_claim(tmp_path: Path) -> None:
    """Concurrent startup worker should leave active claim owner untouched."""
    claim_path = tmp_path / ".restart-claim"
    claim_fd = os.open(str(claim_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(claim_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ctx = SimpleNamespace(
        state_root=tmp_path,
        settings={},
        room_state_querier=object(),
        joined_room_ids=("!one:localhost",),
        logger=MagicMock(),
    )
    notify = AsyncMock(return_value=1)

    try:
        with patch.object(hooks, "_notify_room_threads", notify):
            await hooks.notify_after_restart(ctx)
        notify.assert_not_awaited()
        assert claim_path.exists()
    finally:
        fcntl.flock(claim_fd, fcntl.LOCK_UN)
        os.close(claim_fd)
        claim_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_ready_hook_honors_configured_tag(tmp_path: Path) -> None:
    """Configured restart tag should replace default aliases."""
    ctx = SimpleNamespace(
        state_root=tmp_path,
        settings={"tag": " Deploy-Pending "},
        room_state_querier=object(),
        joined_room_ids=("!room:localhost",),
        query_room_state=_room_state(
            {_tag_key("$pending", "deploy-pending"): _RECORD},
        ),
        send_message=AsyncMock(return_value="$restart-notice"),
        put_room_state=AsyncMock(return_value=True),
        logger=MagicMock(),
    )

    await hooks.notify_after_restart(ctx)

    ctx.send_message.assert_awaited_once_with(
        room_id="!room:localhost",
        text="🔄 Restart completed — this thread's `deploy-pending` changes are now live.",
        thread_id="$pending",
        trigger_dispatch=True,
    )
    ctx.put_room_state.assert_awaited_once_with(
        "!room:localhost",
        hooks.THREAD_TAGS_EVENT_TYPE,
        state_key=_tag_key("$pending", "deploy-pending"),
        content={},
    )
