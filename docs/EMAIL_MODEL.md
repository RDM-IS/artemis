# Email Model — Artemis / ACOS

> Authoritative spec for how Artemis sees, surfaces, and acts on email. Supersedes the `inbox_threads` state machine and issues INBOX-1/2/3. The old "inbox-zero tracker as source of truth" is **retired** — Gmail is the source of truth.

## Core principle

**Gmail is the source of truth for email state. Labels are the state.** Artemis never maintains a separate authoritative model of inbox state. `acos` tables are a *queryable mirror* (for speed) and an *action ledger* (for accountability) — never the authority. If a mirror and Gmail disagree, **Gmail wins and the mirror is corrected.**

## The two paths

Every email is owned by exactly one decision-maker:

- **Path 1 — Ryan's attention.** No playbook claims it → it stays in `INBOX`, surfaces to Ryan, and waits. Ryan chooses: **Act / Snooze / Dispose**. This is the **default** — unmatched email is always Path 1.
- **Path 2 — Artemis's attention.** A playbook (or, later, a promoted learned rule) claims it. Default behavior is **propose** (draft the action, ask). A playbook is **promoted to auto** only by explicit human authorization, per-playbook. Nothing auto-acts by default.

Paths hand off: a playbook may act partway then return to Ryan (propose); Ryan's "Act" may invoke a playbook.

## The two invariants (both checkable)

**1. Location invariant.** Every email is in exactly one of two states:
- **In `INBOX`** ⟺ Path 1, waiting on Ryan. Has the `INBOX` label.
- **Out of `INBOX`** ⟺ acted on. MUST have an `@artemis/<label>` AND a matching `acos.audit_log` entry.

There is no third state. Out-of-inbox-without-`@artemis/*`-label = violation (orphan). `@artemis/*`-label-without-audit-entry = violation (unlogged/confabulated action). Audit-says-done-but-still-`INBOX` = violation (false report).

**2. Visibility invariant.**
- **Path 1 → pushed** to Mattermost. If Artemis surfaces an email to Ryan, it IS in the inbox and Ryan must act. Surfacing = needs-you.
- **Path 2 → silent.** Playbook actions never post to Mattermost. They are pulled on demand: `audit email log <window>`.

## Actions

### Dispositions (internal, reversible) — execute on direction
- **`archive ##`** → mark read · add `@artemis/archive` · remove `INBOX` · log.
- **`file ## as <category>`** → add `@artemis/<category>` · remove `INBOX` · log.
- **`delete ##`** → move to Trash · log.
- **`spam ##`** → move to Spam (trains filter) · log.
- **`snooze ## <when>`** → remove `INBOX` · add `@artemis/snoozed` · set timer · log. **Re-adds `INBOX` at the timer** (snooze is reversible by schedule; it never silently eats mail).

### Outbound (external, irreversible) — ALWAYS draft-then-confirm
- **`reply to ## with ___`**, **`forward ##`**, **send** → Artemis **drafts**, posts the draft to Ryan, waits for explicit send-confirmation, THEN sends. **Even when Ryan directs it** — the send itself is the Brad-Spaits boundary. A playbook may NEVER auto-send; it can only draft-and-propose.

### Disposition grammar (parsing rules)
- **Numbers** are listing indices: singles, comma lists, `&`/`and`, and inclusive ranges (`1-4`). All forms mix: `archive 1-3, 7, 9-11`.
- **Categories may be multi-word** and are slugified: `file 1 as founder loan` → `@artemis/founder-loan`; `file 5-7, 13 as founder loans` → `@artemis/founder-loans`. (Filing an email is a label move only — it is **not** a financial transaction and writes nothing to the founder-loan ledger.)
- **Compound batches** put several groups on one line; numbers may sit before or after the verb: `1-4 archive  5-7 file as founder loans  14 delete`. A `file` category runs until the next number-or-verb token. Parentheticals are stripped (`14 delete (was a test)`).
- **Reversible-only batches** (archive/file) auto-execute with a parse readback. **Batches containing delete/spam** are proposed-then-confirmed (read back the parsed plan, wait for `yes`/`no`) — the inference is the risky surface, so the parse is confirmed before any destructive act.
- **Shape guard (anti-misroute):** a disposition-shaped line (verb + number) that fails to parse while a listing is active is **refused in context** — it must never fall through to the keyword/financial/LLM classifier. This closes the class where `file … as founder loans` mis-fired the read-only financial report.

## Data model

