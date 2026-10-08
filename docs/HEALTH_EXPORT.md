# HEALTH-EXPORT-1 — nutrition to Apple Health, for the VA nutritionist

**Status: built, not deployed, ships OFF.** Authoritative for the export's rules and for the Shortcut. Backlog entry: `docs/ARTEMIS_STATE.md` → HEALTH-EXPORT-1.

## The path

```
nutrition.entry ──► Lambda /api/nutrition/export ──► iOS Shortcut ──► Apple Health
                                                                          │
                                       VA "Share My Health Data" app ◄────┘
                                                    │
                                                    ▼
                                              the nutritionist
```

Artemis cannot write to Apple Health; only something running on the phone can. The VA's public health API is read-only as far as is known, so Apple Health is the one inbound route, and it is the route weight already takes.

**What is confirmed, and what is not (2026-10-08):**

| Fact | State |
|---|---|
| Share My Health Data is granted read access to Dietary Energy, Protein, Carbohydrates, Total Fat and Fiber | **Confirmed** — Health → Apps → SMHD, all five on |
| It does not request Dietary Sugar | **Confirmed** — absent from the list. Sugar also has no data source in `nutrition.entry` |
| Weight reaches the nutritionist by this route | **Confirmed** by Ryan |
| The nutritionist's screen shows nutrition samples at all | **NOT confirmed.** Ask at the next appointment |
| Per-meal samples survive, rather than being summed per day | **NOT confirmed** |

The design does not depend on the two open rows: per-slot samples sum to the right daily total either way. If nutrition turns out not to be displayed, the dietitian report (`@artemis dietitian report`) is the fallback and nothing here is wasted except the Shortcut.

## The rules

Apple Health is **write-only from a Shortcut** — a sample can be logged and cannot be deleted or amended. Every rule follows from that.

1. **Only settled days.** A day is offered once its 48 h correction window has closed: on local date *T*, days up to *T − 3*. Sending today's 00:15 pre-fill would put the plan into a clinical record before `fix` could change it. **The cost is a three-day lag**, which a monthly review does not notice.
2. **One sample set per (day, slot)**, not per entry: a slot's entries are deleted and replaced by a correction, and Health carries no food name anyway.
3. **A day goes whole or not at all.** A day with a slot missing looks, in Health, like a day he ate less.
4. **A NULL macro withholds the day.** Summing it as zero would understate intake. Zero is a value; NULL is not.
5. **An unconfirmed day is withheld by default.** A day still `assumed` / `locked_unconfirmed` is the plan, never confirmed — and Health has no way to label it so, where the dietitian report does. `include_unconfirmed: true` sends them. **This is the one setting that decides how full the feed is; see Activation.**
6. **Exactly once.** A batch is *claimed* before the Shortcut writes and *acked* after. While a claim is open, no new batch is handed out; the next run reports `stuck` and asks. The server cannot see Health, so it never guesses whether a write landed.
7. **The timestamp is a convention.** `nutrition.entry` records no time eaten. Each slot is stamped with the clock time configured for it, on its date, in the active timezone. The four times are Ryan's and have no default.
8. **Nothing is re-sent.** If a sent day's rows change afterwards, `GET /status` reports it under `drift`. It is not corrected automatically, because it cannot be.

A slot that totals zero on every nutrient is skipped. A day with no entries (an unpicked off day, `no_plan`) sends nothing — never zeros.

## Endpoints

All under `/api/nutrition/export`, all `X-API-Key: <export key>`.

| | |
|---|---|
| `POST /claim` | Body optional: `{"max_days": 7}` (1–14). Returns `status`, `message`, `items`, and `batch_id` when there is one |
| `POST /ack` | `{"batch_id": "…"}` — the batch is in Health. Idempotent |
| `POST /release` | `{"batch_id": "…"}` — the batch is NOT in Health; offer it again. Refused for an acked batch |
| `GET /status` | Config, sent totals, any open batch, ready days, withheld days with reasons, drift |

`claim` statuses: `ok` · `empty` · `stuck` · `disabled` · `misconfigured`. Every reply carries a `message` written to be shown as-is.

An item:

```json
{"day": "2031-03-10", "slot": "lunch", "at": "2031-03-10T11:30:00-06:00",
 "kcal": 1100, "protein_g": 71.0, "carb_g": 82.0, "fat_g": 31.0, "fiber_g": 21.0}
```

## Deploy (Ryan runs the AWS steps — ACCOUNT-CHANGES)

1. **Create the key.** Its own secret, so a leak from the phone exposes settled macro totals and nothing else:
   ```
   aws secretsmanager create-secret --name rdmis/dev/health-export-key \
     --secret-string "{\"api_key\":\"$(openssl rand -hex 32)\"}" --region us-east-1
   ```
2. **Confirm the Lambda's role can read it.** If its policy lists secrets by name rather than by `rdmis/dev/*`, the new ARN has to be added — an IAM change, so Ryan's. Without it every export route returns 500 on first use while the rest of the API is unaffected.
3. **Migrate first:** apply `054_health_export.sql` and confirm `nutrition.health_export` exists before deploying the Lambda. It is additive; nothing existing reads it.
4. **Deploy the Lambda** (`PATH=/opt/homebrew/bin:$PATH bash deploy.sh`). No box change: nothing in `artemis/` is touched.
5. **Verify, still switched off:** `POST /claim` with the key → `{"status": "disabled", …}`; with the display key → 401.

