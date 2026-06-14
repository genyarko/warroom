"""Unit tests for the band-sdk 0.2.11 ack-loop patches (shared/sdk_patches.py).

No network: the REST client and link are faked. Verifies (1) mark_processed
recovers from a 422 by re-marking processing then processed, and (2) the
_get_next_message loop guard force-acks and breaks after a stuck message is
re-delivered N times.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from shared.sdk_patches import _LOOP_GUARD_THRESHOLD, apply_sdk_patches


class _Err422(Exception):
    status_code = 422


class _FakeMessagesClient:
    def __init__(self, fail_processed_once: bool = False):
        self.calls: list[str] = []
        self._fail_processed_once = fail_processed_once

    async def mark_agent_message_processing(self, *, chat_id, id, request_options):
        self.calls.append("processing")

    async def mark_agent_message_processed(self, *, chat_id, id, request_options):
        if self._fail_processed_once:
            self._fail_processed_once = False
            self.calls.append("processed-422")
            raise _Err422("Validation failed")
        self.calls.append("processed-ok")


def _patched_link():
    apply_sdk_patches()
    import thenvoi.platform.link as link_mod

    return link_mod.ThenvoiLink


def test_mark_processed_recovers_from_422():
    Link = _patched_link()
    msgs = _FakeMessagesClient(fail_processed_once=True)
    fake = types.SimpleNamespace(rest=types.SimpleNamespace(agent_api_messages=msgs))

    asyncio.run(Link.mark_processed(fake, "room-1", "msg-1"))

    # First processed -> 422, then processing re-established, then processed OK.
    assert msgs.calls == ["processed-422", "processing", "processed-ok"]


def test_mark_processed_happy_path_no_extra_calls():
    Link = _patched_link()
    msgs = _FakeMessagesClient(fail_processed_once=False)
    fake = types.SimpleNamespace(rest=types.SimpleNamespace(agent_api_messages=msgs))

    asyncio.run(Link.mark_processed(fake, "room-1", "msg-1"))

    assert msgs.calls == ["processed-ok"]  # no recovery path when it succeeds


class _FakeLink:
    def __init__(self, msg_id: str):
        self._msg = types.SimpleNamespace(id=msg_id)
        self.acked: list[str] = []

    async def get_next_message(self, room_id):
        return self._msg  # server keeps re-delivering the same message

    async def mark_processing(self, room_id, msg_id):
        self.acked.append("processing")

    async def mark_processed(self, room_id, msg_id):
        self.acked.append("processed")


class _FakeTracker:
    def __init__(self):
        self.perm_failed: list[str] = []

    def mark_permanently_failed(self, msg_id):
        self.perm_failed.append(msg_id)


def _patched_execution():
    apply_sdk_patches()
    from thenvoi.runtime.execution import ExecutionContext

    return ExecutionContext


def test_loop_guard_breaks_and_force_acks_a_stuck_message():
    ExecutionContext = _patched_execution()
    link = _FakeLink("stuck-1")
    tracker = _FakeTracker()
    fake = types.SimpleNamespace(
        link=link, room_id="room-1", _retry_tracker=tracker
    )

    async def run():
        results = []
        # The server re-delivers the same id every time; the guard must trip.
        for _ in range(_LOOP_GUARD_THRESHOLD):
            results.append(await ExecutionContext._get_next_message(fake))
        return results

    results = asyncio.run(run())

    # First THRESHOLD-1 fetches return the message; the THRESHOLD-th returns None
    # (loop broken) and the message is force-acked + marked permanently failed.
    assert all(r is not None for r in results[: _LOOP_GUARD_THRESHOLD - 1])
    assert results[-1] is None
    assert link.acked == ["processing", "processed"]
    assert tracker.perm_failed == ["stuck-1"]


def test_loop_guard_resets_on_distinct_ids():
    ExecutionContext = _patched_execution()
    tracker = _FakeTracker()

    class _RotatingLink(_FakeLink):
        def __init__(self):
            super().__init__("a")
            self._n = 0

        async def get_next_message(self, room_id):
            self._n += 1
            return types.SimpleNamespace(id=f"msg-{self._n}")  # always distinct

    link = _RotatingLink()
    fake = types.SimpleNamespace(link=link, room_id="room-1", _retry_tracker=tracker)

    async def run():
        out = []
        for _ in range(_LOOP_GUARD_THRESHOLD + 3):
            out.append(await ExecutionContext._get_next_message(fake))
        return out

    out = asyncio.run(run())

    # Distinct ids never trip the guard: all returned, nothing force-acked.
    assert all(r is not None for r in out)
    assert link.acked == []
    assert tracker.perm_failed == []


# --- Fix 3: create_chatroom repurposed as deterministic recruiter ----------

class _RecruitTools:
    """Stands in for the room-bound AgentTools instance."""
    def __init__(self):
        self.room_id = "alert-room"
        self.added = []

    async def add_participant(self, identifier, role="member"):
        self.added.append(identifier)
        return {"id": identifier}


def test_create_chatroom_recruits_team_into_current_room():
    import shared.sdk_patches as P
    from thenvoi.runtime.tools import AgentTools
    P.apply_sdk_patches()
    team = ["@x/threat-intel", "@x/compliance", "@x/commander", "@x/facilitator"]
    P._TEAM_CACHE = team
    fake = _RecruitTools()

    rid = asyncio.run(AgentTools.create_chatroom(fake, "WarRoom-INC-C"))

    assert rid == "alert-room"          # returns the CURRENT room, not a new one
    assert fake.added == team           # whole team recruited, deterministically
    assert fake._wr_recruited is True


def test_create_chatroom_recruit_is_idempotent():
    import shared.sdk_patches as P
    from thenvoi.runtime.tools import AgentTools
    P.apply_sdk_patches()
    P._TEAM_CACHE = ["@x/threat-intel", "@x/compliance"]
    fake = _RecruitTools()

    async def run():
        await AgentTools.create_chatroom(fake)
        await AgentTools.create_chatroom(fake)  # repeat call (gpt-4o duplicate)
        return fake.added

    added = asyncio.run(run())
    assert added == ["@x/threat-intel", "@x/compliance"]  # added once, no dupes
