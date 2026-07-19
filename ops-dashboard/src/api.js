// ---------------------------------------------------------------------------
// api.js — fetch helpers for the Engagement Ops API
//
// SECURITY: no API key. The API sits behind Cloudflare Access; the browser
// carries the Access identity via cookies automatically. Every request uses
// credentials:"include" and NO x-api-key header.
//
// API_BASE comes from VITE_OPS_API_BASE (empty = same-origin). See .env.example.
// ---------------------------------------------------------------------------

// import.meta.env is only defined under Vite. Guard it so this module is also
// importable by the plain-node test runner (see test/api.test.mjs).
export const API_BASE =
  (typeof import.meta !== "undefined" &&
    import.meta.env &&
    import.meta.env.VITE_OPS_API_BASE) ||
  "";

// Thrown for any request that does not resolve to a successful JSON body.
// `status` lets callers special-case 401/403 (Access not passed) and 404
// (unknown slug); status 0 is a network-level failure. `endpoint` is the short
// human label of the failed call ("portfolio", "approve") so the UI error strip
// can read "portfolio — 403". `nonJson` marks an ok response whose body was not
// JSON (edge HTML / CORS'd redirect) — a silent-failure case D2 must surface.
export class ApiError extends Error {
  constructor(message, status, body, { endpoint = null, nonJson = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.endpoint = endpoint;
    this.nonJson = nonJson;
  }
}

// True for the Cloudflare-Access-not-passed cases (401/403), which get the
// reload-to-sign-in affordance instead of a plain retry.
export function isAuthError(err) {
  return !!err && (err.status === 401 || err.status === 403);
}

// Short "endpoint — detail" string for the burnt-orange error strip, e.g.
// "portfolio — 403", "portfolio — non-JSON response", "approve — network error".
export function describeError(err) {
  const endpoint = (err && err.endpoint) || "request";
  let detail;
  if (!(err instanceof ApiError)) detail = (err && err.message) || "failed";
  else if (err.nonJson) detail = "non-JSON response";
  else if (err.status === 0) detail = "network error";
  else if (err.status) detail = `${err.status}`;
  else detail = err.message || "failed";
  return `${endpoint} — ${detail}`;
}

async function request(path, { method = "GET", body, label } = {}) {
  const endpoint = label || "request";
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
    throw new ApiError(`Network error: ${err.message}`, 0, null, { endpoint });
  }

  const contentType =
    (res.headers && res.headers.get && res.headers.get("content-type")) || "";
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
    throw new ApiError(msg, res.status, json, { endpoint });
  }

  // D2 (error-as-empty): an OK response that is not JSON — an HTML error page, a
  // CORS'd redirect, an edge challenge that slipped through as 200 — must NOT be
  // treated as an empty success. Surface it as a visible failure so panels don't
  // render their empty-state copy over a real error.
  if (json === null || (contentType && !/\bjson\b/i.test(contentType))) {
    throw new ApiError("non-JSON response", res.status, null, {
      endpoint,
      nonJson: true,
    });
  }
  return json;
}

// --- Reads ---------------------------------------------------------------

export function getPortfolio() {
  return request("/api/portfolio", { label: "portfolio" });
}

export function getEngagement(slug) {
  return request(`/api/engagements/${encodeURIComponent(slug)}`, {
    label: "engagement",
  });
}

export function searchDossier(q) {
  return request(`/api/dossier/search?q=${encodeURIComponent(q)}`, {
    label: "search",
  });
}

export function getPerson(slug) {
  return request(`/api/dossier/person/${encodeURIComponent(slug)}`, {
    label: "person",
  });
}

export function getOrg(org) {
  return request(`/api/dossier/org/${encodeURIComponent(org)}`, {
    label: "org",
  });
}

// --- Mutations -----------------------------------------------------------

// payloadFinal optional — omit for a plain approve, pass an edit object for
// edit-then-approve.
export function approveProposal(id, payloadFinal) {
  const body = payloadFinal ? { payload_final: payloadFinal } : {};
  return request(`/api/proposals/${id}/approve`, {
    method: "POST",
    body,
    label: "approve",
  });
}

export function rejectProposal(id) {
  return request(`/api/proposals/${id}/reject`, {
    method: "POST",
    body: {},
    label: "reject",
  });
}

export function batchProposals(action, ids) {
  return request("/api/proposals/batch", {
    method: "POST",
    body: { action, ids },
    label: `batch ${action}`,
  });
}

export function approveDossierDraft(type, id) {
  return request("/api/dossier-drafts/approve", {
    method: "POST",
    body: { type, id },
    label: "draft approve",
  });
}

export function rejectDossierDraft(type, id) {
  return request("/api/dossier-drafts/reject", {
    method: "POST",
    body: { type, id },
    label: "draft reject",
  });
}

export function closeCommitment(id) {
  return request(`/api/commitments/${id}/close`, {
    method: "POST",
    body: {},
    label: "close commitment",
  });
}
