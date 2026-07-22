# Artemis / ACOS — Architecture & Build State

> **What this is.** The *authored intent* layer: north star, architecture, roadmap, backlog, and the disciplines that govern the build. It holds **no volatile facts** — IPs, instance IDs, ports, module lists, schema dumps live in `context/CONTEXT.generated.md`, generated from the box. A concrete runtime fact appearing here is a bug; move it to the snapshot. Operating rules live in `CLAUDE.md` at repo root.
>
> **Baseline:** updated at the completion of the SQLite→RDS retirement (7/7 phases). Status markers: ✅ built & verified · 🚧 in flight · ⬜ designed, not built.

---

## 0. North star

A single-operator **AI Chief of Staff** that runs operations autonomously *within guardrails*, proposing before acting. The dividing line is fixed:

> **Artemis does statistics. Ryan does semantics.**
> Artemis counts, connects, surfaces, detects, drafts, executes within rules. Ryan means, names, blesses, adjudicates. Artemis never authors meaning or standing rules.

Trajectory toward "Jarvis" is three additive capabilities on the working system: **unified state** (✅ done — the migration), **memory + learning** (⬜ the cognition layer), **bounded autonomy** (⬜ gated standing automations). Each shippable, reversible, no rewrite.

---

## 1. Current architecture

### Tier 1 — Transactional (system of record) ✅
**RDS PostgreSQL**, three schemas — and as of the migration, the **single source of truth** for all operational state:
- `public` — CRM: persons/companies/relationships/engagements/touch_events, deals, interactions, commitments (CRM-scoped), invoices, financials. *Externally owned by the Lambda CRM API.*
- `acos` — operational: action_items, audit_log, calendar_audit, guardrail_violations, **inbox_threads** (018), **system_state/quiet_state/timezone_overrides** (019), **commitments** (020, personal tracker), processed_billing, schema_migrations.
- `health` — plan, session_log, daily_state, adjustments, training_rules, phase_config.

SQLite (`artemis.db`) is **deleted from the box**; no module imports `sqlite3`; CI guards prevent reintroduction.

### Tier 2 — The agent ✅
`acos.service` on EC2 (`python3.11 -m artemis.main`): APScheduler process running cron jobs + routing `@artemis` mentions to domain handlers. RDS via `knowledge/db.py` (now params-hardened); secrets via `knowledge/secrets.py` (Secrets Manager only, defensive shape-parsing). LLM via Anthropic API.

### Tier 3 — Surfaces ✅
- **Mattermost** (`#artemis-ryan`) — primary, plus scheduled proactive posts.
- **gym-display** (`gym.rdm.is`) — owns structured per-set strength capture.
- **Lambda CRM API** (`api/`) — RDS-backed, for external/frontend consumers. The bot reads RDS directly via `knowledge.db` *except* the three CRM commands (`crm status`/`contacts`/`leads`), which go through this API (key from Secrets Manager).

### Meta — context system ✅
`scripts/context_snapshot.sh` → `context/CONTEXT.generated.md`, committed. Precedence: live repo/DB → snapshot → authored docs → memory. Access via `ssh rdmis` (SSH-over-SSM, instance-ID based — no stale IP). `CLAUDE.md` wires this into every Claude Code session.

---

## 2. Second-brain / lakehouse design ⬜

Three layers; **medallion discipline in Postgres**, not an enterprise lakehouse. S3+DuckDB only when a workload demands it. No Iceberg/Glue/Athena at solo scale.

