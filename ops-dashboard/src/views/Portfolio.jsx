import { useState, useEffect, useCallback } from "react";
import { Link } from "react-router-dom";
import { getPortfolio } from "../api";
import { C, FONT_MONO, FONT_BODY } from "../theme";
import { relativeTime, shortDate } from "../format";
import HealthStrip from "../components/HealthStrip";
import Badge from "../components/Badge";
import { Loading, ErrorState, EmptyLine } from "../components/States";

// ---------------------------------------------------------------------------
// Portfolio (/) — 30,000-foot strip. One card per engagement. Renders honestly:
// today there is one card (FCA). No placeholder cards, no faked density.
// ---------------------------------------------------------------------------

export default function Portfolio() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await getPortfolio());
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const engagements = (data && data.engagements) || [];
  const unscoped = (data && data.unscoped) || null;
  const pendingTotal = (data && data.pending_total) || 0;

  return (
    <div style={{ maxWidth: 1000, margin: "0 auto", padding: "20px 16px 48px" }}>
      {/* Header */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          flexWrap: "wrap",
          gap: 12,
          marginBottom: 14,
        }}
      >
        <h1
          style={{
            fontFamily: FONT_MONO,
            fontSize: 20,
            letterSpacing: 3,
            textTransform: "uppercase",
            color: C.ARROW,
            fontWeight: "normal",
          }}
        >
          Engagement Ops
        </h1>
        {pendingTotal > 0 && (
          <Badge bg={C.BURNT_ORANGE} color={C.VOID}>
            {pendingTotal} pending
          </Badge>
        )}
      </div>

      {data && data.health && (
        <div style={{ marginBottom: 18 }}>
          <HealthStrip health={data.health} />
        </div>
      )}

      {loading && <Loading label="Loading portfolio…" />}
      {error && <ErrorState error={error} onRetry={load} />}

      {!loading && !error && (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {engagements.length === 0 && (
              <EmptyLine>No engagements.</EmptyLine>
            )}
            {engagements.map((e) => (
              <EngagementCard key={e.slug} e={e} />
            ))}
          </div>

          {unscoped &&
            (unscoped.pending_proposals > 0 || unscoped.open_commitments > 0) &&
            engagements[0] && (
              <UnscopedSummary
                unscoped={unscoped}
                slug={engagements[0].slug}
              />
            )}
        </>
      )}
    </div>
  );
}

function EngagementCard({ e }) {
  const nearHardDate =
    e.days_to_hard_date != null && e.days_to_hard_date <= 14;
  const stale =
    e.last_capture_staleness_days != null && e.last_capture_staleness_days > 3;

  // "DCAA release · Aug 8 · in 20 days"
  let hardDateLine = null;
  if (e.next_hard_date) {
    const parts = [];
    if (e.next_hard_date_label) parts.push(e.next_hard_date_label);
    parts.push(shortDate(e.next_hard_date));
    if (e.days_to_hard_date != null) {
      parts.push(
        e.days_to_hard_date === 0
          ? "today"
          : e.days_to_hard_date < 0
          ? `${Math.abs(e.days_to_hard_date)} days ago`
          : `in ${e.days_to_hard_date} days`
      );
    }
    hardDateLine = parts.join(" · ");
  }

  let captureLine;
  if (e.last_capture == null) {
    captureLine = "no captures";
  } else {
    captureLine = `captured ${relativeTime(e.last_capture)}`;
  }

  return (
    <Link
      to={`/e/${e.slug}`}
      style={{
        textDecoration: "none",
        color: "inherit",
        display: "block",
      }}
    >
      <div
        style={{
          background: C.SHADOW,
          border: `1px solid ${C.MIST}`,
          borderRadius: 4,
          padding: "16px 18px",
          display: "flex",
          flexDirection: "column",
          gap: 10,
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            gap: 12,
            flexWrap: "wrap",
          }}
        >
          <span
            style={{ fontFamily: FONT_BODY, fontSize: 19, color: C.ARROW }}
          >
            {e.display_name}
          </span>
          {e.pending_proposals > 0 && (
            <Badge bg={C.BURNT_ORANGE} color={C.VOID}>
              {e.pending_proposals} pending
            </Badge>
          )}
        </div>

        {hardDateLine && (
          <div
            style={{
              fontFamily: FONT_MONO,
              fontSize: 12,
              color: nearHardDate ? C.BURNT_ORANGE : C.ORACLE,
            }}
          >
            {hardDateLine}
          </div>
        )}

        <div
          style={{
            display: "flex",
            gap: 18,
            flexWrap: "wrap",
            fontFamily: FONT_MONO,
            fontSize: 11,
            color: C.MOONSTONE,
          }}
        >
          <span>
            <span style={{ color: C.ARROW }}>{e.open_commitments}</span> open
            commitments
          </span>
          <span style={{ color: stale ? C.BURNT_ORANGE : C.MOONSTONE }}>
            {captureLine}
          </span>
          {!e.active && <span style={{ opacity: 0.7 }}>inactive</span>}
          {e.archived && <span style={{ opacity: 0.7 }}>archived</span>}
        </div>
      </div>
    </Link>
  );
}

function UnscopedSummary({ unscoped, slug }) {
  return (
    <Link
      to={`/e/${slug}#unscoped`}
      style={{ textDecoration: "none", color: "inherit" }}
    >
      <div
        style={{
          marginTop: 14,
          fontFamily: FONT_MONO,
          fontSize: 11,
          color: C.MOONSTONE,
          display: "flex",
          gap: 14,
          flexWrap: "wrap",
          padding: "8px 12px",
          border: `1px dashed ${C.MIST}`,
          borderRadius: 4,
        }}
      >
        <span style={{ letterSpacing: 1, textTransform: "uppercase" }}>
          Unscoped
        </span>
        <span>
          <span style={{ color: C.ARROW }}>{unscoped.pending_proposals}</span>{" "}
          pending
        </span>
        <span>
          <span style={{ color: C.ARROW }}>{unscoped.open_commitments}</span>{" "}
          open commitments
        </span>
      </div>
    </Link>
  );
}
