"""Fire a scripted alert at the Triage agent to kick off an incident.

This is the demo's starting gun: it creates a temporary intake room containing
Triage, posts a SOC-style alert into it @mentioning Triage, and exits. Triage
reads the alert and creates the incident war room, recruiting the specialists
it needs (see ``shared/protocol.md`` §E).

    python -m injector.inject_alert INC-C
    python -m injector.inject_alert INC-A
    python -m injector.inject_alert INC-C --dry-run   # just print the message

How it posts
------------
The injector creates a fresh intake room, adds Triage as a participant, and
posts the alert there. This requires a Band **user** API key in
``BAND_INJECTOR_API_KEY`` (generate one in the Band UI; this is the SOC
analyst's identity). Without a key, the injector prints the ready-to-paste
message and the web-UI instructions instead — the validated manual fallback —
and exits cleanly.
"""

from __future__ import annotations

import argparse
import os
import sys

from shared.config import load_agent
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


def _post_via_rest(content: str, triage, api_key: str, base_url: str) -> str:
    """Create intake room with Triage, add Triage as participant, post alert.

    Returns the intake room ID (for reference only; the demo doesn't need it).
    Uses the human API (POST /me/chats, POST /me/chats/{id}/participants, etc.)
    since the injector uses a human (SOC analyst) API key.
    """
    import json
    from urllib.request import Request, urlopen
    from urllib.error import URLError

    def _rest_call(method: str, endpoint: str, payload: dict | None = None) -> dict:
        """Make a REST call to the Band API."""
        url = f"{base_url}/api/v1{endpoint}"
        headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        }
        req = Request(url, method=method, headers=headers)
        if payload:
            req.data = json.dumps(payload).encode("utf-8")
        try:
            with urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8")) if resp.status < 300 else {}
        except URLError as e:
            raise RuntimeError(f"REST call {method} {endpoint} failed: {e}") from e

    # Create intake room (human endpoint)
    room_data = _rest_call("POST", "/me/chats", {"name": "WarRoom Intake (Triage kickoff)"})
    room_id = room_data.get("id")
    if not room_id:
        raise RuntimeError(f"Failed to create room: {room_data}")

    # Add Triage as a participant to the intake room
    _rest_call("POST", f"/me/chats/{room_id}/participants",
               {"agent_id": triage.agent_id})

    # Post alert @mentioning Triage
    handle = (triage.handle or "@merolavtech/triage").lstrip("@")
    _rest_call("POST", f"/me/chats/{room_id}/messages", {
        "content": content,
        "mentions": [{"id": triage.agent_id, "handle": handle, "name": "triage"}],
    })

    return room_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fire an alert at the Triage agent.")
    parser.add_argument("incident", nargs="?", default="INC-C",
                        help="Alert alias/id (INC-A, INC-B, INC-C). Default: INC-C.")
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
    api_key = os.getenv("BAND_INJECTOR_API_KEY")
    base_url = os.getenv("BAND_REST_URL", DEFAULT_BASE_URL)

    print(f"[injector] incident={args.incident.upper()}")

    if args.dry_run or not api_key:
        if not args.dry_run:
            print("[injector] no auto-post: missing BAND_INJECTOR_API_KEY "
                  "(a Band user API key)")
        print("[injector] Paste this into a Band room with Triage "
              "(it @mentions Triage):\n")
        print("-" * 72)
        print(content)
        print("-" * 72)
        return 0

    try:
        room_id = _post_via_rest(content, triage, api_key, base_url)
    except Exception as e:  # noqa: BLE001 — surface any REST/auth error clearly
        print(f"[injector] POST failed: {type(e).__name__}: {e}", file=sys.stderr)
        print("[injector] Falling back to manual paste:\n")
        print(content)
        return 1

    print(f"[injector] created intake room {room_id} with Triage and posted alert.")
    print("[injector] Triage will read it, classify it, and create the incident room.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
