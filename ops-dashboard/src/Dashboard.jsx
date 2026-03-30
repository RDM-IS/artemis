import { useState, useEffect, useCallback } from "react";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const API_BASE =
  "https://inolj7bn99.execute-api.us-east-1.amazonaws.com/default/rdmis-crm-api";
const API_KEY = "kBAuGh_itvJI797R1L3CKuRIbwGXIgwy2beeg1VqVxw";
const REFRESH_MS = 60_000;

// ---------------------------------------------------------------------------
// Brand tokens
// ---------------------------------------------------------------------------

const C = {
  VOID: "#07070A",
  SHADOW: "#12121A",
  MIST: "#2A2A35",
  SIGNAL: "#C8521A",
  ORACLE: "#C8922A",
  MOONSTONE: "#9FB8C8",
  ARROW: "#EDE8E0",
  GREEN: "#2D7A4F",
  EMBER: "#7A2E0A",
};

const FONT_BODY = "Georgia, serif";
const FONT_MONO = "'Courier New', Courier, monospace";

// ---------------------------------------------------------------------------
// Mock data — fallback when API is unreachable
// ---------------------------------------------------------------------------

const MOCK = {
  survival: {
    mrr_cents: 0,
    mrr_target_cents: 4500000,
    client_count: 0,
    expenses_month: {
      total_cents: 84700,
      infra_cents: 31200,
      saas_cents: 53500,
      month: "2026-03",
    },
    founder_loan_balance_cents: 240000,
    runway_months: null,
    pre_revenue: true,
  },
  pipeline: [
    {
      id: "mock-1",
      company_name: "TTI",
      contact_name: "Brian Pivar",
      stage: "Gate 2",
      value_cents: 12500000,
      status: "warm",
      next_action: "Follow-up Apr 2",
      updated_at: "2026-03-26",
    },
    {
      id: "mock-2",
      company_name: "Milwaukee Tool",
      contact_name: "Srinivasan",
      stage: "Intro",
      value_cents: 0,
      status: "active",
      next_action: "Discovery call scheduled",
      updated_at: "2026-03-28",
    },
    {
      id: "mock-3",
      company_name: "Acme",
      contact_name: "Clive",
      stage: "Demo",
      value_cents: 8000000,
      status: "hot",
      next_action: "Demo Apr 7",
      updated_at: "2026-03-29",
    },
  ],
  action_items: [
    {
      id: "ai-1",
      item_type: "scheduling_request",
      priority: "high",
      title: "Clive demo Apr 7",
      description: "Prepare demo environment",
      due_at: "2026-04-07T09:00:00",
      created_at: "2026-03-29T10:00:00",
    },
    {
      id: "ai-2",
      item_type: "follow_up",
      priority: "high",
      title: "Brian followup Apr 2",
      description: "Send revised SOW",
      due_at: "2026-04-02T09:00:00",
      created_at: "2026-03-28T14:00:00",
    },
    {
      id: "ai-3",
      item_type: "follow_up",
      priority: "normal",
      title: "Brad advisory Apr 3",
      description: "Advisory board prep",
      due_at: "2026-04-03T09:00:00",
      created_at: "2026-03-27T11:00:00",
    },
    {
      id: "ai-4",
      item_type: "follow_up",
      priority: "normal",
      title: "Databricks Apr 1",
      description: "Partnership review",
      due_at: "2026-04-01T09:00:00",
      created_at: "2026-03-26T16:00:00",
    },
  ],
  acos: {
    status: "online",
    version: "1.3.0",
    jobs_running: 14,
    last_brief: "2026-03-29T08:05:00",
    uptime_pct: 99.97,
  },
  next_events: [],
  generated_at: new Date().toISOString(),
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function cents(v) {
  if (v == null) return "$0";
  const abs = Math.abs(v);
  if (abs >= 100_00) {
    const k = (abs / 100).toLocaleString("en-US", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    });
    return v < 0 ? `-${k}` : k;
  }
  const d = (abs / 100).toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
  });
  return v < 0 ? `-${d}` : d;
}

function relativeTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now - d;
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

const STATUS_COLOR = {
  hot: C.SIGNAL,
  warm: C.ORACLE,
  active: C.MOONSTONE,
  cold: C.MIST,
  closed: C.GREEN,
  unknown: C.MIST,
};

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Panel({ title, children, style }) {
  return (
    <div
      style={{
        background: C.SHADOW,
        border: `1px solid ${C.MIST}`,
        borderRadius: 4,
        padding: "16px 20px",
        display: "flex",
        flexDirection: "column",
        gap: 12,
        overflow: "hidden",
        ...style,
      }}
    >
      <div
        style={{
          fontFamily: FONT_MONO,
          fontSize: 11,
          letterSpacing: 2,
          textTransform: "uppercase",
          color: C.MOONSTONE,
          borderBottom: `1px solid ${C.MIST}`,
          paddingBottom: 8,
        }}
      >
        {title}
      </div>
      <div style={{ flex: 1, overflowY: "auto" }}>{children}</div>
    </div>
  );
}

