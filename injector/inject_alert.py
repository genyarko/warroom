"""Fire a scripted alert at the Triage agent to kick off an incident.

This is the demo's starting gun: it posts a SOC-style alert into the WarRoom
incident room, @mentioning Triage, exactly as a human SOC analyst would. Triage
then classifies it and recruits the specialists it needs (see
``shared/protocol.md`` §E).

    python -m injector.inject_alert INC-C
    python -m injector.inject_alert INC-A --room <room-uuid>
    python -m injector.inject_alert INC-C --dry-run   # just print the message

How it posts
------------
The room must already contain the **human + Triage** (created fresh per run; put
its id in ``agent_config.yaml`` under ``room.default_room_id``, or pass
``--room``). Posting needs a Band **user** API key in ``BAND_INJECTOR_API_KEY``
(generate one in the Band UI; this is the SOC analyst's identity). Without a key
or a room, the injector prints the ready-to-paste message and the web-UI
instructions instead — the validated manual fallback — and exits cleanly.
"""

from __future__ import annotations

import argparse
import os
import sys

from shared.config import default_room_id, load_agent
from shared.mock_data import list_alerts, load_alert

DEFAULT_BASE_URL = "https://app.thenvoi.com"  # SDK default; agents use the same


def build_alert_message(incident: str) -> tuple[str, dict]:
    """Return (human-readable alert text, the loaded alert dict)."""
    alert = load_alert(incident)
    inc_id = alert.get("incident_id", incident)
    indicators = ", ".join(alert.get("indicators", [])) or "none"
    obs = alert.get("observations", [])
    obs_block = "\n".join(f"  - {o}" for o in obs)
    text = (
        f"@Triage *** NEW SECURITY ALERT *** {inc_id}\n"
        f"{alert.get('title', '')}\n"
        f"Source: {alert.get('source', 'unknown')} | "
        f"Severity hint: {alert.get('severity_hint', 'unknown')} | "
        f"Category: {alert.get('category_hint', 'unknown')}\n"
        f"Affected host: {alert.get('asset_id', 'unknown')}\n"
        f"Indicators: {indicators}\n"
        f"Observations:\n{obs_block}\n\n"
        f"Triage this incident (alias {incident.upper()}): classify it and "
        f"recruit the specialists you need."
    )
    return text, alert


def _post_via_rest(room_id: str, content: str, triage, api_key: str, base_url: str) -> None:
    from thenvoi_rest import (
        ChatMessageRequest,
        ChatMessageRequestMentionsItem,
        RestClient,
    )

    client = RestClient(api_key=api_key, base_url=base_url)
    handle = (triage.handle or "@merolavtech/triage").lstrip("@")
    mention = ChatMessageRequestMentionsItem(id=triage.agent_id, handle=handle, name="triage")
    msg = ChatMessageRequest(content=content, mentions=[mention])
    client.human_api_messages.send_my_chat_message(chat_id=room_id, message=msg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fire an alert at the Triage agent.")
    parser.add_argument("incident", nargs="?", default="INC-C",
                        help="Alert alias/id (INC-A, INC-B, INC-C). Default: INC-C.")
    parser.add_argument("--room", default=None,
                        help="Incident room id. Default: BAND_ROOM_ID env or "
                             "room.default_room_id in agent_config.yaml.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the message and exit without posting.")
    args = parser.parse_args(argv)

    try:
        content, _alert = build_alert_message(args.incident)
    except (KeyError, FileNotFoundError) as e:
        print(f"[injector] {e}\n[injector] available alerts: {list_alerts()}",
              file=sys.stderr)
        return 2

    triage = load_agent("triage")  # also loads .env so BAND_INJECTOR_API_KEY is set
    room_id = args.room or os.getenv("BAND_ROOM_ID") or default_room_id()
    api_key = os.getenv("BAND_INJECTOR_API_KEY")
    base_url = os.getenv("BAND_REST_URL", DEFAULT_BASE_URL)

    print(f"[injector] incident={args.incident.upper()} room={room_id or '(none)'}")

    if args.dry_run or not room_id or not api_key:
        if not args.dry_run:
            missing = []
            if not room_id:
                missing.append("a room (set room.default_room_id or pass --room)")
            if not api_key:
                missing.append("a user key (set BAND_INJECTOR_API_KEY)")
            print(f"[injector] no auto-post: missing {', and '.join(missing)}.")
        print("[injector] Paste this into the incident room in the Band UI "
              "(it @mentions Triage):\n")
        print("-" * 72)
        print(content)
        print("-" * 72)
        return 0

    try:
        _post_via_rest(room_id, content, triage, api_key, base_url)
    except Exception as e:  # noqa: BLE001 — surface any REST/auth error clearly
        print(f"[injector] POST failed: {type(e).__name__}: {e}", file=sys.stderr)
        print("[injector] Falling back to manual paste:\n")
        print(content)
        return 1

    print(f"[injector] alert posted to room {room_id}. Triage is on the case.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
