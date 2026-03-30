import { useState, useEffect, useCallback } from "react";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const API_BASE =
  "https://inolj7bn99.execute-api.us-east-1.amazonaws.com/default/rdmis-crm-api";
const API_KEY = "kBAuGh_itvJI797R1L3CKuRIbwGXIgwy2beeg1VqVxw";
const REFRESH_MS = 60_000;
const EXIT_DATE = new Date("2026-09-30");

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

const GATE_LABELS = {
  0: "Prospect",
  1: "Pitch",
  2: "Pilot Signed",
  3: "Pilot Complete",
  4: "MSA Signed",
  5: "Implementation",
};

const STATUS_COLOR = {
  hot: C.SIGNAL,
  warm: C.ORACLE,
  active: C.MOONSTONE,
  prospect: C.MIST,
  cold: C.MIST,
  closed: C.GREEN,
  unknown: C.MIST,
};

// ---------------------------------------------------------------------------
// Mock data
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
      contact_name: "Brian Pivar + Srinivasan Narayanan",
      gate: 1,
      stage: "Gate 1",
      value_cents: 12500000,
      status: "warm",
      next_action: "Srinivasan call Thu Apr 3",
      updated_at: "2026-03-30",
    },
    {
      id: "mock-p1",
      company_name: "Stanley Black & Decker",
      contact_name: "",
      gate: 0,
      stage: "Gate 0",
      value_cents: 12500000,
      status: "prospect",
      next_action: "Not contacted",
      updated_at: null,
    },
    {
      id: "mock-p2",
      company_name: "Spectrum Brands",
      contact_name: "",
      gate: 0,
      stage: "Gate 0",
      value_cents: 12500000,
      status: "prospect",
      next_action: "Not contacted",
      updated_at: null,
    },
    {
      id: "mock-p3",
      company_name: "ITW",
      contact_name: "",
      gate: 0,
      stage: "Gate 0",
      value_cents: 12500000,
      status: "prospect",
      next_action: "Not contacted",
      updated_at: null,
    },
    {
      id: "mock-p4",
      company_name: "Emerson",
      contact_name: "",
      gate: 0,
      stage: "Gate 0",
      value_cents: 12500000,
      status: "prospect",
      next_action: "Not contacted",
      updated_at: null,
    },
    {
      id: "mock-p5",
      company_name: "Roper Technologies",
      contact_name: "",
      gate: 0,
      stage: "Gate 0",
      value_cents: 12500000,
      status: "prospect",
      next_action: "Not contacted",
      updated_at: null,
    },
    {
      id: "mock-p6",
      company_name: "Kawasaki",
      contact_name: "",
      gate: 0,
      stage: "Gate 0",
      value_cents: 12500000,
      status: "prospect",
      next_action: "Not contacted",
      updated_at: null,
    },
  ],
  action_items: [
    {
      id: "ai-1",
      priority: "high",
      title: "Send Lucint briefing to Brad + Srinivasan",
      due_at: "2026-04-01T09:00:00",
      gate_label: "Gate 1",
    },
    {
      id: "ai-2",
      priority: "high",
      title: "Follow up Brian Pivar LinkedIn",
      due_at: "2026-04-02T09:00:00",
      gate_label: "Gate 1",
    },
    {
      id: "ai-3",
      priority: "high",
      title: "SCORE call — Brad + Srinivasan",
      due_at: "2026-04-03T09:00:00",
      gate_label: "Gate 1",
    },
    {
      id: "ai-4",
      priority: "normal",
      title: "Activate Databricks credits",
      due_at: "2026-04-01T09:00:00",
      gate_label: "Gate 0",
    },
    {
      id: "ai-5",
      priority: "normal",
      title: "Merge PR #33 aws-build \u2192 main",
      due_at: new Date().toISOString(),
      gate_label: "Gate 0",
    },
    {
      id: "ai-6",
      priority: "normal",
      title: "Attorney engagement for NDA template",
      due_at: null,
      gate_label: null,
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
  if (!iso) return "\u2014";
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

function daysToExit() {
  return Math.ceil((EXIT_DATE - new Date()) / 86_400_000);
}

/** Extract a gate number from a deal or action item. */
function parseGate(item) {
  // Prefer explicit gate field
  if (item.gate != null) return item.gate;
  // Try stage string like "Gate 2"
  const m = (item.stage || item.gate_label || "").match(/gate\s*(\d)/i);
  return m ? parseInt(m[1], 10) : null;
}

/** Group action items by gate label. */
function groupByGate(items) {
  const groups = {};
  for (const item of items) {
    const g = parseGate(item);
    const label =
      g != null ? `Gate ${g} \u2014 ${GATE_LABELS[g] || "Unknown"}` : "General";
    if (!groups[label]) groups[label] = [];
    groups[label].push(item);
  }
  // Sort groups: numbered gates first (ascending), then General last
  const sorted = Object.entries(groups).sort((a, b) => {
    const gA = a[0].match(/Gate (\d)/);
    const gB = b[0].match(/Gate (\d)/);
    if (gA && gB) return parseInt(gA[1]) - parseInt(gB[1]);
    if (gA) return -1;
    if (gB) return 1;
    return 0;
  });
  return sorted;
}

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
        padding: "14px 18px",
        display: "flex",
        flexDirection: "column",
        gap: 10,
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
          paddingBottom: 6,
          flexShrink: 0,
        }}
      >
        {title}
      </div>
      <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>{children}</div>
    </div>
  );
}

