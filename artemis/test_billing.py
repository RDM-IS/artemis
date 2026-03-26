"""Tests and CLI dry-run for PB-007 Billing Intake.

Usage:
    python -m artemis.test_billing --dry-run     # process most recent billing email (no writes)
    python -m artemis.test_billing --unit         # run unit tests
"""

import argparse
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_amount_extraction():
    from artemis.billing import extract_amounts, best_amount, parse_amount

    # Basic dollar amounts
    assert extract_amounts("Total: $1,234.56") == ["$1,234.56"]
    assert extract_amounts("Pay $50") == ["$50"]
    assert extract_amounts("Amount: $99.99 plus $10.00 tax") == ["$99.99", "$10.00"]
    assert extract_amounts("No amounts here") == []

    # Bare decimal amounts (no $)
    assert extract_amounts("Invoice total 1,234.56") == ["1,234.56"]

    # best_amount picks largest
    best, all_found = best_amount("$50.00 and $125.00 and $10.00")
    assert best == "$125.00"
    assert len(all_found) == 3

    # parse_amount
    assert parse_amount("$1,234.56") == 1234.56
    assert parse_amount("50") == 50.0
    assert parse_amount("invalid") == 0.0

    print("  ✓ Amount extraction tests passed")


def test_category_classification():
    from artemis.billing import classify_category

    assert classify_category("AWS monthly bill", "billing@aws.amazon.com") == "Infrastructure"
    assert classify_category("Notion Team Plan", "billing@notion.so") == "SaaS / Software"
    assert classify_category("Invoice", "legal@smithlaw.com") == "Legal"
    assert classify_category("Liability renewal", "agent@insurance.com") == "Insurance"
    assert classify_category("MacBook Pro", "apple.com/receipt") == "Hardware"
    assert classify_category("LinkedIn Premium", "linkedin@linkedin.com") == "Sales & Outreach"
    assert classify_category("Random vendor", "hello@random.com") == "Misc"

    # Case insensitive
    assert classify_category("AWS Charges", "") == "Infrastructure"
    assert classify_category("", "support@GITHUB.com") == "SaaS / Software"

    print("  ✓ Category classification tests passed")


def run_unit_tests():
    print("\nRunning PB-007 unit tests:")
    test_amount_extraction()
    test_category_classification()
    print("\nAll tests passed ✓\n")


# ---------------------------------------------------------------------------
# Dry-run CLI
# ---------------------------------------------------------------------------

def run_dry_run():
    """Find the most recent billing-labeled email and process it without writing."""
    from artemis.billing import (
        check_billing_scopes,
        get_billing_messages,
        process_billing_message,
    )
    from artemis.gmail import GmailClient

    print("\nPB-007 Dry Run — Billing Intake\n" + "=" * 40)

    # Check scopes
    scopes_ok, missing = check_billing_scopes()
    if not scopes_ok:
        print(f"⚠ Missing OAuth scopes: {', '.join(missing)}")
        print("  Billing features won't work until scopes are added.")
        print("  Continuing dry-run with available scopes...\n")

    # Authenticate Gmail
    gmail = GmailClient()
    try:
        gmail.authenticate()
    except Exception as e:
        print(f"✗ Gmail authentication failed: {e}")
        sys.exit(1)

    if not gmail.service:
        print("✗ Gmail service not available")
        sys.exit(1)

    # Find billing messages
    print("Searching for billing-labeled emails...")
    message_ids = get_billing_messages(gmail)

    if not message_ids:
        print("No unprocessed billing emails found.")
        print("(Check that the 'artemis/billing' label exists and has emails)")
        return

    print(f"Found {len(message_ids)} unprocessed billing email(s)")
    print(f"Processing most recent: {message_ids[0]}\n")

    # Process with dry_run=True
    result = process_billing_message(gmail, message_ids[0], dry_run=True)

    print(f"Sender:     {result.get('sender_name', '?')} <{result.get('sender_email', '?')}>")
    print(f"Subject:    {result.get('subject', '?')}")
    print(f"Date:       {result.get('date', '?')}")
    print(f"Amount:     {result.get('amount') or '⚠ None detected'}")
    if result.get("all_amounts") and len(result["all_amounts"]) > 1:
        print(f"  All found: {', '.join(result['all_amounts'])}")
    print(f"Category:   {result.get('category', '?')}")
    print(f"Attachments: {result.get('attachments') or 'None'}")
    print(f"Gmail link: {result.get('gmail_link', '?')}")
    print(f"Notes:      {result.get('notes', '')}")

    if result.get("dry_run_row"):
        print(f"\nSheet row that would be appended:")
        for k, v in result["dry_run_row"].items():
            print(f"  {k}: {v}")

    print(f"\nDry run complete — no data was written.\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(description="PB-007 Billing Intake tests")
    parser.add_argument("--dry-run", action="store_true", help="Process most recent billing email (no writes)")
    parser.add_argument("--unit", action="store_true", help="Run unit tests")
    args = parser.parse_args()

    if args.unit:
        run_unit_tests()
    elif args.dry_run:
        run_dry_run()
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
