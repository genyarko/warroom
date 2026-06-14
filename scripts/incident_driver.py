"""Facilitator watchdog — keeps a WarRoom incident from stalling.

WarRoom agents are purely reactive: an agent runs its LLM only when a Band
message @mentions it, so the incident advances as a chain of @mentions. If a
message fails to name the next actor (or an agent analyses but never posts), the
room goes permanently idle. This driver is the out-of-band guarantee against
that: it watches the incident room and, when it goes quiet before RESOLUTION,
posts a targeted nudge AS THE FACILITATOR @mentioning whoever should act next.

It posts as a dedicated ``facilitator`` agent (register it on app.band.ai and
add it to agent_config.yaml; Triage adds it to every incident room). The driver
does NOT run an LLM — it only reads the transcript and posts deterministic
nudges, escalating to the human after repeated stalls.

Usage (from repo root, after the agents are up and an incident is live):
    .venv\\Scripts\\python.exe scripts\\incident_driver.py            # auto-detect room
    .venv\\Scripts\\python.exe scripts\\incident_driver.py --room <id>
    .venv\\Scripts\\python.exe scripts\\incident_driver.py --idle 45 --poll 20
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from shared.config import load_agent  # noqa: E402

DEFAULT_BASE_URL = "https://app.band.ai"
SPECIALIST_ROLES = ("threat_intel", "compliance")  # who owes a FINDING in INC-C
TERMINAL_TYPES = {"RESOLUTION", "CLOSE"}

_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass(frozen=True)
class Nudge:
    target: str   # a role key ("commander"/"threat_intel"/...) or "human"
    ask: str      # the instruction to post


@dataclass(frozen=True)
class ParsedMsg:
    role: str | None        # mapped role of the sender, or None (human/unknown)
    is_human: bool
    type: str | None        # protocol block type, or None (no/invalid block)


def parse_block_type(content: str) -> str | None:
    """Return the `type` of the last fenced json protocol block, if any."""
    blocks = _JSON_BLOCK.findall(content or "")
    for raw in reversed(blocks):
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        t = obj.get("type")
        if isinstance(t, str):
            return t.upper()
    return None


def decide_nudge(parsed: list[ParsedMsg],
                 recruited: tuple[str, ...] = SPECIALIST_ROLES) -> Nudge | None:
    """Pure state machine: given the incident transcript (oldest→newest),
    return the next nudge, or None if nothing is owed / the incident is done.

    Mirrors shared/protocol.md §E.2: BRIEF → FINDINGs → SIGNOFF_REQUEST →
    SIGNOFF/VETO → (ESCALATION → human ruling) → RESOLUTION.
    """
    types = [p.type for p in parsed if p.type]
    if any(t in TERMINAL_TYPES for t in types):
        return None  # incident closed — nothing to drive

    has = set(types)

    # Helper: which specialists have posted a given block type.
    def posted(block: str) -> set[str]:
        return {p.role for p in parsed if p.type == block and p.role}

    if "BRIEF" not in has:
        return Nudge("triage", "classify the alert and post the BRIEF, recruiting "
                     "the specialists, so the incident can start.")

    # Phase: gather FINDINGs.
    missing_findings = [r for r in recruited if r not in posted("FINDING")]
    if "SIGNOFF_REQUEST" not in has:
        if missing_findings:
            return Nudge(missing_findings[0],
                         "post your FINDING (with thenvoi_send_message) "
                         "@mentioning @merolavtech/commander so the Commander can "
                         "issue the SIGNOFF_REQUEST.")
        return Nudge("commander", "all specialist FINDINGs are in — post your "
                     "SIGNOFF_REQUEST @mentioning each specialist.")

    # Phase: collect sign-offs / veto on the request.
    decided = posted("SIGNOFF") | posted("VETO")
    missing_decisions = [r for r in recruited if r not in decided]
    if missing_decisions:
        return Nudge(missing_decisions[0],
                     "respond to the Commander's SIGNOFF_REQUEST with a SIGNOFF "
                     "or a VETO @mentioning @merolavtech/commander.")

    # Phase: veto/deadlock → escalation → human ruling → resolution.
    if "VETO" in has and "ESCALATION" not in has:
        return Nudge("commander", "Compliance has VETOed the contested action — "
                     "post one ESCALATION @mentioning the human CISO (@merolavtech).")

    if "ESCALATION" in has:
        # Has the human ruled since the escalation?
        esc_idx = max(i for i, p in enumerate(parsed) if p.type == "ESCALATION")
        human_ruled = any(p.is_human for p in parsed[esc_idx + 1:])
        if not human_ruled:
            return Nudge("human", "the incident is escalated to you — please post "
                         "your decision so the Commander can resolve it.")
        return Nudge("commander", "the CISO has ruled — execute the approved "
                     "actions and post the RESOLUTION.")

    # All signed off, no veto, no resolution yet.
    return Nudge("commander", "all sign-offs are in — execute the plan and post "
                 "the RESOLUTION.")


# --------------------------------------------------------------------------- I/O

def _roster() -> dict[str, dict]:
    roster = {}
    for role in ("triage", "threat_intel", "compliance", "commander"):
        try:
            c = load_agent(role)
            roster[role] = {"id": c.agent_id, "handle": c.handle.lstrip("@"),
                            "name": role}
        except Exception:
            pass
    return roster


def _list_messages(client, room_id):
    msgs, page = [], 1
    while True:
        resp = client.agent_api_messages.list_agent_messages(
            chat_id=room_id, status="all", page=page, page_size=100)
        msgs.extend(resp.data)
        meta = resp.metadata
        if page >= (getattr(meta, "total_pages", None) or 1):
            break
        page += 1
    # oldest -> newest
    return sorted(msgs, key=lambda m: getattr(m, "inserted_at", "") or "")


def _parse(messages, roster: dict[str, dict]) -> list[ParsedMsg]:
    id_to_role = {v["id"]: r for r, v in roster.items()}
    out = []
    for m in messages:
        is_human = getattr(m, "sender_type", "") == "User"
        role = id_to_role.get(getattr(m, "sender_id", None))
        out.append(ParsedMsg(role=role, is_human=is_human,
                             type=parse_block_type(getattr(m, "content", ""))))
    return out


def _discover_room(client, explicit: str | None):
    if explicit:
        return explicit
    resp = client.agent_api_chats.list_agent_chats(page=1, page_size=100)
    rooms = sorted(resp.data, key=lambda r: getattr(r, "updated_at", "") or "",
                   reverse=True)
    if not rooms:
        return None
    return rooms[0].id  # most recently active room the Facilitator is in


def _human_mention(messages, roster):
    """Find the human (CISO) id/name from the transcript, for @mentioning."""
    for m in messages:
        if getattr(m, "sender_type", "") == "User":
            return {"id": m.sender_id, "handle": "merolavtech",
                    "name": getattr(m, "sender_name", "CISO")}
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Facilitator watchdog for a WarRoom incident.")
    ap.add_argument("--room", default=None, help="Incident room id (default: auto-detect).")
    ap.add_argument("--idle", type=int, default=45, help="Seconds of silence before nudging.")
    ap.add_argument("--poll", type=int, default=20, help="Seconds between polls.")
    ap.add_argument("--max-nudges-per-state", type=int, default=3,
                    help="Stop nudging the same state after N tries (then escalate to human).")
    args = ap.parse_args(argv)

    try:
        from thenvoi_rest import RestClient
        from thenvoi_rest.types.chat_message_request import ChatMessageRequest
        from thenvoi_rest.types.chat_message_request_mentions_item import (
            ChatMessageRequestMentionsItem,
        )
    except ImportError:
        print("[driver] thenvoi_rest not installed in this interpreter.", file=sys.stderr)
        return 2

    try:
        fac = load_agent("facilitator")
    except Exception as e:
        print(f"[driver] facilitator agent not configured: {e}\n"
              "[driver] Register a 'Facilitator' External Agent on app.band.ai and add it "
              "to agent_config.yaml (see agent_config.yaml.example).", file=sys.stderr)
        return 2

    client = RestClient(api_key=fac.api_key, base_url=DEFAULT_BASE_URL)
    roster = _roster()

    room = _discover_room(client, args.room)
    if not room:
        print("[driver] no incident room yet — waiting for Triage to create one and "
              "add the Facilitator (Ctrl-C to stop)...")
        while not room:
            time.sleep(args.poll)
            try:
                room = _discover_room(client, args.room)
            except Exception as e:
                print(f"[driver] discover error: {e}", file=sys.stderr)
    print(f"[driver] watching room {room} | idle>{args.idle}s | poll {args.poll}s")

    def post(text: str, mentions: list[dict]) -> None:
        items = [ChatMessageRequestMentionsItem(id=m["id"], handle=m["handle"],
                                                name=m["name"]) for m in mentions]
        handles = " ".join(f"@{m['handle']}" for m in mentions)
        client.agent_api_messages.create_agent_chat_message(
            chat_id=room,
            message=ChatMessageRequest(content=f"{handles} [Facilitator] {text}",
                                       mentions=items))

    last_seen_count = 0
    quiet_since = time.time()
    state_key = None
    state_nudges = 0

    while True:
        try:
            messages = _list_messages(client, room)
        except Exception as e:
            print(f"[driver] list error: {e}", file=sys.stderr)
            time.sleep(args.poll)
            continue

        if len(messages) != last_seen_count:
            last_seen_count = len(messages)
            quiet_since = time.time()  # new activity → reset the idle timer

        parsed = _parse(messages, roster)
        if any(p.type in TERMINAL_TYPES for p in parsed):
            print("[driver] incident reached RESOLUTION/CLOSE — done.")
            return 0

        idle_for = time.time() - quiet_since
        if idle_for < args.idle:
            time.sleep(args.poll)
            continue

        nudge = decide_nudge(parsed)
        if nudge is None:
            time.sleep(args.poll)
            continue

        # Track repeated nudging of the same state; escalate to human if stuck.
        key = (nudge.target, nudge.ask)
        if key == state_key:
            state_nudges += 1
        else:
            state_key, state_nudges = key, 1

        target = nudge.target
        if state_nudges > args.max_nudges_per_state and target != "human":
            target, ask = "human", (f"the incident is stuck waiting on "
                                     f"{nudge.target} ({nudge.ask}). Please intervene.")
        else:
            ask = nudge.ask

        if target == "human":
            hm = _human_mention(messages, roster)
            mentions = [hm] if hm else []
        else:
            r = roster.get(target)
            mentions = [{"id": r["id"], "handle": r["handle"], "name": r["name"]}] if r else []

        if not mentions:
            print(f"[driver] cannot resolve mention for '{target}'; skipping.", file=sys.stderr)
            time.sleep(args.poll)
            continue

        try:
            post(ask, mentions)
            print(f"[driver] nudged {target} (idle {idle_for:.0f}s, try {state_nudges}): {ask[:80]}")
        except Exception as e:
            print(f"[driver] post error: {e}", file=sys.stderr)

        quiet_since = time.time()  # don't re-nudge until the next idle window
        time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
