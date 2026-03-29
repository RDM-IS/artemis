"""RDMIS CRM API client.

Wraps the production CRM REST API. Falls back gracefully when CRM_API_URL
is not configured — callers should check `is_available()` first.
"""

import logging
from datetime import date

import requests

from artemis import config

logger = logging.getLogger(__name__)

_TIMEOUT = 10  # seconds


class CRMClient:
    """Thin wrapper around the RDMIS CRM API."""

    def __init__(self):
        from knowledge.secrets import get_crm_api_key
        self.base_url = config.CRM_API_URL.rstrip("/")
        self._api_key = get_crm_api_key() if self.base_url else ""
        self._headers = {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
        }

    def is_available(self) -> bool:
        """Return True if CRM API is configured (URL and key present)."""
        return bool(self.base_url and self._api_key)

    # ------------------------------------------------------------------
    # Base request handler
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_trailing_slash(path: str) -> str:
        """Add trailing slash to collection endpoints (API Gateway requirement).

        Collection paths like /organizations need a trailing slash.
        Item paths like /organizations/{id} and /health do not.
        """
        if path.endswith("/"):
            return path
        segments = path.rstrip("/").split("/")
        if len(segments) == 2 and segments[1] not in ("health",):
            return path + "/"
        return path

    def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        """Make an authenticated request. Returns parsed JSON or None on failure."""
        path = self._ensure_trailing_slash(path)
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(
                method, url, headers=self._headers, timeout=_TIMEOUT, **kwargs
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = ""
            try:
                body = e.response.text[:300] if e.response is not None else ""
            except Exception:
                pass
            logger.error("CRM API %s %s → %s: %s", method, path, status, body)
            raise RuntimeError(f"CRM API error ({status}): {body}") from e
        except requests.ConnectionError:
            logger.error("CRM API unreachable: %s", url)
            raise RuntimeError("CRM API unreachable")
        except requests.Timeout:
            logger.error("CRM API timeout: %s", url)
            raise RuntimeError("CRM API timeout")

    def _get(self, path: str, **params) -> dict | list | None:
        return self._request("GET", path, params=params)

    def _post(self, path: str, data: dict) -> dict | None:
        return self._request("POST", path, json=data)

    def _patch(self, path: str, data: dict) -> dict | None:
        return self._request("PATCH", path, json=data)

    def _delete(self, path: str) -> dict | None:
        return self._request("DELETE", path)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> bool:
        """Check API health. Returns True if healthy."""
        try:
            resp = self._get("/health")
            return resp.get("status") == "ok" if isinstance(resp, dict) else False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Organizations
    # ------------------------------------------------------------------

    def get_organizations(self) -> list[dict]:
        result = self._get("/organizations")
        return result if isinstance(result, list) else result.get("items", []) if isinstance(result, dict) else []

    def create_organization(self, data: dict) -> dict:
        return self._post("/organizations", data)

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def get_contacts(self) -> list[dict]:
        result = self._get("/contacts")
        return result if isinstance(result, list) else result.get("items", []) if isinstance(result, dict) else []

    def get_contact(self, contact_id: str) -> dict | None:
        return self._get(f"/contacts/{contact_id}")

    def find_contact_by_name(self, name: str) -> dict | None:
        """Search contacts by name (case-insensitive partial match)."""
        contacts = self.get_contacts()
        name_lower = name.lower()
        for c in contacts:
            c_name = c.get("name", "")
            if name_lower in c_name.lower():
                return c
        return None

    def find_contact_by_email(self, email: str) -> dict | None:
        """Search contacts by email."""
        contacts = self.get_contacts()
        email_lower = email.lower()
        for c in contacts:
            if c.get("email", "").lower() == email_lower:
                return c
        return None

    def create_contact(self, data: dict) -> dict:
        return self._post("/contacts", data)

    # ------------------------------------------------------------------
    # Deals
    # ------------------------------------------------------------------

    def get_deals(self) -> list[dict]:
        result = self._get("/deals")
        return result if isinstance(result, list) else result.get("items", []) if isinstance(result, dict) else []

    def create_deal(self, data: dict) -> dict:
        return self._post("/deals", data)

    def update_deal(self, deal_id: str, gate: str | None = None, stage: str | None = None) -> dict:
        payload = {}
        if gate is not None:
            payload["gate"] = gate
        if stage is not None:
            payload["stage"] = stage
        return self._patch(f"/deals/{deal_id}", payload)

    # ------------------------------------------------------------------
    # Interactions
    # ------------------------------------------------------------------

    def get_interactions(self) -> list[dict]:
        result = self._get("/interactions")
        return result if isinstance(result, list) else result.get("items", []) if isinstance(result, dict) else []

    def create_interaction(self, data: dict) -> dict:
        return self._post("/interactions", data)

    # ------------------------------------------------------------------
    # Commitments
    # ------------------------------------------------------------------

    def get_commitments(self, status: str | None = None) -> list[dict]:
        params = {}
        if status:
            params["status"] = status
        result = self._get("/commitments", **params)
        return result if isinstance(result, list) else result.get("items", []) if isinstance(result, dict) else []

    def get_commitment(self, commitment_id: str) -> dict | None:
        return self._get(f"/commitments/{commitment_id}")

    def create_commitment(self, data: dict) -> dict:
        """Create a commitment. Expected fields: description, due_date, contact_id (optional)."""
        return self._post("/commitments", data)

    def resolve_commitment(self, commitment_id: str) -> dict:
        return self._patch(f"/commitments/{commitment_id}", {"status": "resolved"})

    # ------------------------------------------------------------------
    # Invoices & Founder Loans (read-only for now)
    # ------------------------------------------------------------------

    def get_invoices(self) -> list[dict]:
        result = self._get("/invoices")
        return result if isinstance(result, list) else result.get("items", []) if isinstance(result, dict) else []

    def get_founder_loans(self) -> list[dict]:
        result = self._get("/founder-loans")
        return result if isinstance(result, list) else result.get("items", []) if isinstance(result, dict) else []

    # ------------------------------------------------------------------
    # Aggregate status
    # ------------------------------------------------------------------

    def get_status_summary(self) -> dict:
        """Get counts across all entities for the crm status command."""
        summary = {}
        try:
            orgs = self.get_organizations()
            summary["organizations"] = len(orgs)
        except Exception:
            summary["organizations"] = "error"

        try:
            contacts = self.get_contacts()
            summary["contacts"] = len(contacts)
        except Exception:
            summary["contacts"] = "error"

        try:
            deals = self.get_deals()
            summary["deals_total"] = len(deals)
            # Group by gate
            gates: dict[str, int] = {}
            for d in deals:
                gate = d.get("gate", "unknown")
                gates[gate] = gates.get(gate, 0) + 1
            summary["deals_by_gate"] = gates
        except Exception:
            summary["deals_total"] = "error"
            summary["deals_by_gate"] = {}

        try:
            open_commitments = self.get_commitments(status="open")
            summary["open_commitments"] = len(open_commitments)
        except Exception:
            summary["open_commitments"] = "error"

        return summary

    def format_status(self) -> str:
        """Format CRM status for Mattermost."""
        s = self.get_status_summary()
        lines = ["**CRM Status:**"]
        lines.append(f"- Organizations: **{s.get('organizations', '?')}**")
        lines.append(f"- Contacts: **{s.get('contacts', '?')}**")

        deals_total = s.get("deals_total", "?")
        lines.append(f"- Deals: **{deals_total}**")
        gates = s.get("deals_by_gate", {})
        if gates:
            for gate, count in sorted(gates.items()):
                lines.append(f"  - {gate}: {count}")

        lines.append(f"- Open commitments: **{s.get('open_commitments', '?')}**")
        return "\n".join(lines)