function ProgressBar({ value, max, color }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  return (
    <div
      style={{
        height: 6,
        background: C.MIST,
        borderRadius: 3,
        overflow: "hidden",
        marginTop: 4,
      }}
    >
      <div
        style={{
          height: "100%",
          width: `${pct}%`,
          background: color || C.SIGNAL,
          borderRadius: 3,
          transition: "width 0.6s ease",
        }}
      />
    </div>
  );
}

function StatusBadge({ status }) {
  return (
    <span
      style={{
        fontFamily: FONT_MONO,
        fontSize: 10,
        letterSpacing: 1,
        textTransform: "uppercase",
        padding: "2px 8px",
        borderRadius: 3,
        background: STATUS_COLOR[status] || C.MIST,
        color: status === "cold" || status === "active" ? C.ARROW : C.VOID,
      }}
    >
      {status}
    </span>
  );
}

function Stat({ label, value, mono, color }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <div
        style={{
          fontFamily: FONT_MONO,
          fontSize: 10,
          color: C.MOONSTONE,
          letterSpacing: 1,
          textTransform: "uppercase",
          marginBottom: 2,
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontFamily: mono ? FONT_MONO : FONT_BODY,
          fontSize: mono ? 14 : 16,
          color: color || C.ARROW,
        }}
      >
        {value}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Panel: Survival
// ---------------------------------------------------------------------------

function SurvivalPanel({ data }) {
  const mrr = data.mrr_cents || 0;
  const target = data.mrr_target_cents || 4500000;
  const exp = data.expenses_month || {};

  return (
    <Panel title="Survival">
      <div style={{ marginBottom: 8 }}>
        <div
          style={{
            fontFamily: FONT_MONO,
            fontSize: 10,
            color: C.MOONSTONE,
            letterSpacing: 1,
            textTransform: "uppercase",
          }}
        >
          MRR
        </div>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
          <span
            style={{
              fontFamily: FONT_MONO,
              fontSize: 32,
              fontWeight: "bold",
              color: mrr > 0 ? C.GREEN : C.SIGNAL,
            }}
          >
            {cents(mrr)}
          </span>
          <span
            style={{ fontFamily: FONT_MONO, fontSize: 13, color: C.MOONSTONE }}
          >
            / {cents(target)}
          </span>
        </div>
        <ProgressBar value={mrr} max={target} color={C.ORACLE} />
      </div>

      <Stat
        label="Runway"
        value={
          data.runway_months != null
            ? `${data.runway_months} months`
            : "Pre-Revenue"
        }
        color={data.pre_revenue ? C.ORACLE : C.GREEN}
      />
      <Stat
        label="Founder Loan Balance"
        value={cents(data.founder_loan_balance_cents)}
        mono
      />

      <div
        style={{
          borderTop: `1px solid ${C.MIST}`,
          paddingTop: 8,
          marginTop: 4,
        }}
      >
        <div
          style={{
            fontFamily: FONT_MONO,
            fontSize: 10,
            color: C.MOONSTONE,
            letterSpacing: 1,
            textTransform: "uppercase",
            marginBottom: 6,
          }}
        >
          Expenses — {exp.month || "—"}
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr 1fr",
            gap: 8,
          }}
        >
          <Stat label="Total" value={cents(exp.total_cents)} mono />
          <Stat label="Infra" value={cents(exp.infra_cents)} mono />
          <Stat label="SaaS" value={cents(exp.saas_cents)} mono />
        </div>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Panel: Pipeline
// ---------------------------------------------------------------------------

function PipelinePanel({ data }) {
  return (
    <Panel title="Pipeline">
      {data.length === 0 && (
        <div style={{ color: C.MOONSTONE, fontStyle: "italic" }}>
          No active deals
        </div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {data.map((deal) => (
          <div
            key={deal.id}
            style={{
              background: C.VOID,
              border: `1px solid ${C.MIST}`,
              borderRadius: 4,
              padding: "10px 14px",
            }}
          >
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginBottom: 4,
              }}
            >
              <span style={{ fontWeight: "bold", fontSize: 15 }}>
                {deal.company_name}
              </span>
              <StatusBadge status={deal.status} />
            </div>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                fontSize: 13,
                color: C.MOONSTONE,
                marginBottom: 4,
              }}
            >
              <span>{deal.contact_name}</span>
              <span style={{ fontFamily: FONT_MONO, fontSize: 12 }}>
                {deal.stage}
              </span>
            </div>
            {deal.value_cents > 0 && (
              <div
                style={{
                  fontFamily: FONT_MONO,
                  fontSize: 14,
                  color: C.ORACLE,
                  marginBottom: 4,
                }}
              >
                {cents(deal.value_cents)}
              </div>
            )}
            {deal.next_action && (
              <div
                style={{
                  fontSize: 12,
                  color: C.ARROW,
                  opacity: 0.7,
                  fontStyle: "italic",
                }}
              >
                {deal.next_action}
              </div>
            )}
          </div>
        ))}
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Panel: Action Items
// ---------------------------------------------------------------------------

function ActionItemsPanel({ data }) {
  return (
    <Panel title="Action Items">
      {data.length === 0 && (
        <div
          style={{
            color: C.GREEN,
            fontStyle: "italic",
            padding: "12px 0",
          }}
        >
          All clear
        </div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {data.map((item) => (
          <div
            key={item.id}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "6px 10px",
              background: C.VOID,
              border: `1px solid ${C.MIST}`,
              borderRadius: 4,
              borderLeft: `3px solid ${
                item.priority === "high" ? C.SIGNAL : C.MIST
              }`,
            }}
          >
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 14 }}>{item.title}</div>
              {item.description && (
                <div
                  style={{
                    fontSize: 11,
                    color: C.MOONSTONE,
                    marginTop: 2,
                  }}
                >
                  {item.description}
                </div>
              )}
            </div>
            <div
              style={{
                fontFamily: FONT_MONO,
                fontSize: 10,
                color: C.MOONSTONE,
                whiteSpace: "nowrap",
              }}
            >
              {item.due_at
                ? new Date(item.due_at).toLocaleDateString("en-US", {
                    month: "short",
                    day: "numeric",
                  })
                : ""}
            </div>
          </div>
        ))}
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Panel: ACOS Health
// ---------------------------------------------------------------------------

function AcosHealthPanel({ data }) {
  const isOnline = data.status === "online";
  return (
    <Panel title="ACOS Health">
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
        <div
          style={{
            width: 10,
            height: 10,
            borderRadius: "50%",
            background: isOnline ? C.GREEN : C.SIGNAL,
            boxShadow: isOnline
              ? `0 0 8px ${C.GREEN}`
              : `0 0 8px ${C.SIGNAL}`,
          }}
        />
        <span
          style={{
            fontFamily: FONT_MONO,
            fontSize: 14,
            color: isOnline ? C.GREEN : C.SIGNAL,
            textTransform: "uppercase",
            letterSpacing: 2,
          }}
        >
          {data.status || "unknown"}
        </span>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <Stat label="Version" value={data.version || "—"} mono />
        <Stat label="Jobs Running" value={data.jobs_running ?? "—"} mono />
        <Stat label="Last Brief" value={relativeTime(data.last_brief)} mono />
        <Stat
          label="Uptime"
          value={data.uptime_pct != null ? `${data.uptime_pct}%` : "—"}
          mono
          color={C.GREEN}
        />
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Main Dashboard
// ---------------------------------------------------------------------------

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [source, setSource] = useState(null); // "live" | "mock"
  const [lastFetch, setLastFetch] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/dashboard/full`, {
        headers: { "x-api-key": API_KEY },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setSource("live");
      setLastFetch(new Date());
    } catch (err) {
      console.warn("Dashboard API fetch failed, using mock data:", err);
      if (!data) {
        setData(MOCK);
        setSource("mock");
        setLastFetch(new Date());
      }
    }
  }, [data]);

  useEffect(() => {
    fetchData();
    const id = setInterval(fetchData, REFRESH_MS);
    return () => clearInterval(id);
  }, [fetchData]);

  if (!data) {
    return (
      <div
        style={{
          height: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontFamily: FONT_MONO,
          color: C.MOONSTONE,
        }}
      >
        Loading...
      </div>
    );
  }

  return (
    <div
      style={{
        height: "100vh",
        display: "flex",
        flexDirection: "column",
        background: C.VOID,
        padding: 16,
        gap: 12,
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "0 4px",
          flexShrink: 0,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span
            style={{
              fontFamily: FONT_MONO,
              fontSize: 14,
              letterSpacing: 3,
              color: C.ARROW,
              textTransform: "uppercase",
            }}
          >
            RDMIS Ops
          </span>
          <span
            style={{
              fontFamily: FONT_MONO,
              fontSize: 10,
              padding: "2px 8px",
              borderRadius: 3,
              letterSpacing: 1,
              background: source === "live" ? C.GREEN : C.ORACLE,
              color: C.VOID,
              textTransform: "uppercase",
              fontWeight: "bold",
            }}
          >
            {source === "live" ? "LIVE" : "MOCK"}
          </span>
        </div>
        <div
          style={{
            fontFamily: FONT_MONO,
            fontSize: 10,
            color: C.MOONSTONE,
          }}
        >
          {lastFetch
            ? `Updated ${lastFetch.toLocaleTimeString()}`
            : ""}
        </div>
      </div>

      {/* Grid */}
      <div
        style={{
          flex: 1,
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gridTemplateRows: "1fr 1fr",
          gap: 12,
          minHeight: 0,
        }}
      >
        <SurvivalPanel data={data.survival || MOCK.survival} />
        <PipelinePanel data={data.pipeline || MOCK.pipeline} />
        <ActionItemsPanel data={data.action_items || MOCK.action_items} />
        <AcosHealthPanel data={data.acos || MOCK.acos} />
      </div>
    </div>
  );
}