- **Layer A — Transactional:** RDS (Tier 1). Real.
- **Layer B — Analytical/memory:** bronze→silver→gold as Postgres schemas. Bronze = raw immutable capture; silver = deterministic-rule-cleaned, machine-trusted; gold = confidence-weighted observations that inform but don't act.
- **Layer C — Knowledge (Obsidian):** human-authored semantic surface. **Hard split:** `generated/` (machine-written, freely regenerated) vs `authored/` (Ryan's thinking, never overwritten). Postgres is rebuildable cache; vault file is canon. `pgvector` for embeddings. Deterministic transforms may write back to a note; inferential/model-judgment stays a Postgres projection with confidence+provenance, never silently editing canon.

### Artemis's cognition medallion ⬜ (the "memory + learning")
A medallion over Artemis's **own decisions**:
- **Bronze = `acos.cognition_log`** — every consequential decision, immutable, append-only: decision, `assumptions` (jsonb — highest-value column), confidence, outcome, correction, `manual_gap` flag. `manual_gap=true` entries self-generate the playbook roadmap.
- **Silver = hard rules** — deterministic, Ryan+Claude author, Artemis executes, machine-trusted.
- **Gold = soft rules** — observed tendencies, confidence-weighted, context-conditioned; bias defaults, **never auto-act**. Evolution-ready schema: `(rule, context, confidence, last_reinforced_at, evidence_count, supersedes?)`. Accretion + decay + supersession (Ryan adjudicates growth vs drift). Future `/drift` diffs authored gold vs observed gold.
- **Actionable gold** = self-proposed-but-human-approved playbooks; activation always gated.

> **Note (HEALTH-1 relevance):** a premature, half-built "learning"/`add_note` stub is currently live in routing and confabulating ("I've learned that…"). The cognition layer above is the *correct* version, built deliberately and gated. The stub must come out of live routing — see backlog.

---

## 3. The @artemis ↔ Ryan loop ⬜

Capture (dumb, immutable) → Surface (synthesis surfacer detects high-surprise cross-domain co-occurrence) → Adjudicate (Ryan assigns meaning) → Propose (gated playbook draft) → Build loop (headless Claude Code → branch → build → preview → critique → PR, never auto-merged). Integrity depends on the statistics/semantics wall and activation-gating.

---

## 4. What's built ✅

- **9 playbooks** (PB-001…009) + CRM Write Guard (PB-008).
- **Inbox-zero** triage with state machine (now `acos.inbox_threads`); full-body fetch fix.
- **CRM** on RDS; Lambda API; three Mattermost CRM commands wired (Secrets-Manager key).
- **Health subsystem**: `plan_detail` (verified-live "today's workout"), cardio capture, nutrition + grocery→Postgres, plan reseed + validator.
- **Calendar guards**: dupe-detection + audit (Brad guards), anti-confabulation guard.
- **Context-snapshot system** + `ssh rdmis` over SSM + clean branch hygiene (auto-delete on merge).
- **SQLite→RDS migration — COMPLETE (7/7):** guardrails, life_ops workouts (+ rest-day shim), crm (deleted), inbox (018), quiet_hours (019), commitments (020). `artemis.db` deleted; `execute_query` params-hardened (killed the `%`/quote landmine); no-SQLite CI regression guards in every migrated module. CT-anchoring applied inline where each phase touched date logic.
- **PB-011 Vault / Second Brain Ingest (v1) ✅** — schema `vault` (migration 028); sync (04:00 CT cron + on-demand `vault sync` / `digest`); one extraction pass per new note → gated proposals adjudicated with the E3 `approve`/`reject` range syntax; morning-brief digest + journal-diff coverage + undictated-meeting nudge. The vault file is canon; Postgres is a rebuildable projection.

---

## 5. In flight 🚧

Nothing mid-migration. Next build is **HEALTH-1** (below) — top of backlog.

---

## 6. Backlog (prioritized)

**HEALTH-1 (high) — morning check-in misroute + confabulation loop.** A health check-in ("sleep 7 energy 5 …") is not logged. `detect_health_intent` correctly returns `log_morning_state` and `handle_morning_intent` works (verified: writes `health.daily_state`, returns "Logged: …"). But the LLM classifier returns `general_reply` (0.95), a "Correction re-route" sends it to a confabulating `add_note` path that "learns" fake rules (including rules about its own errors) and captures the wrong message as the note. **Root causes:** (a) deterministic health intent not short-circuiting the LLM classifier; (b) a premature `add_note`/"learning" stub live in routing with no backing store. **Fix:** make positive `detect_health_intent` unoverridable → `handle_morning_intent`; disable/gate the `add_note` stub out of live routing. Verify by re-sending the check-in via Mattermost.

**CONFIRM-ARB — confirm-handler arbitration / bare-`yes` disambiguation (medium; own future branch off `main` after feat/health-ramp merges).** Multiple flows consume a bare `yes`/`no`/`confirm`/`cancel` in `#artemis-ryan`, each gating on its *own* pending-state, and those states are **independent** — several can be live at once. A bare control word is then resolved by fixed `deterministic_chain` order (first-match-wins), which is deterministic but **not** intent-aware: a `yes` meant for flow A can silently execute flow B.

- **Pending stores (independent; nothing clears the others):**
  | flow | store | expiry |
  |---|---|---|
  | calendar create / delete / dupe-override, `rule add`, dossier/org set | in-mem `_pending_confirms[channel]` (single value → these are mutually exclusive *with each other*) | 600s (calendar) / none (rule, dossier) |
  | debrief capture | `acos.system_state` `debrief_pending:{ch}` | 600s |
  | modality swap | `acos.system_state` `modality_swap_pending:{ch}` | 600s |
  | nutrition target / grocery staples | `acos.system_state` keys | 900s / 1800s |
  | **ramp repeat/restart** | `health.ramp_state.pending_payload` | **none** |
  | inbox disposition batch | `_inbox_listing_state[ch]` | none |
- **Chain trace (bare `yes`, ramp + `rule add` both live):** duplicate_override→no · calendar/delete_confirm→no (wrong type) · debrief/swap→no pending · **ramp_confirm→WOULD match (index before rule_command)** · rule_command never reached. The confirm backstop only re-runs the in-memory calendar handlers, so it doesn't help.
- **Worst case:** the ramp pending **never expires**, so it can coexist for days with a just-created `rule add`/dossier pending; the user types `yes` to activate the rule and (pre-fix) the ramp repeat/restart fires instead.
- **Interim (shipped on feat/health-ramp):** `ramp_confirm` matches **only** the qualified `yes ramp`/`no ramp` and never a bare control word — it removes ramp from the bare-`yes` contention entirely. The *general* race (debrief↔swap↔nutrition↔rule↔disposition ordering) remains.
- **Fix (this item):** a shared `_count_open_pendings(channel_id)` helper over all stores; when **>1** pending is open and a bare control word arrives, reply with a disambiguation prompt (`reply `yes ramp` or `yes rule``) and consume the word safely instead of first-match-wins; teach each confirm handler to also accept its qualified form. Keep first-match-wins when exactly one pending is open.

**CRM-2 / COMMIT-1 — two-store seams (medium).** Two contact stores: `public.contacts`/`organizations` (CRM API) vs `public.persons`/`companies` (Write Guard, PB-008). Two commitment stores: `acos.commitments` (personal tracker) vs `public.commitments` (CRM contact/deal-scoped). Both intentional/legitimate, but the boundaries need documenting before the cognition layer reasons over them. `crm status` reads only the `contacts`/`public.commitments` side.

**CRM-1 — `leads` has no real meaning (low).** No lead-status concept in RDS (`leads` returns same as `contacts`). Lead-status probably belongs on `deals`/pipeline, not contacts. Future schema decision, not a bug.

**INBOX-1 — triage doesn't populate inbox-zero (medium).** Triage summarizes live but never calls `upsert_thread`, so the snooze/waiting/done lifecycle has no data. Either wire triage to persist threads, or retire the lifecycle if superseded.

**CAL-1 — double calendar-audit write (low).** Three create/delete sites now write `acos.calendar_audit` twice (`log_calendar_action` + `_audit_calendar_write`). Dedupe; decide canonical writer.

**SCHEMA-DRIFT — deploy must run migrations (process).** Merging a migration doesn't apply it (016/017 were unapplied for weeks). Add `run_migrations.py` to the deploy path or a scheduled drift check (it's idempotent-safe).

