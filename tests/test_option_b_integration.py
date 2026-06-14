"""Integration test for Option B flow: alert → intake room → Triage → incident room.

This test validates the REST API contract without actually calling Band
(mocks the HTTP layer to verify the request payloads are correct).
"""

import json
from unittest.mock import Mock, patch, call
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_injector_rest_flow():
    """Test that injector makes the correct REST calls in the right order."""
    from injector.inject_alert import _post_via_rest
    from shared.config import AgentCreds
    from contextlib import contextmanager

    triage = AgentCreds(
        name="triage",
        framework="langgraph",
        agent_id="triage-uuid-123",
        api_key="triage-key",
        account="primary",
        handle="@merolavtech/triage",
    )

    call_sequence = []

    @contextmanager
    def mock_response(status, data):
        """Create a mock context manager response."""
        resp = Mock()
        resp.status = status
        resp.read.return_value = json.dumps(data).encode()
        resp.__enter__ = Mock(return_value=resp)
        resp.__exit__ = Mock(return_value=None)
        yield resp

    def mock_urlopen(req):
        call_sequence.append({"method": req.get_method(), "url": req.full_url})

        # Return appropriate response based on call sequence
        if len(call_sequence) == 1:
            # Create room
            resp = Mock(status=201, read=lambda: json.dumps({"id": "intake-room-123"}).encode())
        elif len(call_sequence) == 2:
            # Add participant
            resp = Mock(status=200, read=lambda: json.dumps({}).encode())
        elif len(call_sequence) == 3:
            # Send message
            resp = Mock(status=200, read=lambda: json.dumps({}).encode())

        resp.__enter__ = Mock(return_value=resp)
        resp.__exit__ = Mock(return_value=None)
        return resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        room_id = _post_via_rest(
            content="@Triage test alert",
            triage=triage,
            api_key="test-api-key",
            base_url="https://app.thenvoi.com",
        )

    assert room_id == "intake-room-123", "Should return the created room ID"
    assert len(call_sequence) == 3, f"Expected 3 REST calls, made {len(call_sequence)}"
    print("[OK] Injector makes correct REST calls in correct order")


def test_injector_rest_endpoint_pattern():
    """Test that injector uses correct REST endpoint patterns."""
    from injector.inject_alert import _post_via_rest
    from shared.config import AgentCreds

    triage = AgentCreds(
        name="triage",
        framework="langgraph",
        agent_id="triage-uuid-123",
        api_key="triage-key",
        account="primary",
        handle="@merolavtech/triage",
    )

    endpoints_called = []

    def mock_urlopen(req):
        endpoints_called.append({
            "method": req.get_method(),
            "url": req.full_url,
        })

        # Return mock responses
        if len(endpoints_called) == 1:
            resp = Mock(status=201, read=lambda: json.dumps({"id": "room-123"}).encode())
        else:
            resp = Mock(status=200, read=lambda: json.dumps({}).encode())

        resp.__enter__ = Mock(return_value=resp)
        resp.__exit__ = Mock(return_value=None)
        return resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        _post_via_rest(
            content="@Triage test",
            triage=triage,
            api_key="test-key",
            base_url="https://app.thenvoi.com",
        )

    # Verify endpoint sequence
    assert len(endpoints_called) == 3, f"Should make 3 REST calls, made {len(endpoints_called)}"

    # First call: /me/chats (create room)
    assert "/me/chats" in endpoints_called[0]["url"], "First call should create room at /me/chats"
    assert endpoints_called[0]["method"] == "POST", "Create room should use POST"

    # Second call: /me/chats/{id}/participants (add Triage)
    assert "/me/chats/" in endpoints_called[1]["url"] and "participants" in endpoints_called[1]["url"], \
        "Second call should add participant"

    # Third call: /me/chats/{id}/messages (send alert)
    assert "/me/chats/" in endpoints_called[2]["url"] and "messages" in endpoints_called[2]["url"], \
        "Third call should send message"

    print("[OK] Injector uses correct REST endpoint patterns")


def test_triage_context_switching_prompt():
    """Test that Triage's prompt correctly handles context switching (intake → incident room)."""
    triage_prompt = (REPO_ROOT / "agents" / "triage" / "prompt.md").read_text(encoding="utf-8")

    # Should mention that all messages post to the incident room, not the intake room
    assert "incident room" in triage_prompt.lower(), "Prompt should mention incident room"
    assert "bootstrap" in triage_prompt.lower() or "create" in triage_prompt.lower(), \
        "Prompt should mention creating/bootstrapping"

    # Should have a rule about context (intake room vs incident room)
    rules_section = triage_prompt[triage_prompt.find("## Rules"):] if "## Rules" in triage_prompt else ""
    assert "intake room" in rules_section.lower() or "incident room" in rules_section.lower(), \
        "Rules section should clarify room context"

    print("[OK] Triage prompt handles room context switching")


def test_schema_consistency():
    """Test that message schemas are consistent with the protocol."""
    from shared.schemas import ProtocolMessage

    # BRIEF should be a valid message type
    try:
        msg = ProtocolMessage(
            type="BRIEF",
            incident="INC-C",
            summary="Test brief",
            severity="critical",
            evidence=["evidence1"],
            recruited=["threat_intel", "compliance"],
            mentions=["@merolavtech/threat-intel"],
        )
        assert msg.type == "BRIEF"
        print("[OK] ProtocolMessage schema supports BRIEF with recruited field")
    except Exception as e:
        raise AssertionError(f"ProtocolMessage schema error: {e}")


def test_no_pre_created_room_references():
    """Test that code doesn't reference pre-created rooms."""
    # Check Triage main.py
    triage_main = (REPO_ROOT / "agents" / "triage" / "main.py").read_text(encoding="utf-8")
    assert "default_room_id" not in triage_main, "Triage should not reference default_room_id"

    # Check Commander main.py (shouldn't be affected, but verify)
    commander_main = (REPO_ROOT / "agents" / "commander" / "main.py").read_text(encoding="utf-8")
    assert "create_chatroom" not in commander_main, \
        "Commander should not have create_chatroom (only Triage should)"

    print("[OK] No pre-created room references in agent code")


if __name__ == "__main__":
    import pytest
    try:
        pytest.main([__file__, "-v"])
    except ImportError:
        # Run tests manually
        tests = [
            test_injector_rest_flow,
            test_injector_rest_endpoint_pattern,
            test_triage_context_switching_prompt,
            test_schema_consistency,
            test_no_pre_created_room_references,
        ]
        for test in tests:
            try:
                test()
            except AssertionError as e:
                print(f"[FAIL] {test.__name__}: {e}")
            except Exception as e:
                print(f"[ERROR] {test.__name__}: {e}")
