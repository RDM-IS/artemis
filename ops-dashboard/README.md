# Engagement Ops

Two-level engagement operations UI (OPS-2). A Vite + React SPA (no TypeScript)
that surfaces the ACOS approval queue, commitments, dossiers, and projects for
each engagement.

## Two levels

```
/            Portfolio  — 30,000-foot strip: one card per engagement,
                          global ACOS health, portfolio-wide pending badge.
/e/:slug     Engagement — approval queue (centerpiece), commitments,
                          dossier lookup, projects, health strip.
```

Routing is `react-router-dom`. `src/main.jsx` wraps the app in `BrowserRouter`;
`src/App.jsx` holds the `<Routes>`.

## Security — no embedded API key

There is **no API key in the client**. The API sits behind **Cloudflare
Access**; the browser carries the Access identity via cookies automatically.
Every fetch uses `credentials: "include"` and sends **no** `x-api-key` header.
If a request returns 401/403 (Access not passed), the UI shows a clear "Not
authenticated — reload to sign in via Cloudflare Access" message.

## Configuration

Single build-time variable, read in `src/api.js`:

```
VITE_OPS_API_BASE=      # empty = same-origin (recommended)
                        # or a full origin, e.g. https://ops-api.rdm.is
```

See `.env.example`. Copy to `.env` to override locally. No secrets live here.

## API contract

All routes are under `/api` and require Cloudflare Access (enforced at the edge,
not in this code). Reads: `GET /api/portfolio`, `GET /api/engagements/<slug>`,
`GET /api/dossier/search|person/<slug>|org/<org>`. Mutations (POST, JSON):
proposal approve/reject/batch, dossier-draft approve/reject (approve/reject only
— no edit in v1), and commitment close. After any mutation the engagement view
re-fetches from the server so displayed state is authoritative, not optimistic.

## Design system

Dark-terminal aesthetic with two reserved 70s accents. Tokens live in
`src/theme.js`.

| Token        | Hex       | Usage                                             |
|--------------|-----------|---------------------------------------------------|
| VOID         | `#07070A` | Page background                                   |
| SHADOW       | `#12121A` | Panel background                                  |
| MIST         | `#2A2A35` | Borders, muted elements                           |
| SIGNAL       | `#C8521A` | Alerts, primary accent                            |
| ORACLE       | `#C8922A` | Warnings, secondary accent                        |
| MOONSTONE    | `#9FB8C8` | Labels, secondary text                            |
| ARROW        | `#EDE8E0` | Primary text                                      |
| GREEN        | `#2D7A4F` | Success, online status                            |
| EMBER        | `#7A2E0A` | Deep accent (badges)                              |
| AVOCADO      | `#A9B14A` | **ONLY** approve / confirm actions                |
| BURNT_ORANGE | `#D86E2C` | Attention: overdue, stale, pending, near hard-date|

**Fonts**: Georgia (body), Courier New (data / labels / mono values).

**Mobile-usable**: the dossier lookup is used on a phone in a hallway; the
approval queue on a laptop. Responsive flex/grid, wrapping rows, 44px tap
targets, no horizontal body scroll.

## File layout

```
src/
  main.jsx                  BrowserRouter entry
  App.jsx                   <Routes>
  theme.js                  color + font tokens
  features.js               feature flags ({ legacyPanels: false })
  api.js                    fetch helpers (credentials:"include", no key)
  format.js                 date / duration display helpers
  views/
    Portfolio.jsx           / — portfolio strip
    Engagement.jsx          /e/:slug — the working surface
  components/
    Panel.jsx               titled dark container
    Badge.jsx               mono pill
    States.jsx              Loading / ErrorState / EmptyLine
    HealthStrip.jsx         <HEALTH> strip (renders nulls as —)
    ProposalCard.jsx        approval item + inline edit + batch checkbox
    DossierDraftCard.jsx    dossier draft (approve/reject only)
    CommitmentRow.jsx       commitment + Close
    DossierSearch.jsx       read-only person/org lookup + detail drawer
  legacy/
    LegacyDashboard.jsx     parked RDMIS survival/pipeline/revenue dashboard
```

## Parked legacy dashboard

The old RDMIS survival / pipeline / revenue dashboard is **parked, not deleted**
in `src/legacy/LegacyDashboard.jsx`. It renders nowhere by default. Flip
`features.legacyPanels` to `true` in `src/features.js` to expose it at `/legacy`.

## Local development

```bash
cd ops-dashboard
npm install
npm run dev        # http://localhost:5173
```

## Build and deploy — Cloudflare Pages + Access

```bash
npm run build      # outputs to dist/
```

- **Root Directory**: `ops-dashboard`
- **Build command**: `npm run build`
- **Output directory**: `dist`
- **SPA fallback**: `_redirects` (`/* /index.html 200`) — kept for Cloudflare
  Pages so client-side routes like `/e/<slug>` resolve on hard reload.
- **Access**: put the Pages project (and the API) behind a Cloudflare Access
  application so the identity cookie is present for `credentials:"include"`
  requests. No API key is provisioned or embedded.
