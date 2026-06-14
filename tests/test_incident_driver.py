"""Unit tests for the Facilitator watchdog's pure logic (scripts/incident_driver).

No network: exercises parse_block_type and the decide_nudge state machine across
the full INC-C flow (BRIEF -> FINDINGs -> SIGNOFF_REQUEST -> SIGNOFF/VETO ->
ESCALATION -> human ruling -> RESOLUTION).
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from incident_driver import ParsedMsg, decide_nudge, parse_block_type  # noqa: E402


def _m(type_=None, role=None, is_human=False, text=""):
    return ParsedMsg(role=role, is_human=is_human, type=type_, text=text)


# --- parse_block_type ------------------------------------------------------

def test_parse_block_type_extracts_last_json_type():
    content = 'Some text\n```json\n{"type": "FINDING", "incident": "INC-C"}\n```'
    assert parse_block_type(content) == "FINDING"


def test_parse_block_type_ignores_non_protocol_json_and_plain_text():
    assert parse_block_type("just a plain message, no block") is None
    # A tool-output-ish json without a type is not a protocol block.
    assert parse_block_type('```json\n{"status": "ok"}\n```') is None


# --- decide_nudge state machine -------------------------------------------

def test_no_brief_nudges_triage():
    assert decide_nudge([]).target == "triage"


def test_brief_only_nudges_first_missing_specialist():
    n = decide_nudge([_m("BRIEF", role="triage")])
    assert n.target == "threat_intel"
    assert "FINDING" in n.ask


def test_one_finding_nudges_the_other_specialist():
    parsed = [_m("BRIEF", role="triage"), _m("FINDING", role="threat_intel")]
    assert decide_nudge(parsed).target == "compliance"


def test_all_findings_nudges_commander_for_signoff_request():
    parsed = [
        _m("BRIEF", role="triage"),
        _m("FINDING", role="threat_intel"),
        _m("FINDING", role="compliance"),
    ]
    n = decide_nudge(parsed)
    assert n.target == "commander"
    assert "SIGNOFF_REQUEST" in n.ask


def test_signoff_request_missing_decision_nudges_specialist():
    parsed = [
        _m("BRIEF", role="triage"),
        _m("FINDING", role="threat_intel"),
        _m("FINDING", role="compliance"),
        _m("SIGNOFF_REQUEST", role="commander"),
        _m("SIGNOFF", role="threat_intel"),
        # compliance has not responded yet
    ]
    assert decide_nudge(parsed).target == "compliance"


def test_veto_without_escalation_nudges_commander_to_escalate():
    parsed = [
        _m("BRIEF", role="triage"),
        _m("FINDING", role="threat_intel"),
        _m("FINDING", role="compliance"),
        _m("SIGNOFF_REQUEST", role="commander"),
        _m("SIGNOFF", role="threat_intel"),
        _m("VETO", role="compliance"),
    ]
    n = decide_nudge(parsed)
    assert n.target == "commander"
    assert "ESCALATION" in n.ask


def test_escalation_without_ruling_nudges_human():
    parsed = [
        _m("BRIEF", role="triage"),
        _m("FINDING", role="threat_intel"),
        _m("FINDING", role="compliance"),
        _m("SIGNOFF_REQUEST", role="commander"),
        _m("SIGNOFF", role="threat_intel"),
        _m("VETO", role="compliance"),
        _m("ESCALATION", role="commander"),
    ]
    assert decide_nudge(parsed).target == "human"


def test_human_ruling_nudges_commander_to_resolve():
    parsed = [
        _m("BRIEF", role="triage"),
        _m("FINDING", role="threat_intel"),
        _m("FINDING", role="compliance"),
        _m("SIGNOFF_REQUEST", role="commander"),
        _m("SIGNOFF", role="threat_intel"),
        _m("VETO", role="compliance"),
        _m("ESCALATION", role="commander"),
        _m(None, is_human=True),  # the CISO replies (plain text, no block)
    ]
    n = decide_nudge(parsed)
    assert n.target == "commander"
    assert "RESOLUTION" in n.ask


def test_terminal_states_stop_driving():
    base = [_m("BRIEF", role="triage")]
    assert decide_nudge(base + [_m("RESOLUTION", role="commander")]) is None
    assert decide_nudge(base + [_m("CLOSE", role="triage")]) is None


# --- tolerance: messy LLM output (no clean json types) ---------------------

def test_specialist_post_without_finding_type_still_counts():
    # Threat Intel posted, but with no/odd type (seen live: 'GATHERING', no block).
    parsed = [
        _m("BRIEF", role="triage"),
        _m(None, role="threat_intel", text="here is my analysis of the iocs..."),
    ]
    # It counts as contributed, so the next nudge targets the silent specialist.
    assert decide_nudge(parsed).target == "compliance"


def test_keyword_signoff_request_advances_phase():
    # Commander issues a SIGNOFF_REQUEST in prose, no json type.
    parsed = [
        _m("BRIEF", role="triage"),
        _m(None, role="threat_intel", text="finding: critical"),
        _m(None, role="compliance", text="finding: gdpr hold"),
        _m(None, role="commander", text="please sign off on this plan: isolate, image, wipe"),
    ]
    n = decide_nudge(parsed)
    assert n.target in ("threat_intel", "compliance")
    assert "SIGNOFF" in n.ask  # we're in the sign-off phase now


def test_keyword_veto_triggers_escalation_nudge():
    parsed = [
        _m("BRIEF", role="triage"),
        _m(None, role="threat_intel", text="finding"),
        _m(None, role="compliance", text="finding"),
        _m(None, role="commander", text="please sign off on the plan"),
        _m(None, role="threat_intel", text="i sign off"),
        _m(None, role="compliance", text="i must veto the wipe — legal hold"),
    ]
    n = decide_nudge(parsed)
    assert n.target == "commander"
    assert "ESCALATION" in n.ask


def test_keyword_escalation_then_human_resolution():
    parsed = [
        _m("BRIEF", role="triage"),
        _m(None, role="threat_intel", text="finding"),
        _m(None, role="compliance", text="finding"),
        _m(None, role="commander", text="please sign off"),
        _m(None, role="threat_intel", text="sign off"),
        _m(None, role="compliance", text="veto the wipe"),
        _m(None, role="commander", text="escalating to the CISO for a decision"),
    ]
    assert decide_nudge(parsed).target == "human"
    # CISO replies (plain text, no role) -> commander should resolve.
    parsed.append(_m(None, is_human=True, text="authorize the wipe"))
    assert decide_nudge(parsed).target == "commander"
