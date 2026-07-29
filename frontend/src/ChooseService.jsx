/**
 * ChooseService — run ranking, then the social worker picks which service a referral
 * goes to.
 *
 * This is the human gate in the pipeline (`003_sw_selection_gate.sql`). Ranking produces
 * a shortlist; `advance_referral` then parks the referral at `awaiting_sw_selection` and
 * queues a `select_resource` action to the `social_worker` component, which nothing polls
 * — because a person is the poller. This screen is that person.
 *
 * WHY A HUMAN GATE AND NOT A RANK-ORDERING. Two reasons, and only the first is obvious.
 * A ranker that has never met the patient shouldn't silently commit them to a provider.
 * But the second matters more long-term: the SW's choice, and the label they attach to
 * it, is the ONLY training signal the subjective layer ever gets (`sw_feedback` →ranking
 * Layer 3). Auto-selecting rank 1 doesn't just remove a safeguard, it starves the
 * feedback loop that would make ranking better over time.
 *
 * So the label is required, not optional, and "accept the top pick" is still a choice —
 * a fast one, but recorded as a decision. The SW can also override the ranked pick with
 * any service in the catalog, which rides along on the same submit
 * (POST /api/referrals/{id}/choose-service). No contact-method override — patient
 * outreach always goes out over SMS.
 */

import { useEffect, useState } from "react";
import { api } from "./api.js";
import { C, Btn } from "./ui.jsx";

// backend/service_ranking's sw_feedback.label enum. Free-text notes ride alongside.
const LABELS = [
  ["good_fit", "Good fit"],
  ["wrong_service", "Wrong service"],
  ["too_far", "Too far"],
  ["insurance_mismatch", "Insurance mismatch"],
  ["other", "Other"],
];

const pct = (n) => (n == null ? "—" : `${Math.round(n)}`);

export default function ChooseService({ referralId, onBack, onChosen }) {
  const [results, setResults] = useState(null);   // ranked shortlist, null = loading
  const [services, setServices] = useState(null); // full catalog, for the override picker
  const [error, setError] = useState(null);
  const [picked, setPicked] = useState(null);           // the ranked dropdown's own pick
  const [overrideServiceId, setOverrideServiceId] = useState("");
  const [label, setLabel] = useState("good_fit");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [rankBusy, setRankBusy] = useState(false);
  const [rankError, setRankError] = useState(null);

  const loadRanking = () =>
    api.ranking(referralId).then((d) => {
      const rows = d.results || [];
      setResults(rows);
      // Keep the current pick if it's still in the (re-ranked) list; otherwise fall
      // back to the new #1 — the default selection the SW sees.
      if (rows.length) {
        setPicked((p) => (p && rows.some((r) => r.service_id === p) ? p : rows[0].service_id));
      }
      return rows;
    });

  useEffect(() => {
    Promise.all([loadRanking(), api.services().then((d) => setServices(d.services || []))])
      .catch((e) => setError(String(e)));
    // referralId is the only thing this should re-run for.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [referralId]);

  const runRanking = async () => {
    setRankBusy(true);
    setRankError(null);
    try {
      await api.rankReferral(referralId);
      await loadRanking();
    } catch (e) {
      setRankError(String(e));
    } finally {
      setRankBusy(false);
    }
  };

  // The override wins when set; otherwise it's whatever the ranked dropdown has picked.
  const effectiveServiceId = overrideServiceId || picked;
  const selectedResult = (results || []).find((r) => r.service_id === effectiveServiceId);

  const confirm = async () => {
    setBusy(true);
    try {
      await api.chooseService(referralId, {
        service_id: effectiveServiceId,
        label,
        label_notes: notes || null,
      });
      onChosen?.(referralId);
    } catch (e) {
      alert(`Could not record the choice: ${e}`);
      setBusy(false);
    }
  };

  if (error)
    return (
      <div style={s.wrap}>
        <Btn tone="ghost" onClick={onBack}>← Back</Btn>
        <div style={s.note}>
          Couldn’t load this screen: {error}
          <div style={{ marginTop: 8, color: C.sub }}>
            This screen proxies the ranking service, so it needs
            {" "}<code>SERVICE_RANKING_BASE_URL</code> set and that service reachable.
          </div>
        </div>
      </div>
    );
  if (results === null || services === null) return <div style={s.wrap}>Loading…</div>;

  const top = results[0];

  return (
    <div style={s.wrap}>
      <Btn tone="ghost" onClick={onBack}>← Back</Btn>

      <div style={{ marginTop: 14, marginBottom: 4 }}>
        <div style={s.h1}>Choose a service</div>
        <div style={s.sub}>
          {results.length
            ? `${results.length} option${results.length === 1 ? "" : "s"} passed eligibility, ranked by fit. `
            : "Ranking hasn’t been run for this referral yet. "}
          Your choice is what moves the referral forward — and the label you attach is
          what teaches the ranker.
        </div>
      </div>

      <div style={{ display: "flex", gap: 10, alignItems: "center", margin: "10px 0 18px" }}>
        <Btn tone={results.length ? "ghost" : "accent"} small={!!results.length}
             disabled={rankBusy} onClick={runRanking}>
          {rankBusy ? "Ranking…" : results.length ? "Re-run ranking" : "Run service ranking"}
        </Btn>
        {rankError && <span style={{ fontSize: 12, color: C.danger }}>{rankError}</span>}
      </div>

      {results.length > 0 && (
        <div style={s.card}>
          <div style={{ flex: 1 }}>
            <div style={s.sectionLabel}>Recommended service</div>
            <select
              style={s.input}
              value={picked || ""}
              onChange={(e) => { setPicked(e.target.value); setOverrideServiceId(""); }}
            >
              {results.map((r) => (
                <option key={r.service_id} value={r.service_id}>
                  #{r.rank} · {r.service_name || r.service_id} — {pct(r.combined_score)}% match
                  {r.service_id === top.service_id ? " · top pick" : ""}
                </option>
              ))}
            </select>

            {selectedResult && !overrideServiceId && (
              <>
                <div style={{ display: "flex", gap: 16, marginTop: 12, flexWrap: "wrap" }}>
                  <Score label="overall" value={selectedResult.combined_score} strong />
                  <Score label="objective" value={selectedResult.objective_score} />
                  <Score label="judgement" value={selectedResult.subjective_score} />
                </div>
                {selectedResult.organization_name && (
                  <div style={{ fontSize: 12, color: C.sub, marginTop: 6 }}>
                    {selectedResult.organization_name}
                  </div>
                )}
                {selectedResult.subjective_rationale && (
                  // Layer 3's reasoning. Shown in full rather than truncated: it's the
                  // only part of the score a human can actually check.
                  <div style={s.rationale}>“{selectedResult.subjective_rationale}”</div>
                )}
              </>
            )}
          </div>
        </div>
      )}

      <div style={s.card}>
        <div style={{ flex: 1 }}>
          <div style={s.sectionLabel}>Override service</div>
          <div style={{ fontSize: 12, color: C.sub, marginBottom: 8 }}>
            Not in the ranked list, or you know better — pick any service directly.
          </div>
          <select
            style={s.input}
            value={overrideServiceId}
            onChange={(e) => setOverrideServiceId(e.target.value)}
          >
            <option value="">{results.length ? "Use the ranked selection above" : "Select a service…"}</option>
            {services.map((sv) => <option key={sv.id} value={sv.id}>{sv.name}</option>)}
          </select>
          {overrideServiceId && !selectedResult && (
            <div style={{ fontSize: 12, color: C.sub, marginTop: 6 }}>
              No ranking data for this service — it wasn’t part of the ranked shortlist.
            </div>
          )}
        </div>
      </div>

      <div style={s.card}>
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 700, color: C.ink, marginBottom: 6 }}>
            Why this one?
          </div>
          <div style={{ fontSize: 12, color: C.sub, marginBottom: 10 }}>
            Recorded to <code>sw_feedback</code> — this is what the ranker learns from.
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 10 }}>
            {LABELS.map(([value, text]) => (
              <button
                key={value}
                onClick={() => setLabel(value)}
                style={{ ...s.chip, ...(label === value ? s.chipOn : {}) }}
              >
                {text}
              </button>
            ))}
          </div>
          <input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Optional note (e.g. patient has used them before)"
            style={s.input}
          />
        </div>
      </div>

      <div style={{ display: "flex", gap: 8, marginTop: 16, alignItems: "center" }}>
        <Btn tone="ok" disabled={busy || !effectiveServiceId} onClick={confirm}>
          {busy ? "Submitting…" : "Complete referral →"}
        </Btn>
        <span style={{ fontSize: 12, color: C.sub }}>
          Releases the referral to outreach on the service you picked.
        </span>
      </div>
    </div>
  );
}

