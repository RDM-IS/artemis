# RDMIS Ops Dashboard

Real-time operations dashboard for RDMIS, designed for always-on TV display.
Connects to the RDMIS CRM API (Lambda + API Gateway) and renders four panels:
**Survival**, **Pipeline**, **Action Items**, and **ACOS Health**.

## Architecture

```
ops-dashboard/          Vite + React SPA (no TypeScript)
  src/
    Dashboard.jsx       Single-file dashboard component
    App.jsx             Root component
    main.jsx            React entry point
  index.html            Shell with base styles
  vercel.json           SPA rewrites for Vercel deployment
```

**API Backend**: FastAPI Lambda at
`https://inolj7bn99.execute-api.us-east-1.amazonaws.com/default/rdmis-crm-api`

**Endpoint consumed**: `GET /dashboard/full` (requires `x-api-key` header)

**Hosting target**: Vercel (ops.rdm.is)

## Panels

### 1. Survival (top-left)
- MRR vs $45K target with progress bar
- Founder loan balance
- Runway calculation (shows "Pre-Revenue" when MRR is zero)
- Monthly expense breakdown: total, infrastructure, SaaS

### 2. Pipeline (top-right)
- Active deals from the CRM
- Company name, contact, stage, deal value, status badge
- Status colors: hot (orange), warm (gold), active (blue), cold (gray)

### 3. Action Items (bottom-left)
- Up to 8 pending items from ACOS action queue
- High-priority items highlighted with orange left border
- Shows "All clear" when queue is empty

### 4. ACOS Health (bottom-right)
- System status with green/red indicator dot
- Version, active job count, last brief timestamp, uptime percentage

## Data Flow

1. On mount, fetches `GET /dashboard/full` with the API key
2. Parses combined JSON payload containing all four panel datasets
3. If the API is unreachable, falls back to hardcoded mock data
4. Auto-refreshes every 60 seconds
5. Header shows **LIVE** (green) or **MOCK** (gold) badge

## Design System

| Token     | Hex       | Usage                     |
|-----------|-----------|---------------------------|
| VOID      | `#07070A` | Page background           |
| SHADOW    | `#12121A` | Panel background          |
| MIST      | `#2A2A35` | Borders, muted elements   |
| SIGNAL    | `#C8521A` | Alerts, hot status        |
| ORACLE    | `#C8922A` | Warnings, warm status     |
| MOONSTONE | `#9FB8C8` | Labels, secondary text    |
| ARROW     | `#EDE8E0` | Primary text              |
| GREEN     | `#2D7A4F` | Success, online status    |
| EMBER     | `#7A2E0A` | Reserved accent           |

**Fonts**: Georgia (body text), Courier New (data, labels, mono values)

**Aesthetic**: Bloomberg Terminal meets private equity war room

## Local Development

```bash
cd ops-dashboard
npm install
npm run dev
```

Opens at http://localhost:5173. The dashboard will attempt to hit the
live API; if CORS or network issues occur locally, it falls back to
mock data automatically.

## Build and Deploy

```bash
npm run build     # outputs to dist/
```

### Vercel

Connect the repo and set:
- **Root Directory**: `ops-dashboard`
- **Build Command**: `npm run build`
- **Output Directory**: `dist`
- **Domain**: `ops.rdm.is`

The `vercel.json` handles SPA routing via catch-all rewrite.

## Environment Notes

- The API key is embedded in the client bundle. This is intentional
  for an internal ops tool. The API Gateway also has IP restrictions
  and the key only grants read access to dashboard endpoints.
- CORS is enabled on the API (`allow_origins: ["*"]`) to support
  both local dev and the Vercel deployment.
- No environment variables are required; all config is in
  `Dashboard.jsx` constants.

## Extending

To add a new panel:

1. Add a helper function in `api/app/routers/dashboard.py` (backend)
2. Include it in the `/dashboard/full` response
3. Create a `<NewPanel>` component in `Dashboard.jsx`
4. Add it to the grid in the main `Dashboard` component