| Store | Role | Lifetime |
|---|---|---|
| **Gmail** | Source of truth. Labels = state. Every email, forever. | permanent |
| **`acos.email_index`** | Queryable mirror of the **active working set** so Artemis sees the WHOLE inbox at once (not a 5/20 page). | active only — see disposition |
| **`acos.audit_log`** | Every Artemis **action**, with provenance. The accountability ledger and the ML training corpus. | permanent |

### `acos.email_index` — the working set
Exists for ONE reason: let Artemis query/list/match across the **entire** inbox in one fast query, instead of Gmail API pagination. It is **not** acted through — Artemis decides from the index, acts against Gmail.

- **Holds:** in-`INBOX` (Path 1) + snoozed (will resurface) + proposed-awaiting-confirm. The active queue.
- **Drops on terminal disposition** (archived/deleted/filed/spam) — history lives in `audit_log` + Gmail, so the index stays small and means "currently active."
- **Columns:** gmail message_id, thread_id, sender, sender_domain, subject, snippet, received_at, is_unread, current_labels[], path (1/2), pb_match (nullable), state (inbox/snoozed/pending), snooze_until (nullable), indexed_at.
- **No audit entry for indexing** — indexing is observation, not action.

### `acos.audit_log` — actions + ML corpus
Every state-changing action. **Designed as future training data** — capture the features a learner will need:
- `action` (archive/file/delete/spam/snooze/reply/forward/label)
- `message_id`, `thread_id`
- `source` — **`user_directed`** | **`playbook:<id>`** | **`playbook:<id>:confirmed`** (who decided)
- `action_class` — `disposition` | `outbound`
- email features at decision time: `sender`, `sender_domain`, `subject`, `had_attachment`, `received_hour`, `prior_labels[]`
- result: `applied_labels[]`, `removed_labels[]`, `approved` (for outbound), `verified` (Gmail confirmed), `ts`

This schema makes "how does Ryan dispose of emails like this" a query, which is the foundation of the learning layer.

## The command flow (every inbox command)

1. **Sync** `email_index` from Gmail (cheap — list IDs + labels for the working-set query).
2. **Query** the index (fast, full inbox) to list / find / match.
3. **Act** against **Gmail** (the real archive/label/trash/send).
4. **Verify** Gmail actually changed (re-read the label state).
5. **Update** `email_index` + write `audit_log` to match verified reality.
6. **Report** only what step 4 verified. Never report success on an unverified action.

## Surfacing at scale

- **Working set (v1):** `in:inbox` = "needs action." Everything in the inbox needs disposition regardless of read state — Ryan reads mail without dispositioning it, so `is:unread` would hide the real working set. Read-state is a display hint (`is_unread`), **not** the filter.
- **Listing:** numbered, paginated — `inbox` shows the first page (default 20) with `more` to continue, plus a total count ("23 need action — showing 1–20"). IDs are fresh per listing; act on them immediately (`archive 2, 5, 9`).
- Full-inbox visibility is the hard requirement: Artemis can see and act on email #1–95, not just the last 5.

## Reconciliation check (the trust mechanism)

On demand (`@artemis check inbox integrity`; scheduled later): scan Gmail labels + `email_index` + `audit_log` and report invariant violations — orphans (out-of-inbox, no `@artemis/*`), unlogged actions (label, no audit), false reports (audit says done, still `INBOX`), stale index (index ≠ Gmail). **Don't trust the report — check the invariant.**

## The learning layer (future — corpus first)

Not built now. The path:
- **Layer 0 — deterministic rules** (author explicit `if/then`; most value, no ML).
- **Layer 1 — frequency statistics** over `audit_log` ("archived 14/14 from Etsy marketing → propose a rule"). Counting, interpretable, proposes rules Ryan blesses.
- **Layer 2 — feature classifier** (when counting isn't enough): predict Path-1-vs-Path-2 + confidence from index features; **proposes**, never acts.
- **Layer 3 — LLM classification**, bounded: proposes with reasoning, overridable by Layers 0–1, never auto-acts.

Maps to the cognition medallion: `audit_log` = bronze (corpus) · authored rules = silver (machine-trusted to propose) · learned patterns = gold (confidence-weighted, inform-not-act). **Artemis surfaces patterns and proposes; Ryan promotes. Artemis never self-promotes.**

**Prerequisite:** a clean `audit_log` of real decisions. The inbox rebuild is what produces it. Ship the model, accumulate weeks of decisions, then mine. Counting before classifying; proposing before acting; Ryan promotes before anything auto-runs.
