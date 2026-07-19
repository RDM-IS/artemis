import { useState, useEffect, useCallback } from "react";
import { useParams, Link } from "react-router-dom";
import {
  getEngagement,
  approveProposal,
  rejectProposal,
  batchProposals,
  approveDossierDraft,
  rejectDossierDraft,
  closeCommitment,
} from "../api";
import { C, FONT_MONO, FONT_BODY } from "../theme";
import { shortDate } from "../format";
import Panel from "../components/Panel";
import HealthStrip from "../components/HealthStrip";
import Badge from "../components/Badge";
import ProposalCard from "../components/ProposalCard";
import DossierDraftCard from "../components/DossierDraftCard";
import CommitmentRow from "../components/CommitmentRow";
import DossierSearch from "../components/DossierSearch";
import { Loading, ErrorStrip, EmptyLine } from "../components/States";

// ---------------------------------------------------------------------------
// Engagement (/e/:slug) — the working surface. Approval queue is the
// centerpiece: widest, top. After any mutation we re-fetch the engagement so
// results come from the server (optimistic removal is reconciled on refetch).
// ---------------------------------------------------------------------------

export default function Engagement() {
  const { slug } = useParams();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  // ids currently mid-mutation (disabled + dimmed), and batch selection.
  const [busyIds, setBusyIds] = useState(() => new Set());
  const [selected, setSelected] = useState(() => new Set());
  // D2: per-id mutation failures, keyed by the same composite ids as busyIds
  // (proposal id, `c${id}`, `d${type}-${id}`). A failed mutation records its
  // ApiError here and renders inline near the card — it is NOT silently reverted.
  const [mutErrors, setMutErrors] = useState(() => new Map());

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await getEngagement(slug));
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [slug]);

  useEffect(() => {
    // Reset transient state when the slug changes.
    setSelected(new Set());
    setBusyIds(new Set());
    setMutErrors(new Map());
    load();
  }, [load]);

  // --- mutation plumbing --------------------------------------------------

  const withBusy = useCallback(async (ids, fn) => {
    setBusyIds((prev) => {
      const next = new Set(prev);
      ids.forEach((i) => next.add(i));
      return next;
    });
    // Clear any prior error for these ids — this attempt supersedes it.
    setMutErrors((prev) => {
      const next = new Map(prev);
      ids.forEach((i) => next.delete(i));
      return next;
    });
    let ok = false;
    try {
      await fn();
      ok = true;
    } catch (err) {
      // D2: do NOT silently restore. Record the failure inline against each
      // affected id so the operator sees the action failed, and skip the
      // reconciling refetch (which would make it look like nothing happened).
      setMutErrors((prev) => {
        const next = new Map(prev);
        ids.forEach((i) => next.set(i, err));
        return next;
      });
    } finally {
      if (ok) {
        await load();
        setSelected((prev) => {
          const next = new Set(prev);
          ids.forEach((i) => next.delete(i));
          return next;
        });
      }
      setBusyIds((prev) => {
        const next = new Set(prev);
        ids.forEach((i) => next.delete(i));
        return next;
      });
    }
  }, [load]);

  const onApprove = useCallback(
    (id, payloadFinal) => withBusy([id], () => approveProposal(id, payloadFinal)),
    [withBusy]
  );
  const onReject = useCallback(
    (id) => withBusy([id], () => rejectProposal(id)),
    [withBusy]
  );
  const onCloseCommitment = useCallback(
    (id) => withBusy([`c${id}`], () => closeCommitment(id)),
    [withBusy]
  );
  const onApproveDraft = useCallback(
    (type, id) => withBusy([`d${type}-${id}`], () => approveDossierDraft(type, id)),
    [withBusy]
  );
  const onRejectDraft = useCallback(
    (type, id) => withBusy([`d${type}-${id}`], () => rejectDossierDraft(type, id)),
    [withBusy]
  );

  const toggleSelect = useCallback((id) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }, []);

  const runBatch = useCallback(
    (action) => {
      const ids = [...selected];
      if (ids.length === 0) return;
      withBusy(ids, () => batchProposals(action, ids));
    },
    [selected, withBusy]
  );

  // --- render -------------------------------------------------------------

  if (loading && !data) {
    return (
      <Shell>
        <BackLink />
        <Loading label="Loading engagement…" />
      </Shell>
    );
  }

  if (error && !data) {
    const notFound = error.status === 404;
    return (
      <Shell>
        <BackLink />
        {notFound ? (
          <EmptyLine>Engagement “{slug}” not found.</EmptyLine>
        ) : (
          <ErrorStrip error={error} onRetry={load} />
        )}
      </Shell>
    );
  }

  const eng = (data && data.engagement) || {};
  const queue = (data && data.approval_queue) || {};
  const scoped = queue.scoped || [];
  const unscoped = queue.unscoped || [];
  const drafts = queue.dossier_drafts || [];
  const commitments = (data && data.commitments) || [];
  const dossiers = (data && data.dossiers) || [];
  const projects = (data && data.projects) || [];

  const hardDate = hardDateLine(eng);

  return (
    <Shell>
      {/* Header */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12, marginBottom: 18 }}>
        <BackLink />
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            gap: 12,
            flexWrap: "wrap",
          }}
        >
          <h1
            style={{
              fontFamily: FONT_BODY,
              fontSize: 26,
              color: C.ARROW,
              fontWeight: "normal",
            }}
          >
            {eng.display_name || slug}
          </h1>
          {queue.pending_count > 0 && (
            <Badge bg={C.BURNT_ORANGE} color={C.VOID}>
              {queue.pending_count} pending
            </Badge>
          )}
        </div>
        {hardDate && (
          <div
            style={{
              fontFamily: FONT_MONO,
              fontSize: 13,
              color: hardDate.near ? C.BURNT_ORANGE : C.ORACLE,
            }}
          >
            {hardDate.text}
          </div>
        )}
        {data && data.health && <HealthStrip health={data.health} />}
      </div>

      {/* Non-fatal read error (a refetch failed but we still have prior data).
          Mutation failures render inline on their card, not here. */}
      {error && data && (
        <div style={{ marginBottom: 14 }}>
          <ErrorStrip error={error} onRetry={load} />
        </div>
      )}

      {/* 1. Approval queue — centerpiece */}
      <Panel
        title="Approval queue"
        accent={C.SIGNAL}
        subtitle={`${queue.pending_count || 0} pending`}
        style={{ marginBottom: 16 }}
      >
        <Group label={`Scoped · ${eng.display_name || slug}`}>
          {scoped.length === 0 ? (
            <EmptyLine>No scoped proposals.</EmptyLine>
          ) : (
            scoped.map((p) => (
              <ProposalCard
                key={p.id}
                proposal={p}
                busy={busyIds.has(p.id)}
                selected={selected.has(p.id)}
                error={mutErrors.get(p.id)}
                onToggleSelect={toggleSelect}
                onApprove={onApprove}
                onReject={onReject}
              />
            ))
          )}
        </Group>

        <Group label="Unscoped" note="not tied to this engagement">
          {unscoped.length === 0 ? (
            <EmptyLine>No unscoped proposals.</EmptyLine>
          ) : (
            unscoped.map((p) => (
              <ProposalCard
                key={p.id}
                proposal={p}
                busy={busyIds.has(p.id)}
                selected={selected.has(p.id)}
                error={mutErrors.get(p.id)}
                onToggleSelect={toggleSelect}
                onApprove={onApprove}
                onReject={onReject}
              />
            ))
          )}
        </Group>

        <Group label="Dossier drafts" note="approve / reject only — no edit">
          {drafts.length === 0 ? (
            <EmptyLine>No dossier drafts.</EmptyLine>
          ) : (
            drafts.map((d) => (
              <DossierDraftCard
                key={`${d.draft_type}-${d.id}`}
                draft={d}
                busy={busyIds.has(`d${d.draft_type}-${d.id}`)}
                error={mutErrors.get(`d${d.draft_type}-${d.id}`)}
                onApprove={onApproveDraft}
                onReject={onRejectDraft}
              />
            ))
          )}
        </Group>
      </Panel>

      {/* 2. Commitments */}
      <Panel title="Commitments / action items" style={{ marginBottom: 16 }}>
        {commitments.length === 0 ? (
          <EmptyLine>No open commitments.</EmptyLine>
        ) : (
          commitments.map((cm) => (
            <CommitmentRow
              key={cm.id}
              commitment={cm}
              busy={busyIds.has(`c${cm.id}`)}
              error={mutErrors.get(`c${cm.id}`)}
              onClose={onCloseCommitment}
            />
          ))
        )}
      </Panel>

      {/* 3. Dossiers */}
      <Panel title="Dossiers" subtitle="read-only lookup" style={{ marginBottom: 16 }}>
        <DossierSearch />
        {dossiers.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 4 }}>
            <div
              style={{
                fontFamily: FONT_MONO,
                fontSize: 10,
                letterSpacing: 1,
                textTransform: "uppercase",
                color: C.MOONSTONE,
              }}
            >
              On this engagement
            </div>
            {dossiers.map((pc) => (
              <div key={pc.dossier_id} style={{ fontFamily: FONT_BODY, fontSize: 14, color: C.ARROW }}>
                <span style={{ color: C.ARROW }}>{pc.full_name}</span>
                {pc.title ? ` · ${pc.title}` : ""}
                {pc.org ? ` · ${pc.org}` : ""}
                {!pc.active && (
                  <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: C.MOONSTONE }}> (inactive)</span>
                )}
              </div>
            ))}
          </div>
        )}
      </Panel>

      {/* 4. Projects — thin */}
      <Panel title="Projects" style={{ marginBottom: 16 }}>
        {projects.length === 0 ? (
          <EmptyLine>No projects.</EmptyLine>
        ) : (
          projects.map((pr) => (
            <div key={pr.capture_id} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <div style={{ fontFamily: FONT_BODY, fontSize: 15, color: C.ARROW }}>
                {pr.title}
              </div>
              {(pr.linked_commitments || []).length > 0 && (
                <div style={{ paddingLeft: 12, display: "flex", flexDirection: "column", gap: 2 }}>
                  {pr.linked_commitments.map((lc) => (
                    <div key={lc.id} style={{ fontFamily: FONT_MONO, fontSize: 11, color: C.MOONSTONE }}>
                      #{lc.id} {lc.title}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))
        )}
      </Panel>

      {/* Sticky batch action bar */}
      {selected.size > 0 && (
        <div
          style={{
            position: "sticky",
            bottom: 0,
            marginTop: 8,
            display: "flex",
            gap: 10,
            alignItems: "center",
            flexWrap: "wrap",
            background: C.SHADOW,
            border: `1px solid ${C.MIST}`,
            borderRadius: 6,
            padding: "12px 16px",
            boxShadow: "0 -4px 16px rgba(0,0,0,0.5)",
          }}
        >
          <span style={{ fontFamily: FONT_MONO, fontSize: 12, color: C.MOONSTONE }}>
            {selected.size} selected
          </span>
          <div style={{ flex: 1 }} />
          <button onClick={() => runBatch("approve")} style={batchBtn(C.AVOCADO)}>
            Approve selected ({selected.size})
          </button>
          <button onClick={() => runBatch("reject")} style={batchBtn(C.SIGNAL)}>
            Reject selected ({selected.size})
          </button>
          <button onClick={() => setSelected(new Set())} style={batchBtn(C.MIST)}>
            Clear
          </button>
        </div>
      )}
    </Shell>
  );
}

// ---------------------------------------------------------------------------

function Shell({ children }) {
  return (
    <div style={{ maxWidth: 1040, margin: "0 auto", padding: "20px 16px 64px" }}>
      {children}
    </div>
  );
}

function BackLink() {
  return (
    <Link
      to="/"
      style={{
        fontFamily: FONT_MONO,
        fontSize: 11,
        letterSpacing: 1,
        textTransform: "uppercase",
        color: C.MOONSTONE,
        textDecoration: "none",
      }}
    >
      ← Portfolio
    </Link>
  );
}

function Group({ label, note, children }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
        <span
          style={{
            fontFamily: FONT_MONO,
            fontSize: 11,
            letterSpacing: 1.5,
            textTransform: "uppercase",
            color: C.ORACLE,
          }}
        >
          {label}
        </span>
        {note && (
          <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: C.MOONSTONE, opacity: 0.8 }}>
            {note}
          </span>
        )}
      </div>
      {children}
    </div>
  );
}

function hardDateLine(eng) {
  if (!eng || !eng.next_hard_date) return null;
  const near = eng.days_to_hard_date != null && eng.days_to_hard_date <= 14;
  const label = eng.next_hard_date_label || "hard date";
  let tail = "";
  if (eng.days_to_hard_date != null) {
    tail =
      eng.days_to_hard_date === 0
        ? "today"
        : eng.days_to_hard_date < 0
        ? `${Math.abs(eng.days_to_hard_date)} days ago`
        : `in ${eng.days_to_hard_date} days`;
  }
  const text = `${label}: ${shortDate(eng.next_hard_date)}${tail ? ` (${tail})` : ""}`;
  return { text, near };
}

function batchBtn(border) {
  return {
    fontFamily: FONT_MONO,
    fontSize: 12,
    letterSpacing: 1,
    textTransform: "uppercase",
    padding: "9px 16px",
    borderRadius: 4,
    border: `1px solid ${border}`,
    background: "transparent",
    color: border === C.MIST ? C.MOONSTONE : border,
    cursor: "pointer",
    minHeight: 40,
  };
}
