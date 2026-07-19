import { useState, useCallback } from "react";
import { searchDossier, getPerson, getOrg } from "../api";
import { C, FONT_MONO, FONT_BODY } from "../theme";
import { orDash, shortDate } from "../format";
import { Loading, ErrorState, EmptyLine } from "./States";

// ---------------------------------------------------------------------------
// DossierSearch — read-only lookup. Search box → person/org cards. Clicking a
// person or org opens a detail drawer. Single column, large tap targets: this
// is used on a phone in a hallway.
// ---------------------------------------------------------------------------

export default function DossierSearch() {
  const [q, setQ] = useState("");
  const [results, setResults] = useState(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState(null);
  const [detail, setDetail] = useState(null); // {kind:'person'|'org', data}
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState(null);

  const runSearch = useCallback(async (e) => {
    if (e) e.preventDefault();
    const term = q.trim();
    if (!term) return;
    setSearching(true);
    setError(null);
    try {
      setResults(await searchDossier(term));
    } catch (err) {
      setError(err);
      setResults(null);
    } finally {
      setSearching(false);
    }
  }, [q]);

  const openPerson = useCallback(async (slug) => {
    setDetail(null);
    setDetailLoading(true);
    setDetailError(null);
    try {
      setDetail({ kind: "person", data: await getPerson(slug) });
    } catch (err) {
      setDetailError(err);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const openOrg = useCallback(async (org) => {
    setDetail(null);
    setDetailLoading(true);
    setDetailError(null);
    try {
      setDetail({ kind: "org", data: await getOrg(org) });
    } catch (err) {
      setDetailError(err);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const people = (results && results.people) || [];
  const orgs = (results && results.orgs) || [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <form onSubmit={runSearch} style={{ display: "flex", gap: 8 }}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search people / orgs…"
          style={{
            flex: 1,
            fontFamily: FONT_BODY,
            fontSize: 16,
            background: C.VOID,
            border: `1px solid ${C.MIST}`,
            borderRadius: 4,
            color: C.ARROW,
            padding: "10px 12px",
            minHeight: 44,
            boxSizing: "border-box",
          }}
        />
        <button
          type="submit"
          style={{
            fontFamily: FONT_MONO,
            fontSize: 12,
            letterSpacing: 1,
            textTransform: "uppercase",
            padding: "0 16px",
            borderRadius: 4,
            border: `1px solid ${C.MOONSTONE}`,
            background: "transparent",
            color: C.ARROW,
            cursor: "pointer",
            minHeight: 44,
          }}
        >
          Search
        </button>
      </form>

      {searching && <Loading label="Searching…" />}
      {error && <ErrorState error={error} onRetry={runSearch} />}

      {results && !searching && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {people.length === 0 && orgs.length === 0 && (
            <EmptyLine>No matches.</EmptyLine>
          )}
          {people.map((p) => (
            <ResultCard
              key={`p-${p.slug}`}
              title={p.full_name}
              sub={[p.title, p.org].filter(Boolean).join(" · ")}
              kind="person"
              onClick={() => openPerson(p.slug)}
            />
          ))}
          {orgs.map((o) => (
            <ResultCard
              key={`o-${o.org}`}
              title={o.display_name || o.org}
              sub="organization"
              kind="org"
              onClick={() => openOrg(o.org)}
            />
          ))}
        </div>
      )}

      {(detailLoading || detailError || detail) && (
        <div
          style={{
            marginTop: 4,
            background: C.VOID,
            border: `1px solid ${C.MIST}`,
            borderRadius: 4,
            padding: "14px 16px",
          }}
        >
          {detailLoading && <Loading label="Loading dossier…" />}
          {detailError && <ErrorState error={detailError} />}
          {detail && detail.kind === "person" && <PersonDetail p={detail.data} />}
          {detail && detail.kind === "org" && <OrgDetail o={detail.data} onPerson={openPerson} />}
          {detail && (
            <button
              onClick={() => setDetail(null)}
              style={{
                marginTop: 12,
                fontFamily: FONT_MONO,
                fontSize: 11,
                letterSpacing: 1,
                textTransform: "uppercase",
                padding: "8px 14px",
                borderRadius: 4,
                border: `1px solid ${C.MIST}`,
                background: "transparent",
                color: C.MOONSTONE,
                cursor: "pointer",
                minHeight: 40,
              }}
            >
              Close
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function ResultCard({ title, sub, kind, onClick }) {
  return (
    <button
      onClick={onClick}
      style={{
        textAlign: "left",
        background: C.VOID,
        border: `1px solid ${C.MIST}`,
        borderRadius: 4,
        padding: "12px 14px",
        cursor: "pointer",
        display: "flex",
        flexDirection: "column",
        gap: 3,
        minHeight: 52,
        width: "100%",
      }}
    >
      <span style={{ fontFamily: FONT_BODY, fontSize: 16, color: C.ARROW }}>
        {title}
      </span>
      <span style={{ fontFamily: FONT_MONO, fontSize: 11, color: C.MOONSTONE }}>
        {sub || kind}
      </span>
    </button>
  );
}

function DetailField({ label, value }) {
  return (
    <div>
      <div
        style={{
          fontFamily: FONT_MONO,
          fontSize: 9,
          letterSpacing: 1,
          textTransform: "uppercase",
          color: C.MOONSTONE,
        }}
      >
        {label}
      </div>
      <div style={{ fontFamily: FONT_BODY, fontSize: 14, color: C.ARROW }}>
        {orDash(value)}
      </div>
    </div>
  );
}

function PersonDetail({ p }) {
  const entries = p.entries || [];
  const commitments = p.commitments || [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ fontFamily: FONT_BODY, fontSize: 19, color: C.ARROW }}>
        {p.full_name}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
        <DetailField label="title" value={p.title} />
        <DetailField label="org" value={p.org} />
        <DetailField label="reports to" value={p.reports_to} />
      </div>
      {p.position_terrain && <DetailField label="position / terrain" value={p.position_terrain} />}
      {p.needs_from_me && <DetailField label="needs from me" value={p.needs_from_me} />}

      {entries.length > 0 && (
        <div>
          <SubHead>Recent entries</SubHead>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {entries.map((en, i) => (
              <div key={i} style={{ fontFamily: FONT_BODY, fontSize: 13, color: C.ARROW }}>
                <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: C.MOONSTONE }}>
                  {shortDate(en.entry_date)}{en.status ? ` · ${en.status}` : ""} —{" "}
                </span>
                {en.text}
              </div>
            ))}
          </div>
        </div>
      )}

      {commitments.length > 0 && (
        <div>
          <SubHead>Commitments</SubHead>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {commitments.map((cm) => (
              <div key={cm.id} style={{ fontFamily: FONT_BODY, fontSize: 13, color: C.ARROW }}>
                <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: C.MOONSTONE }}>
                  #{cm.id} {cm.status ? `${cm.status} ` : ""}{cm.due_date ? shortDate(cm.due_date) : ""} —{" "}
                </span>
                {cm.title}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function OrgDetail({ o, onPerson }) {
  const notes = o.notes || [];
  const people = o.people || [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ fontFamily: FONT_BODY, fontSize: 19, color: C.ARROW }}>
        {o.display_name || o.org}
      </div>
      {o.exists === false && (
        <EmptyLine>No dossier on file for this org yet.</EmptyLine>
      )}
      {o.overview && <DetailField label="overview" value={o.overview} />}
      {o.active_work && <DetailField label="active work" value={o.active_work} />}
      {o.opportunities && <DetailField label="opportunities" value={o.opportunities} />}

      {people.length > 0 && (
        <div>
          <SubHead>People</SubHead>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {people.map((pp) => (
              <button
                key={pp.slug}
                onClick={() => onPerson(pp.slug)}
                style={{
                  textAlign: "left",
                  background: "transparent",
                  border: "none",
                  cursor: "pointer",
                  fontFamily: FONT_BODY,
                  fontSize: 14,
                  color: C.MOONSTONE,
                  padding: "6px 0",
                  minHeight: 36,
                }}
              >
                <span style={{ color: C.ARROW }}>{pp.full_name}</span>
                {pp.title ? ` · ${pp.title}` : ""}
                {pp.reports_to ? ` · reports to ${pp.reports_to}` : ""}
              </button>
            ))}
          </div>
        </div>
      )}

      {notes.length > 0 && (
        <div>
          <SubHead>Notes</SubHead>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {notes.map((n, i) => (
              <div key={i} style={{ fontFamily: FONT_BODY, fontSize: 13, color: C.ARROW }}>
                <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: C.MOONSTONE }}>
                  {shortDate(n.note_date)} —{" "}
                </span>
                {n.text}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function SubHead({ children }) {
  return (
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
      {children}
    </div>
  );
}
