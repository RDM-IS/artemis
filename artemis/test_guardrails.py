"""Test the external attendee guardrail.

Run directly: python -m artemis.test_guardrails
"""

import sys


def test_external_attendee_guardrail():
    """Test that external attendees are blocked without approval."""
    from artemis.guardrails import check_external_attendees, get_external_attendees

    # 1. No attendees — should be allowed
    result = check_external_attendees("Team standup", None)
    assert result["allowed"], f"FAIL: No attendees should be allowed, got {result}"

    result = check_external_attendees("Team standup", [])
    assert result["allowed"], f"FAIL: Empty attendees should be allowed, got {result}"

    # 2. Internal-only attendees — should be allowed
    result = check_external_attendees("1:1 sync", ["ryan@rdm.is"])
    assert result["allowed"], f"FAIL: Internal @rdm.is should be allowed, got {result}"

    result = check_external_attendees("1:1 sync", ["ryan@gmail.com"])
    assert result["allowed"], f"FAIL: Internal @gmail.com should be allowed, got {result}"

    result = check_external_attendees("1:1 sync", ["ryan@rdm.is", "ryan@gmail.com"])
    assert result["allowed"], f"FAIL: Mixed internal should be allowed, got {result}"

    # 3. External attendee WITHOUT approval — MUST be blocked
    result = check_external_attendees(
        "Call with Brad",
        ["ryan@rdm.is", "brad.spaits@external.com"],
        user_approved=False,
    )
    assert not result["allowed"], f"FAIL: External attendee should be BLOCKED, got {result}"
    assert "brad.spaits@external.com" in result["external"], f"FAIL: Should identify external email"

    # 4. External attendee WITH approval — should be allowed (user confirmed)
    result = check_external_attendees(
        "Call with Brad",
        ["ryan@rdm.is", "brad.spaits@external.com"],
        user_approved=True,
    )
    assert result["allowed"], f"FAIL: Approved external should be allowed, got {result}"

    # 5. Multiple external attendees — all must be flagged
    result = check_external_attendees(
        "Group call",
        ["ryan@rdm.is", "alice@acme.com", "bob@corp.net"],
        user_approved=False,
    )
    assert not result["allowed"]
    assert len(result["external"]) == 2, f"FAIL: Should flag 2 externals, got {result['external']}"

    # 6. get_external_attendees helper
    ext = get_external_attendees(["ryan@rdm.is", "alice@acme.com", "bob@gmail.com"])
    assert ext == ["alice@acme.com"], f"FAIL: Should only flag acme.com, got {ext}"

    ext = get_external_attendees(None)
    assert ext == [], f"FAIL: None should return [], got {ext}"

    ext = get_external_attendees(["RYAN@RDM.IS", "Alice@ACME.COM"])
    assert ext == ["alice@acme.com"], f"FAIL: Should be case-insensitive, got {ext}"

    # 7. Verify guardrail cannot be bypassed by passing user_approved without actual external
    result = check_external_attendees("Solo work", ["ryan@rdm.is"], user_approved=True)
    assert result["allowed"], "FAIL: Internal-only with approval should be fine"

    print("PASS — all external attendee guardrail tests passed")
    return True


if __name__ == "__main__":
    try:
        success = test_external_attendee_guardrail()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"FAIL — {e}")
        sys.exit(1)
