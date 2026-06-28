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

---

## 5. In flight 🚧

Nothing mid-migration. Next build is **HEALTH-1** (below) — top of backlog.

---

## 6. Backlog (prioritized)

**HEALTH-1 (high) — morning check-in misroute + confabulation loop.** A health check-in ("sleep 7 energy 5 …") is not logged. `detect_health_intent` correctly returns `log_morning_state` and `handle_morning_intent` works (verified: writes `health.daily_state`, returns "Logged: …"). But the LLM classifier returns `general_reply` (0.95), a "Correction re-route" sends it to a confabulating `add_note` path that "learns" fake rules (including rules about its own errors) and captures the wrong message as the note. **Root causes:** (a) deterministic health intent not short-circuiting the LLM classifier; (b) a premature `add_note`/"learning" stub live in routing with no backing store. **Fix:** make positive `detect_health_intent` unoverridable → `handle_morning_intent`; disable/gate the `add_note` stub out of live routing. Verify by re-sending the check-in via Mattermost.

**CRM-2 / COMMIT-1 — two-store seams (medium).** Two contact stores: `public.contacts`/`organizations` (CRM API) vs `public.persons`/`companies` (Write Guard, PB-008). Two commitment stores: `acos.commitments` (personal tracker) vs `public.commitments` (CRM contact/deal-scoped). Both intentional/legitimate, but the boundaries need documenting before the cognition layer reasons over them. `crm status` reads only the `contacts`/`public.commitments` side.

**CRM-1 — `leads` has no real meaning (low).** No lead-status concept in RDS (`leads` returns same as `contacts`). Lead-status probably belongs on `deals`/pipeline, not contacts. Future schema decision, not a bug.

**INBOX-1 — triage doesn't populate inbox-zero (medium).** Triage summarizes live but never calls `upsert_thread`, so the snooze/waiting/done lifecycle has no data. Either wire triage to persist threads, or retire the lifecycle if superseded.

**CAL-1 — double calendar-audit write (low).** Three create/delete sites now write `acos.calendar_audit` twice (`log_calendar_action` + `_audit_calendar_write`). Dedupe; decide canonical writer.

**SCHEMA-DRIFT — deploy must run migrations (process).** Merging a migration doesn't apply it (016/017 were unapplied for weeks). Add `run_migrations.py` to the deploy path or a scheduled drift check (it's idempotent-safe).

**Papercuts.** SIGTERM-ignored shutdown (90s SIGKILL every restart — likely websocket/scheduler not closing on signal); Mattermost websocket flap (~60s reconnect loop); SSO re-auth friction (longer session or self-healing ProxyCommand); Mac-vs-EC2 prompt confusion (distinct prompt / dedicated tab).

---

## 7. Operating disciplines (non-negotiable)

Propose-then-confirm · the Brad Spaits rule (no autonomous external comms; activation gates) · trust-the-data-not-the-report · verify-on-the-live-box · statistics-vs-semantics wall · generated-vs-authored split · CT-anchored "today" · one system of record (RDS) · no-tokens-on-disk (Secrets Manager) · solo-scale (no enterprise patterns) · `feat/*`→PR→`main`, migrate-first deploy. Full detail in `CLAUDE.md`.

---

## 8. Why the order

1. **Unified state (✅ done)** — no trustworthy analytical/cognition layer over a split-brain. Done first, correctly.
2. **Cognition layer** — learning/self-proposal needs one coherent decision log. (HEALTH-1's confabulating stub is what happens when this is half-built and ungated — build it deliberately.)
3. **Knowledge layer (vault)** — the semantic surface Ryan authors; the synthesis surfacer needs it populated.
4. **Bounded autonomy** — safe only once state is trustworthy, decisions logged, meaning adjudicated, automations gated.

The migration was load-bearing foundation, now laid. The system has one source of truth; the next layers can be built on solid ground.
