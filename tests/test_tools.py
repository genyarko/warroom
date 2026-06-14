"""Standalone unit tests for the Phase 3 domain layer.

Tests the *pure* tool logic (no Band, no LLM, no network) plus the framework
tool-builders. Pytest-discoverable, but also runs with no pytest installed:

    uv run python -m tests.test_tools

Exit criterion coverage (plan §Phase 3): all three alerts load; each agent's
tools return correct domain answers; action tools write to the log.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from shared import mock_data
from agents.triage.tools import classify_alert, lookup_asset
from agents.threat_intel.tools import assess_spread_risk, lookup_ioc
from agents.compliance.tools import (
    check_regulatory_triggers,
    evidence_preservation_requirements,
    start_notification_clock,
)


# --- Mock environment ------------------------------------------------------

def test_all_three_alerts_load():
    for alias in ("INC-A", "INC-B", "INC-C"):
        alert = mock_data.load_alert(alias)
        assert alert["incident_id"].startswith(alias), alert["incident_id"]
    assert len(mock_data.list_alerts()) == 3


def test_mock_data_sizes():
    assert len(mock_data.load_iocs()) >= 15
    assert len(mock_data.load_assets()) == 8
    assert len(mock_data.load_reg_rules()) >= 3
    # The PII server the demo hinges on exists and is flagged.
    db = mock_data.get_asset("srv-db-01")
    assert db is not None and "customer_pii" in db["data_classes"]


# --- Triage ----------------------------------------------------------------

def test_classify_inc_c_contested():
    v = classify_alert("INC-C")
    assert v["disposition"] == "investigate"
    assert v["severity"] == "critical"
    assert v["pii_involved"] is True
    # All three specialists recruited, with reasoning.
    assert set(v["recommended_specialists"]) == {"threat_intel", "compliance", "commander"}
    assert v["malicious_indicators"], "BlackHaze hash should be known-malicious"


def test_classify_inc_a_no_compliance():
    v = classify_alert("INC-A")
    assert v["disposition"] == "investigate"
    assert v["pii_involved"] is False
    # Real malware -> Threat Intel + Commander, but NO Compliance (no regulated data).
    assert "threat_intel" in v["recommended_specialists"]
    assert "compliance" not in v["recommended_specialists"]


def test_classify_inc_b_false_positive_closes():
    v = classify_alert("INC-B")
    assert v["disposition"] == "close"
    assert v["is_false_positive"] is True
    assert v["recommended_specialists"] == []


def test_lookup_asset():
    assert lookup_asset("srv-db-01")["found"] is True
    assert lookup_asset("nope-99")["found"] is False


# --- Threat Intel ----------------------------------------------------------

def test_lookup_ioc_match_and_miss():
    hit = lookup_ioc("185.220.101.47")
    assert hit["matched"] is True
    assert hit["malware_family"] == "BlackHaze ransomware"
    assert hit["lateral_movement"] is True
    assert lookup_ioc("10.0.0.255")["matched"] is False


def test_assess_spread_risk_differentiates():
    pii = assess_spread_risk("srv-db-01")
    assert pii["spread_risk"] in ("high", "critical")
    assert pii["reachable_high_value_hosts"]
    ws = assess_spread_risk("ws-eng-014")
    assert ws["spread_risk"] in ("low", "medium")


# --- Compliance ------------------------------------------------------------

def test_regulatory_triggers_inc_c():
    r = check_regulatory_triggers("INC-C")
    ids = {t["rule_id"] for t in r["triggered"]}
    assert "GDPR-ART-33" in ids
    assert "SEC-8K-1.05" in ids
    assert r["any_evidence_preservation_required"] is True


def test_regulatory_triggers_inc_a_none():
    r = check_regulatory_triggers("INC-A")
    # ws-eng-014 holds only internal_confidential -> no notification regime.
    assert r["triggered"] == []


def test_notification_clock_72h():
    now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
    clock = start_notification_clock("GDPR-ART-33", "INC-C", now=now)
    assert clock["started"] is True
    assert clock["deadline_utc"] == datetime(2026, 6, 16, 12, 0, 0,
                                             tzinfo=timezone.utc).isoformat()


def test_evidence_preservation_blocks_wipe_on_pii():
    pii = evidence_preservation_requirements("srv-db-01")
    assert pii["preservation_required"] is True
    assert "wipe_host" in pii["blocks_destructive_actions"]
    clean = evidence_preservation_requirements("ws-eng-014")
    assert clean["preservation_required"] is False


# --- Commander action tools ------------------------------------------------

def test_action_tools_write_log():
    from agents.commander import tools as cmd

    with tempfile.TemporaryDirectory() as d:
        log_path = os.path.join(d, "actions_log.jsonl")
        os.environ["ACTIONS_LOG_PATH"] = log_path
        try:
            cmd._isolate_host(cmd.isolate_host(asset_id="srv-db-01", reason="ransomware"))
            cmd._preserve_disk_image(
                cmd.preserve_disk_image(asset_id="srv-db-01", reason="evidence"))
            cmd._notify_stakeholders(
                cmd.notify_stakeholders(stakeholders=["DPO", "CISO"], message="breach"))
        finally:
            del os.environ["ACTIONS_LOG_PATH"]

        with open(log_path, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]

    assert [r["action"] for r in records] == [
        "isolate_host", "preserve_disk_image", "notify_stakeholders"]
    assert all("timestamp_utc" in r and r["actor"] == "commander" for r in records)


# --- Framework tool-builders (wiring smoke test) ---------------------------

def test_framework_builders_produce_named_tools():
    from agents.triage.tools import langchain_tools as triage_lc
    from agents.threat_intel.tools import langchain_tools as intel_lc
    from agents.compliance.tools import pydantic_ai_tools
    from agents.commander.tools import anthropic_tools
    from thenvoi.runtime.custom_tools import get_custom_tool_name

    assert {t.name for t in triage_lc()} == {"classify_alert", "lookup_asset"}
    assert {t.name for t in intel_lc()} == {"lookup_ioc", "assess_spread_risk"}
    assert {f.__name__ for f in pydantic_ai_tools()} == {
        "check_regulatory_triggers", "start_notification_clock",
        "evidence_preservation_requirements"}
    anthropic_names = {get_custom_tool_name(model) for model, _ in anthropic_tools()}
    assert anthropic_names == {
        "isolate_host", "preserve_disk_image", "wipe_host", "notify_stakeholders"}


# --- Standalone runner -----------------------------------------------------

def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
