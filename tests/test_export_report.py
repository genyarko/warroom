"""Tests for the Phase 6 incident-report exporter (scripts/export_report).

No network: build_report and _protocol_blocks are pure; messages are faked.
"""

from __future__ import annotations

import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from export_report import _protocol_blocks, build_report  # noqa: E402


def _m(content, sender_name="Agent", sender_type="Agent", ts="2026-06-14T10:00:00",
       message_type="text"):
    return types.SimpleNamespace(content=content, sender_name=sender_name,
                                 sender_type=sender_type, inserted_at=ts,
                                 message_type=message_type)


def _block(d):
    import json
    return f"text before\n```json\n{json.dumps(d)}\n```"


def test_protocol_blocks_extracts_typed_only():
    assert _protocol_blocks(_block({"type": "FINDING", "incident": "INC-C"}))[0]["type"] == "FINDING"
    assert _protocol_blocks("no block here") == []
    assert _protocol_blocks('```json\n{"no":"type"}\n```') == []  # not a protocol block


def _incident_messages():
    return [
        _m(_block({"type": "BRIEF", "incident": "INC-C-2026-0042", "severity": "critical",
                   "summary": "Ransomware on srv-db-01; recruiting Intel + Compliance."}),
           sender_name="triage", ts="2026-06-14T10:00:00"),
        _m(_block({"type": "FINDING", "incident": "INC-C-2026-0042",
                   "summary": "BlackHaze, lateral movement; wipe required.",
                   "evidence": ["185.220.101.47 = C2"]}),
           sender_name="Threat Intel", ts="2026-06-14T10:01:00"),
        _m(_block({"type": "VETO", "incident": "INC-C-2026-0042",
                   "summary": "Block wipe — legal hold.", "regulation": "GDPR-ART-33",
                   "deadline_utc": "2026-06-17T10:00:00+00:00"}),
           sender_name="Compliance", ts="2026-06-14T10:02:00"),
        _m(_block({"type": "ESCALATION", "incident": "INC-C-2026-0042",
                   "summary": "Wipe needs your authorization."}),
           sender_name="Commander", ts="2026-06-14T10:03:00"),
        _m("Authorize the wipe after imaging.", sender_name="George N",
           sender_type="User", ts="2026-06-14T10:04:00"),
        _m(_block({"type": "RESOLUTION", "incident": "INC-C-2026-0042",
                   "summary": "Isolated, imaged, wiped; GDPR clock running."}),
           sender_name="Commander", ts="2026-06-14T10:05:00"),
    ]


def test_build_report_has_all_sections_and_highlights_human():
    actions = [{"timestamp_utc": "2026-06-14T10:05:00", "action": "wipe_host",
                "asset_id": "srv-db-01", "reason": "per CISO authorization"}]
    clocks = [{"regulation": "GDPR-ART-33", "deadline_utc": "2026-06-17T10:00:00+00:00",
               "window": "72 hours", "t_minus": "T-minus 71h 0m", "breached": False}]
    report, incident, alias = build_report(_incident_messages(), actions, clocks,
                                           room_id="room-1")
    assert incident == "INC-C-2026-0042" and alias == "INC-C"
    # sections present
    for heading in ("# Incident report", "## Executive summary",
                    "## Decision timeline", "## Regulatory obligations & clock",
                    "## Actions taken", "## Appendix"):
        assert heading in report
    # exec summary picked up severity + outcome (from RESOLUTION)
    assert "critical" in report
    assert "GDPR clock running" in report
    # timeline includes the protocol beats + the human ruling, highlighted
    assert "FINDING — Threat Intel" in report
    assert "VETO — Compliance" in report
    assert "HUMAN RULING" in report
    assert "Authorize the wipe after imaging" in report
    # regulatory table + action
    assert "GDPR-ART-33" in report and "T-minus 71h 0m" in report
    assert "wipe_host" in report
    # the pitch line
    assert "It **is** the incident" in report


def test_build_report_handles_open_incident_and_no_clocks():
    msgs = [_m(_block({"type": "BRIEF", "incident": "INC-A-2026-0039",
                       "severity": "medium", "summary": "Trojan on ws-eng-014."}),
               sender_name="triage")]
    report, incident, alias = build_report(msgs, [], [], room_id="r")
    assert incident == "INC-A-2026-0039"
    assert "OPEN" in report                       # no RESOLUTION/CLOSE
    assert "No notification clocks" in report
    assert "No actions were executed" in report