**VAULT-UNAPPROVE — gated `unapprove <n>` reversal (low).** A gated reversal of an approved vault proposal using its `target_ref` (`commitment:N` / `dossier_entry:N` / `org_note:N`): flip the proposal back to pending and undo/retire the written row. Human-gated (propose-then-confirm); do not build the auto-path. Deferred from OPS-1.

**OPS-RUNBOOK — runbook registry expansion (low).** `artemis/opsdiag.py` seeds vault-pat-auth / vault-secret-missing / vault-clone-network / google-oauth-refresh / rds-unreachable. Add a TLS/cert-expiry class (feed the existing SSL monitor's findings through `classify`), and more classes as new failure shapes surface in the audit log (`action='failure'`, `metadata.failure_class`).

**EXTRACT-DEDUPE-MONITOR — extraction dedupe monitoring (low).** The OPS-1 prompt tuning added cross-type dedupe + commitment-direction + decision-ownership discipline. Watch the proposal stream for residual duplicates (same fact under two types) and mis-directed commitments; if the prompt guidance proves insufficient, add a deterministic post-extraction dedupe pass over `vault.extraction_proposal` before proposals surface.

**Papercuts.** SIGTERM-ignored shutdown (90s SIGKILL every restart — likely websocket/scheduler not closing on signal); Mattermost websocket flap (~60s reconnect loop); SSO re-auth friction (longer session or self-healing ProxyCommand); Mac-vs-EC2 prompt confusion (distinct prompt / dedicated tab).

---

## 7. Operating disciplines (non-negotiable)

Propose-then-confirm · the Brad Spaits rule (no autonomous external comms; activation gates) · trust-the-data-not-the-report · verify-on-the-live-box · statistics-vs-semantics wall · generated-vs-authored split · CT-anchored "today" · one system of record (RDS) · no-tokens-on-disk (Secrets Manager) · solo-scale (no enterprise patterns) · `feat/*`→PR→`main`, migrate-first deploy. Full detail in `CLAUDE.md`.

---

## 8. Why the order

1. **Unified state (✅ done)** — no trustworthy analytical/cognition layer over a split-brain. Done first, correctly.
2. **Knowledge layer (vault) (✅ v1 — PB-011)** — the semantic surface Ryan authors; the synthesis surfacer needs it populated. Precedes the cognition medallion (the June decision, now reality): the cognition layer reasons *over* adjudicated knowledge, so the knowledge substrate is laid first.
3. **Cognition layer** — learning/self-proposal needs one coherent decision log. (HEALTH-1's confabulating stub is what happens when this is half-built and ungated — build it deliberately.)
4. **Bounded autonomy** — safe only once state is trustworthy, knowledge adjudicated, decisions logged, automations gated.

The migration was load-bearing foundation, now laid. The system has one source of truth; the next layers can be built on solid ground.
