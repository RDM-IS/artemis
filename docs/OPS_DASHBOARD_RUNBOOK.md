# OPS Dashboard Runbook — dashboard-managed state (ops.rdm.is)

Authored companion to the OPS-2 PR (#81) deploy checklist. The Cloudflare/DNS
config for `ops.rdm.is` is **dashboard-managed** (no repo IaC, same as
gym.rdm.is), so no code captures it. This file records the required dashboard
state so it survives a rebuild. Every item below was a **live first-day defect**
(POLISH-2) — GETs worked, something else silently broke.

## Cloudflare Access — Bypass OPTIONS on the API application

The Access application covering the **API hostname** (`ops-api.rdm.is`) MUST have
**"Bypass OPTIONS requests"** enabled.

Without it, CORS **preflight** (`OPTIONS`) requests are challenged and rejected
**403 at the edge**, before they reach the origin. The symptom is asymmetric and
misleading: simple GETs succeed (no preflight), but **every mutation POST**
(approve / reject / batch / close) dies on its preflight. Observed live: the UI
would dim-and-revert a card with no data change.

- Where: Cloudflare Zero Trust → Access → Applications → the API app → policies /
  settings → **Bypass OPTIONS requests**.
- Verify: an `OPTIONS` preflight to an `/api/...` route returns `200/204`, not
  `403`; a POST from the SPA then succeeds.

## Cloudflare Pages — custom domain via the Custom domains tab (NOT a manual CNAME)

`ops.rdm.is` MUST be attached to the Pages project through the **Custom domains**
tab of the Pages project.

Do **NOT** point a manual CNAME at a specific deployment URL
(`<hash>.<project>.pages.dev`). A manual CNAME to a deployment URL **pins the
site to that immutable deployment forever** — new production builds publish, but
the custom domain never moves to them.

Observed live (the "three-pin incident"): (1) the env var wasn't baked into the
bundle, (2) no production build ran after the var was set, and (3) a
deployment-pinned CNAME held the domain on the stale build. All three had to be
cleared before the fix went live.

- Where: Cloudflare Pages → the project → **Custom domains** → add `ops.rdm.is`
  (Cloudflare manages the DNS record and re-points it to the latest production
  deployment automatically).

## Vite env vars bake at BUILD time

`VITE_OPS_API_BASE` (and any `VITE_*` var) is inlined into the JS bundle when
`npm run build` runs. It is **not** read at runtime.

Changing it requires a **new production deployment**. Setting the var in the
Pages dashboard and *not* triggering a rebuild leaves the old value baked in the
served bundle. After changing the var: trigger a production deployment and
confirm the served bundle hash changed.

## Recommended — Build watch paths

Set the Pages project's **Build watch paths** to `ops-dashboard/*` (operator sets
this in the Pages dashboard). Backend-only merges to `main` then don't trigger a
pointless SPA rebuild. Merges touching `ops-dashboard/` still rebuild.

## Backlog note (do not build)

Dossier-draft rejection **hard-deletes** rows, while vault-proposal rejection
**soft-marks** them (`status='rejected'`). Consider soft-delete symmetry later so
rejected dossier drafts also persist as evidence for future rule promotion.
Deferred — recorded here so the asymmetry isn't rediscovered as a bug.