function Score({ label, value, strong }) {
  return (
    <div>
      <div style={{ fontSize: strong ? 18 : 15, fontWeight: 700,
                    color: strong ? C.accent : C.ink }}>
        {pct(value)}
      </div>
      <div style={{ fontSize: 10, color: C.sub, textTransform: "uppercase", letterSpacing: 0.4 }}>
        {label}
      </div>
    </div>
  );
}

const s = {
  wrap: { padding: 24, maxWidth: 860, margin: "0 auto" },
  h1: { fontSize: 22, fontWeight: 700, color: C.ink },
  sub: { fontSize: 13, color: C.sub, marginTop: 4, maxWidth: 620 },
  note: { marginTop: 16, background: "#fff", border: `1px dashed ${C.border}`, borderRadius: 12, padding: 16, fontSize: 13, color: C.ink },
  card: { display: "flex", gap: 12, alignItems: "flex-start", background: "#fff", border: "1px solid", borderColor: C.border, borderRadius: 12, padding: 14, marginBottom: 12 },
  sectionLabel: { fontSize: 12, fontWeight: 700, color: C.sub, textTransform: "uppercase", letterSpacing: 0.3, marginBottom: 8 },
  rationale: { marginTop: 10, fontSize: 13, color: C.ink, background: C.bg, borderRadius: 8, padding: "8px 10px", lineHeight: 1.45 },
  chip: { border: `1px solid ${C.border}`, background: "#fff", color: C.sub, borderRadius: 999, padding: "4px 11px", fontSize: 12, fontWeight: 600, cursor: "pointer" },
  chipOn: { borderColor: C.accent, color: C.accent, background: "#fff" },
  input: { width: "100%", boxSizing: "border-box", border: `1px solid ${C.border}`, borderRadius: 8, padding: "8px 10px", fontSize: 13 },
};
