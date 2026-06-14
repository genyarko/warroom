"""Runtime patches for band-sdk 0.2.11 message-ack bugs.

Two related defects make an agent spin forever on a single message, logging
"Catching up missed message <id> via /next resync" indefinitely. This stalled
the INC-C demo twice (Compliance, then Commander). Root cause (verified against
the live API): the SDK lifecycle is mark_processing -> handle -> mark_processed,
and the server's `/processed` endpoint returns HTTP 422 when there is no active
processing attempt. On the FIRST message the attempt is gone by the time
mark_processed runs (it expired during the slow cold-start LLM call, and/or the
startup /next sync raced the WebSocket path for the same first message). The SDK
*swallows* that 422, marks the message processed only locally, and never acks it
server-side -- so /next returns it forever. The local dedupe stops re-handling,
so the spin makes no LLM calls (no token cost) but never ends.

Both fixes are monkeypatches (no SDK fork). Call ``apply_sdk_patches()`` once,
before ``Agent.create``.

1. ``ThenvoiLink.mark_processed`` -- on 422, re-establish the processing attempt
   (``mark_processing``) and retry ``mark_processed`` once. Fixes the cause: the
   message gets acked, so /next stops returning it.
2. ``ExecutionContext._get_next_message`` -- loop guard. Both catch-up loops
   (``_sync_via_next`` and ``_resync_pending_messages``) fetch through this one
   method, so if /next returns the same id N times in a row we force-ack it, mark
   it permanently-failed locally, and return None to break the loop. Defense in
   depth in case the 422 recovery itself fails.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("warroom.sdk_patches")

# After this many consecutive identical /next results, treat it as a stuck loop.
_LOOP_GUARD_THRESHOLD = 5

# Repeat create_chatroom calls within this many seconds are treated as the same
# incident room (Triage/LLMs sometimes call create_chatroom twice in one turn,
# fragmenting the team across two rooms). New incidents are minutes+ apart, so a
# short window de-dupes the quirk without blocking legitimate new rooms.
import os
import time as _time
_ROOM_DEDUP_SECONDS = int(os.getenv("WARROOM_ROOM_DEDUP_SECONDS", "180"))
_orig_create_chatroom = None  # set at patch time; swappable for tests

_APPLIED = False


def _is_422(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 422:
        return True
    try:
        from thenvoi_rest.errors.unprocessable_entity_error import (
            UnprocessableEntityError,
        )
    except Exception:  # pragma: no cover - import shape varies
        return False
    return isinstance(exc, UnprocessableEntityError)


def apply_sdk_patches() -> None:
    """Idempotently install the band-sdk 0.2.11 ack-loop fixes."""
    global _APPLIED
    if _APPLIED:
        return

    import thenvoi.platform.link as link_mod
    from thenvoi.runtime.execution import ExecutionContext

    req_opts = link_mod.DEFAULT_REQUEST_OPTIONS
    Link = link_mod.ThenvoiLink

    # --- Fix 1: 422-recovering mark_processed --------------------------------
    async def mark_processed(self, room_id: str, message_id: str) -> None:
        msgs = self.rest.agent_api_messages
        try:
            await msgs.mark_agent_message_processed(
                chat_id=room_id, id=message_id, request_options=req_opts
            )
            return
        except Exception as e:  # noqa: BLE001 - branch on 422 vs other below
            if not _is_422(e):
                logger.warning(
                    "Failed to mark message %s as processed: %s", message_id, e
                )
                return
        # 422 == "no active processing attempt". Re-establish it and retry once.
        try:
            await msgs.mark_agent_message_processing(
                chat_id=room_id, id=message_id, request_options=req_opts
            )
            await msgs.mark_agent_message_processed(
                chat_id=room_id, id=message_id, request_options=req_opts
            )
            logger.info(
                "Recovered 422 ack for message %s (re-marked processing then "
                "processed)",
                message_id,
            )
        except Exception as e:  # noqa: BLE001 - last-resort, don't crash the loop
            logger.warning(
                "422-recovery failed for message %s: %s", message_id, e
            )

    Link.mark_processed = mark_processed

    # --- Fix 2: loop guard on the shared /next fetch -------------------------
    async def _get_next_message(self):
        msg = await self.link.get_next_message(self.room_id)
        if msg is None:
            self._wr_last_next_id = None
            self._wr_last_next_count = 0
            return None

        last = getattr(self, "_wr_last_next_id", None)
        count = getattr(self, "_wr_last_next_count", 0)
        count = count + 1 if msg.id == last else 1
        self._wr_last_next_id = msg.id
        self._wr_last_next_count = count

        if count >= _LOOP_GUARD_THRESHOLD:
            logger.warning(
                "Loop guard: /next returned %s %d times consecutively; "
                "force-acking and breaking the resync loop",
                msg.id,
                count,
            )
            try:
                await self.link.mark_processing(self.room_id, msg.id)
                await self.link.mark_processed(self.room_id, msg.id)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Loop guard force-ack failed for %s: %s", msg.id, e
                )
            try:
                self._retry_tracker.mark_permanently_failed(msg.id)
            except Exception:  # noqa: BLE001 - best effort
                pass
            self._wr_last_next_id = None
            self._wr_last_next_count = 0
            return None  # break the catch-up loop

        return msg

    ExecutionContext._get_next_message = _get_next_message

    # --- Fix 3: idempotent create_chatroom (anti duplicate-room split) --------
    # Triage (esp. gpt-4o) sometimes calls create_chatroom twice in one turn,
    # creating two incident rooms and fragmenting the team (specialists briefed in
    # room A, Commander ends up in room B). Within a short window, return the room
    # already created instead of making a duplicate.
    global _orig_create_chatroom
    from thenvoi.runtime.tools import AgentTools
    _orig_create_chatroom = AgentTools.create_chatroom

    async def create_chatroom(self, task_id=None):
        now = _time.monotonic()
        last = getattr(self, "_wr_last_room", None)  # (room_id, monotonic_ts)
        if last and (now - last[1]) < _ROOM_DEDUP_SECONDS:
            logger.warning(
                "Idempotent create_chatroom: reusing room %s (a duplicate "
                "create within %ss was suppressed to avoid splitting the team)",
                last[0], _ROOM_DEDUP_SECONDS)
            return last[0]
        room_id = await _orig_create_chatroom(self, task_id)
        self._wr_last_room = (room_id, now)
        return room_id

    AgentTools.create_chatroom = create_chatroom

    _APPLIED = True
    logger.info("band-sdk 0.2.11 ack-loop + idempotent-room patches applied")