function ProgressBar({ value, max, color }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  return (
    <div
      style={{
        height: 5,
        background: C.MIST,
        borderRadius: 3,
        overflow: "hidden",
        marginTop: 3,
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

function GateBadge({ gate }) {
  const isProspect = gate === 0;
  return (
    <span
      style={{
        fontFamily: FONT_MONO,
        fontSize: 9,
        letterSpacing: 1,
        textTransform: "uppercase",
        padding: "2px 7px",
        borderRadius: 3,
        background: isProspect ? C.MIST : C.EMBER,
        color: C.ARROW,
        whiteSpace: "nowrap",
      }}
    >
      Gate {gate}
    </span>
  );
}

function StatusDot({ status }) {
  return (
    <span
      style={{
        display: "inline-block",
        width: 7,
        height: 7,
        borderRadius: "50%",
        background: STATUS_COLOR[status] || C.MIST,
        boxShadow: `0 0 5px ${STATUS_COLOR[status] || C.MIST}`,
        flexShrink: 0,
      }}
    />
  );
}

function Stat({ label, value, mono, color, small }) {
  return (
    <div style={{ marginBottom: small ? 4 : 6 }}>
      <div
        style={{
          fontFamily: FONT_MONO,
          fontSize: 9,
          color: C.MOONSTONE,
          letterSpacing: 1,
          textTransform: "uppercase",
          marginBottom: 1,
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontFamily: mono ? FONT_MONO : FONT_BODY,
          fontSize: small ? 12 : 14,
          color: color || C.ARROW,
        }}
      >
        {value}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Panel: Pipeline (gate-sorted deal cards)
// ---------------------------------------------------------------------------

function PipelinePanel({ data }) {
  const sorted = [...data].sort((a, b) => {
    const gA = parseGate(a) ?? 99;
    const gB = parseGate(b) ?? 99;
    if (gA !== gB) return gA - gB;
    return 0;
  });

  return (
    <Panel title="Pipeline" style={{ flex: 1, minHeight: 0 }}>
      {sorted.length === 0 && (
        <div style={{ color: C.MOONSTONE, fontStyle: "italic" }}>
          No deals
        </div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {sorted.map((deal) => {
          const gate = parseGate(deal) ?? 0;
          const isProspect = gate === 0;
          return (
            <div
              key={deal.id}
              style={{
                background: C.VOID,
                border: `1px solid ${C.MIST}`,
                borderRadius: 4,
                padding: "9px 12px",
                opacity: isProspect ? 0.5 : 1,
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: 3,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <StatusDot status={deal.status} />
                  <span style={{ fontWeight: "bold", fontSize: 14 }}>
                    {deal.company_name}
                  </span>
                </div>
                <GateBadge gate={gate} />
              </div>

              {deal.contact_name && (
                <div
                  style={{
                    fontSize: 12,
                    color: C.MOONSTONE,
                    marginBottom: 3,
                    paddingLeft: 15,
                  }}
                >
                  {deal.contact_name}
                </div>
              )}

              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  paddingLeft: 15,
                }}
              >
                <span
                  style={{
                    fontFamily: FONT_MONO,
                    fontSize: 12,
                    color: C.ORACLE,
                  }}
                >
                  {cents(deal.value_cents)}
                </span>
                {deal.next_action && (
                  <span
                    style={{
                      fontSize: 11,
                      color: C.ARROW,
                      opacity: 0.6,
                      fontStyle: "italic",
                      textAlign: "right",
                      maxWidth: "60%",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {deal.next_action}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Panel: Survival (compact)
// ---------------------------------------------------------------------------

function SurvivalPanel({ data }) {
  const mrr = data.mrr_cents || 0;
  const target = data.mrr_target_cents || 4500000;
  const exp = data.expenses_month || {};

  return (
    <Panel title="Survival">
      <div style={{ marginBottom: 4 }}>
        <div
          style={{
            fontFamily: FONT_MONO,
            fontSize: 9,
            color: C.MOONSTONE,
            letterSpacing: 1,
            textTransform: "uppercase",
          }}
        >
          MRR
        </div>
        <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
          <span
            style={{
              fontFamily: FONT_MONO,
              fontSize: 24,
              fontWeight: "bold",
              color: mrr > 0 ? C.GREEN : C.SIGNAL,
            }}
          >
            {cents(mrr)}
          </span>
          <span
            style={{ fontFamily: FONT_MONO, fontSize: 11, color: C.MOONSTONE }}
          >
            / {cents(target)}
          </span>
        </div>
        <ProgressBar value={mrr} max={target} color={C.ORACLE} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4 }}>
        <Stat
          label="Runway"
          value={
            data.runway_months != null
              ? `${data.runway_months}mo`
              : "Pre-Revenue"
          }
          color={data.pre_revenue ? C.ORACLE : C.GREEN}
          small
        />
        <Stat
          label="Founder Loan"
          value={cents(data.founder_loan_balance_cents)}
          mono
          small
        />
        <Stat
          label={`Expenses ${exp.month || ""}`}
          value={cents(exp.total_cents)}
          mono
          small
        />
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Panel: Action Items (grouped by gate)
// ---------------------------------------------------------------------------

function ActionItemsPanel({ data }) {
  const groups = groupByGate(data);

  return (
    <Panel title="Action Items" style={{ flex: 1, minHeight: 0 }}>
      {data.length === 0 && (
        <div
          style={{
            color: C.GREEN,
            fontStyle: "italic",
            padding: "8px 0",
          }}
        >
          All clear
        </div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {groups.map(([label, items]) => (
          <div key={label}>
            <div
              style={{
                fontFamily: FONT_MONO,
                fontSize: 10,
                letterSpacing: 1,
                textTransform: "uppercase",
                color: C.ORACLE,
                marginBottom: 6,
              }}
            >
              {label}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {items.map((item) => (
                <div
                  key={item.id}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "5px 10px",
                    background: C.VOID,
                    border: `1px solid ${C.MIST}`,
                    borderRadius: 4,
                    borderLeft: `3px solid ${
                      item.priority === "high" ? C.SIGNAL : C.MIST
                    }`,
                  }}
                >
                  <div
                    style={{
                      width: 12,
                      height: 12,
                      border: `1.5px solid ${C.MOONSTONE}`,
                      borderRadius: 2,
                      flexShrink: 0,
                    }}
                  />
                  <div style={{ flex: 1, fontSize: 13 }}>{item.title}</div>
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
          </div>
        ))}
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Panel: ACOS Health (compact)
// ---------------------------------------------------------------------------

function AcosHealthPanel({ data }) {
  const isOnline = data.status === "online";
  return (
    <Panel title="ACOS Health">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 6,
        }}
      >
        <div
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: isOnline ? C.GREEN : C.SIGNAL,
            boxShadow: isOnline
              ? `0 0 6px ${C.GREEN}`
              : `0 0 6px ${C.SIGNAL}`,
          }}
        />
        <span
          style={{
            fontFamily: FONT_MONO,
            fontSize: 12,
            color: isOnline ? C.GREEN : C.SIGNAL,
            textTransform: "uppercase",
            letterSpacing: 2,
          }}
        >
          {data.status || "unknown"}
        </span>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4 }}>
        <Stat label="Version" value={data.version || "\u2014"} mono small />
        <Stat label="Jobs" value={data.jobs_running ?? "\u2014"} mono small />
        <Stat
          label="Last Brief"
          value={relativeTime(data.last_brief)}
          mono
          small
        />
        <Stat
          label="Uptime"
          value={data.uptime_pct != null ? `${data.uptime_pct}%` : "\u2014"}
          mono
          color={C.GREEN}
          small
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
  const [source, setSource] = useState(null);
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

  const days = daysToExit();

  return (
    <div
      style={{
        height: "100vh",
        display: "flex",
        flexDirection: "column",
        background: C.VOID,
        padding: 14,
        gap: 10,
      }}
    >
      {/* ── Header ── */}
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
            fontSize: 13,
            letterSpacing: 1,
            color: days <= 90 ? C.SIGNAL : C.ORACLE,
          }}
        >
          {days > 0 ? `${days} days to exit` : "EXIT DAY"}
        </div>

        <div
          style={{ fontFamily: FONT_MONO, fontSize: 10, color: C.MOONSTONE }}
        >
          {lastFetch ? `Updated ${lastFetch.toLocaleTimeString()}` : ""}
        </div>
      </div>

      {/* ── Body: left column + right column ── */}
      <div
        style={{
          flex: 1,
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 10,
          minHeight: 0,
        }}
      >
        {/* Left column: Pipeline + Survival */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 10,
            minHeight: 0,
          }}
        >
          <PipelinePanel data={data.pipeline || MOCK.pipeline} />
          <SurvivalPanel data={data.survival || MOCK.survival} />
        </div>

        {/* Right column: Action Items + ACOS Health */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 10,
            minHeight: 0,
          }}
        >
          <ActionItemsPanel
            data={data.action_items || MOCK.action_items}
          />
          <AcosHealthPanel data={data.acos || MOCK.acos} />
        </div>
      </div>
    </div>
  );
}
