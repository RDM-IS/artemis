// ---------------------------------------------------------------------------
// D2 — the shared fetch wrapper must classify responses correctly:
//   ok + JSON       -> data
//   ok + HTML       -> error (non-JSON), NOT an empty success
//   !ok             -> error (carries status)
//   network throw   -> error (status 0)
// A silent "non-JSON parsed as empty" is exactly the bug that made every panel
// render its empty state during the CORS/misconfig incident.
//
//   node --test  (or: npm test)
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { getPortfolio, ApiError, describeError } from "../src/api.js";

// Minimal Response stand-in for a mocked global fetch.
function fakeResponse({ ok, status, contentType, text }) {
  return {
    ok,
    status,
    headers: { get: (k) => (k.toLowerCase() === "content-type" ? contentType : null) },
    text: async () => text,
  };
}

function withFetch(impl, fn) {
  const prev = globalThis.fetch;
  globalThis.fetch = impl;
  return Promise.resolve()
    .then(fn)
    .finally(() => {
      globalThis.fetch = prev;
    });
}

test("ok + JSON -> parsed data", async () => {
  await withFetch(
    async () =>
      fakeResponse({
        ok: true,
        status: 200,
        contentType: "application/json",
        text: '{"engagements":[]}',
      }),
    async () => {
      const data = await getPortfolio();
      assert.deepEqual(data, { engagements: [] });
    }
  );
});

test("ok + HTML -> non-JSON error (not an empty success)", async () => {
  await withFetch(
    async () =>
      fakeResponse({
        ok: true,
        status: 200,
        contentType: "text/html; charset=utf-8",
        text: "<!doctype html><title>hi</title>",
      }),
    async () => {
      await assert.rejects(getPortfolio(), (err) => {
        assert.ok(err instanceof ApiError);
        assert.equal(err.nonJson, true);
        assert.equal(describeError(err), "portfolio — non-JSON response");
        return true;
      });
    }
  );
});

test("!ok -> error carrying the status", async () => {
  await withFetch(
    async () =>
      fakeResponse({
        ok: false,
        status: 403,
        contentType: "application/json",
        text: '{"error":"forbidden"}',
      }),
    async () => {
      await assert.rejects(getPortfolio(), (err) => {
        assert.ok(err instanceof ApiError);
        assert.equal(err.status, 403);
        assert.equal(describeError(err), "portfolio — 403");
        return true;
      });
    }
  );
});

test("network throw -> error with status 0", async () => {
  await withFetch(
    async () => {
      throw new TypeError("Failed to fetch");
    },
    async () => {
      await assert.rejects(getPortfolio(), (err) => {
        assert.ok(err instanceof ApiError);
        assert.equal(err.status, 0);
        assert.equal(describeError(err), "portfolio — network error");
        return true;
      });
    }
  );
});

test("ok + JSON body but no content-type -> accepted", async () => {
  // Lenient: a valid JSON body with a missing content-type still succeeds.
  await withFetch(
    async () =>
      fakeResponse({ ok: true, status: 200, contentType: null, text: '{"engagements":[]}' }),
    async () => {
      const data = await getPortfolio();
      assert.deepEqual(data, { engagements: [] });
    }
  );
});
