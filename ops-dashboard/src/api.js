// ---------------------------------------------------------------------------
// api.js — fetch helpers for the Engagement Ops API
//
// SECURITY: no API key. The API sits behind Cloudflare Access; the browser
// carries the Access identity via cookies automatically. Every request uses
// credentials:"include" and NO x-api-key header.
//
// API_BASE comes from VITE_OPS_API_BASE (empty = same-origin). See .env.example.
// ---------------------------------------------------------------------------

export const API_BASE = import.meta.env.VITE_OPS_API_BASE || "";

// Thrown for any non-OK response. `status` lets callers special-case 401/403
// (Access not passed) and 404 (unknown slug).
export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function request(path, { method = "GET", body } = {}) {
  const opts = {
    method,
    credentials: "include",
    headers: {},
  };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, opts);
  } catch (err) {
    // Network-level failure (DNS, offline, CORS preflight blocked, etc.)
    throw new ApiError(`Network error: ${err.message}`, 0, null);
  }

  let json = null;
  const text = await res.text();
  if (text) {
    try {
      json = JSON.parse(text);
    } catch {
      json = null;
    }
  }

  if (!res.ok) {
    const msg =
      (json && (json.error || json.message)) || `HTTP ${res.status}`;
    throw new ApiError(msg, res.status, json);
  }
  return json;
}

// --- Reads ---------------------------------------------------------------

export function getPortfolio() {
  return request("/api/portfolio");
}

export function getEngagement(slug) {
  return request(`/api/engagements/${encodeURIComponent(slug)}`);
}

export function searchDossier(q) {
  return request(`/api/dossier/search?q=${encodeURIComponent(q)}`);
}

export function getPerson(slug) {
  return request(`/api/dossier/person/${encodeURIComponent(slug)}`);
}

export function getOrg(org) {
  return request(`/api/dossier/org/${encodeURIComponent(org)}`);
}

// --- Mutations -----------------------------------------------------------

// payloadFinal optional — omit for a plain approve, pass an edit object for
// edit-then-approve.
export function approveProposal(id, payloadFinal) {
  const body = payloadFinal ? { payload_final: payloadFinal } : {};
  return request(`/api/proposals/${id}/approve`, { method: "POST", body });
}

export function rejectProposal(id) {
  return request(`/api/proposals/${id}/reject`, { method: "POST", body: {} });
}

export function batchProposals(action, ids) {
  return request("/api/proposals/batch", {
    method: "POST",
    body: { action, ids },
  });
}

export function approveDossierDraft(type, id) {
  return request("/api/dossier-drafts/approve", {
    method: "POST",
    body: { type, id },
  });
}

export function rejectDossierDraft(type, id) {
  return request("/api/dossier-drafts/reject", {
    method: "POST",
    body: { type, id },
  });
}

export function closeCommitment(id) {
  return request(`/api/commitments/${id}/close`, { method: "POST", body: {} });
}
