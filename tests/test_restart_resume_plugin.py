# ruff: noqa: INP001
"""Behavior tests for restart-resume plugin."""

from __future__ import annotations

import fcntl
import os
import sys
from importlib import util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks import EVENT_BOT_READY
from mindroom.hooks.decorators import get_hook_metadata


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


def test_hook_metadata_is_router_scoped() -> None:
    """Restart scan should run once through router lifecycle."""
    metadata = get_hook_metadata(hooks.notify_after_restart)

    assert metadata is not None
    assert metadata.event_name == EVENT_BOT_READY
    assert metadata.hook_name == "notify-after-restart"
    assert metadata.agents == (ROUTER_AGENT_NAME,)
    assert metadata.priority == 100
    assert metadata.timeout_ms == 30000


@pytest.mark.asyncio
async def test_pending_thread_is_notified_and_both_aliases_are_cleared() -> None:
    """Successful notification should preserve unrelated tags and clear restart aliases."""
    ctx = SimpleNamespace(
        query_room_state=AsyncMock(
            return_value={
                "$pending": {
                    "tags": {
                        "pending-restart": {"set_by": "code"},
                        "restart-pending": {"set_by": "code"},
                        "keep-me": {"set_by": "user"},
                    },
                },
                "$untouched": {"tags": {"keep-me": {"set_by": "user"}}},
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
    ctx.put_room_state.assert_awaited_once_with(
        "!room:localhost",
        hooks.THREAD_TAGS_EVENT_TYPE,
        state_key="$pending",
        content={"tags": {"keep-me": {"set_by": "user"}}},
    )


@pytest.mark.asyncio
async def test_failed_notification_keeps_pending_tag() -> None:
    """Failed send should leave state intact for next restart."""
    ctx = SimpleNamespace(
        query_room_state=AsyncMock(
            return_value={"$pending": {"tags": {"restart-pending": {"set_by": "code"}}}},
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
        query_room_state=AsyncMock(
            return_value={"$pending": {"tags": {"pending-restart": {"set_by": "code"}}}},
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
    )


@pytest.mark.asyncio
async def test_ready_hook_scans_rooms_and_releases_claim(tmp_path: Path) -> None:
    """Lifecycle entry point should visit all rooms and clean its claim file."""
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
        call(ctx, "!one:localhost", hooks.PENDING_RESTART_TAGS),
        call(ctx, "!two:localhost", hooks.PENDING_RESTART_TAGS),
    ]
    assert not (tmp_path / ".restart-claim").exists()
    ctx.logger.info.assert_called_once_with("Restart-notify complete", notified_count=1)


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
        settings={"tag": "deploy-pending"},
        room_state_querier=object(),
        joined_room_ids=("!room:localhost",),
        query_room_state=AsyncMock(
            return_value={"$pending": {"tags": {"deploy-pending": {"set_by": "user"}}}},
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