## Activation (Ryan's, and it forces the two decisions)

The migration seeds `{"enabled": false}`. Turning it on needs a start date and four meal times — there are no defaults — and a choice about unconfirmed days:

```sql
UPDATE nutrition.prep_config SET updated_at = now(), value = '{
  "enabled": true,
  "start_date": "YYYY-MM-DD",
  "include_unconfirmed": false,
  "slot_times": {"breakfast": "HH:MM", "lunch": "HH:MM",
                 "snacks": "HH:MM", "dinner": "HH:MM"}
}'::jsonb WHERE key = 'health_export';
```

- **`start_date`** — the first day to send. Everything settled from that day on goes out on the first runs, oldest first, seven days per run.
- **`include_unconfirmed`** — `false` sends only days Ryan corrected or logged into. `true` also sends days that stood as planned and were never touched. On the first fortnight of data that was the difference between sending nothing and sending every work day (3 of 3 recorded days were `locked_unconfirmed`). The tracker's own stance is default-to-plan; the argument against is that Health cannot mark a number as assumed.
- **`slot_times`** — when each slot is stamped. `snacks` is one slot holding both the afternoon and the evening snack, so it gets one time.

Then `GET /status` and read `ready_days` and `withheld` **before** building the Shortcut's automation: it shows exactly what the first run will send.

## The Shortcut — "Send meals to Health"

Shortcuts files are signed and cannot be generated from the repo, so this is built by hand, once. `BASE` is the API Gateway URL the Health Auto Export app already posts to, ending before `/api/health/ingest`.

1. **Text** → the export key. Rename the variable `Key`.
2. **Get Contents of URL** → `BASE/api/nutrition/export/claim` · Method **POST** · Headers `X-API-Key` = `Key` · Request Body **JSON** (empty). Rename the result `Claim`.
3. **Get Dictionary Value** `status` from `Claim` → variable `Status`. Likewise `message` → `Message`, `batch_id` → `Batch`.
4. **If** `Status` **is** `stuck`:
   - **Choose from Menu**, prompt `Message`, with two items:
     - **"Yes — those meals are in Health"** → Get Contents of URL `BASE/api/nutrition/export/ack`, POST, same header, JSON body `batch_id` = `Batch`.
     - **"No — none of them are"** → the same against `…/release`.
   - **Stop This Shortcut.**
   - *If only some reached Health: delete those days' Dietary Energy, Protein, Carbohydrates, Total Fat and Fiber entries whose source is Shortcuts, in the Health app, then run again and answer No.*
5. **If** `Status` **is not** `ok`: **Show Notification** `Message`, then **Stop This Shortcut.** (This is the ordinary daily ending: "Nothing new to send to Health.")
6. **Get Dictionary Value** `items` from `Claim`, then **Repeat with Each** item:
   - **Get Dictionary Value** `at` from Repeat Item → **Get Dates from Input** → variable `When`.
   - **Log Health Sample** ×5, each with **Date** = `When`:

     | Type | Value (from Repeat Item) | Unit |
     |---|---|---|
     | Dietary Energy | `kcal` | kcal |
     | Protein | `protein_g` | g |
     | Carbohydrates | `carb_g` | g |
     | Total Fat | `fat_g` | g |
     | Fiber | `fiber_g` | g |
7. After the loop: **Get Contents of URL** `BASE/api/nutrition/export/ack`, POST, same header, JSON body `batch_id` = `Batch`. **Show Notification** with its `message`.

**Step 7 must be last and must not be skipped on error.** If the Shortcut stops anywhere in step 6, the batch stays claimed and the next run asks — that is the design working, not a fault.

### First run — check these, they are not verified from here

- **Run it by hand first**, with `{"max_days": 1}` as the claim body, and compare the one day in Health → Browse → Nutrition against `GET /status`.
- **A zero value.** A slot can legitimately carry `fiber_g: 0`. HealthKit accepts a zero-quantity sample; whether the Shortcuts action does was not tested. If it errors, the batch is left claimed (nothing is lost) — the fix would be server-side, sending `null` for a zero and an If around each Log action; it is not built because it may not be needed.
- **A locked phone.** Health data is unreadable while the phone is locked, and logging from a time-of-day automation may fail for the same reason. If so, run it from the Home Screen, or trigger the automation on something that implies an unlocked phone.
- **The sample's source reads "Shortcuts"**, not Artemis. That is how to find and delete them if a batch ever has to be undone by hand.

## Not built, on purpose

- **Sugar.** No source in `nutrition.entry`, and the VA app does not read it.
- **Saturated fat, sodium, cholesterol, water.** The VA app reads all four. `nutrition.food` has `sodium_mg` but `nutrition.entry` does not, and the others are not recorded anywhere. Worth adding at the source given the LDL goal; a schema change of its own.
- **Meal names.** Health has nowhere to put them. What he ate stays in the dietitian report.
- **Body-fat percentage and workouts.** The VA app reads both; neither is nutrition, and workouts already reach Health from the watch.
- **Same-day export.** Would mean writing samples that a `fix` could later contradict, with no way to take them back.
- **A Mattermost command.** Status and resolution are in the API and the Shortcut; the box is untouched.
