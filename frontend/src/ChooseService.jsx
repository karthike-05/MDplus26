/**
 * ChooseService — the social worker picks which service a referral goes to.
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
 * a fast one, but recorded as a decision.
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
  const [results, setResults] = useState(null);
  const [error, setError] = useState(null);
  const [picked, setPicked] = useState(null);
  const [label, setLabel] = useState("good_fit");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.ranking(referralId)
      .then((d) => {
        const rows = d.results || [];
        setResults(rows);
        if (rows.length) setPicked(rows[0].service_id);   // top pick pre-selected
      })
      .catch((e) => setError(String(e)));
  }, [referralId]);

  const confirm = async () => {
    setBusy(true);
    try {
      await api.chooseService(referralId, { service_id: picked, label, label_notes: notes || null });
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
          Couldn’t load the ranked options: {error}
          <div style={{ marginTop: 8, color: C.sub }}>
            This screen proxies the ranking service, so it needs
            {" "}<code>SERVICE_RANKING_BASE_URL</code> set and that service reachable.
          </div>
        </div>
      </div>
    );
  if (!results) return <div style={s.wrap}>Loading ranked options…</div>;

  if (!results.length)
    return (
      <div style={s.wrap}>
        <Btn tone="ghost" onClick={onBack}>← Back</Btn>
        <div style={s.note}>
          No ranked options yet — ranking hasn’t run for this referral, or every candidate
          failed the hard filter.
        </div>
      </div>
    );

  const top = results[0];

  return (
    <div style={s.wrap}>
      <Btn tone="ghost" onClick={onBack}>← Back</Btn>

      <div style={{ marginTop: 14, marginBottom: 4 }}>
        <div style={s.h1}>Choose a service</div>
        <div style={s.sub}>
          {results.length} option{results.length === 1 ? "" : "s"} passed eligibility,
          ranked by fit. Your choice is what moves the referral forward — and the label
          you attach is what teaches the ranker.
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 10, margin: "16px 0" }}>
        {results.map((r) => {
          const isPicked = r.service_id === picked;
          const isTop = r.service_id === top.service_id;
          return (
            <label
              key={r.service_id}
              style={{ ...s.card, borderColor: isPicked ? C.accent : C.border,
                       boxShadow: isPicked ? `0 0 0 2px ${C.accent}22` : "none" }}
            >
              <input
                type="radio"
                name="service"
                checked={isPicked}
                onChange={() => setPicked(r.service_id)}
                style={{ marginTop: 4 }}
              />
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ fontWeight: 700, color: C.ink }}>
                    {r.service_name || r.service_id}
                  </span>
                  {isTop && <span style={s.topPill}>ranker’s top pick</span>}
                  <span style={s.rank}>#{r.rank}</span>
                </div>
                {r.organization_name && (
                  <div style={{ fontSize: 12, color: C.sub, marginTop: 2 }}>
                    {r.organization_name}
                  </div>
                )}

                <div style={{ display: "flex", gap: 16, marginTop: 8, flexWrap: "wrap" }}>
                  <Score label="overall" value={r.combined_score} strong />
                  <Score label="objective" value={r.objective_score} />
                  <Score label="judgement" value={r.subjective_score} />
                </div>

                {r.subjective_rationale && (
                  // Layer 3's reasoning. Shown in full rather than truncated: it's the
                  // only part of the score a human can actually check.
                  <div style={s.rationale}>“{r.subjective_rationale}”</div>
                )}
              </div>
            </label>
          );
        })}
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
        <Btn tone="ok" disabled={busy || !picked} onClick={confirm}>
          {busy ? "Recording…" : picked === top.service_id
            ? "Accept top pick & continue"
            : "Choose this service & continue"}
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
  card: { display: "flex", gap: 12, alignItems: "flex-start", background: "#fff", border: "1px solid", borderColor: C.border, borderRadius: 12, padding: 14, cursor: "pointer" },
  topPill: { fontSize: 10, fontWeight: 700, color: C.teal, border: `1px solid ${C.teal}`, borderRadius: 999, padding: "1px 7px" },
  rank: { fontSize: 11, fontWeight: 700, color: C.sub, background: C.bg, borderRadius: 999, padding: "1px 7px" },
  rationale: { marginTop: 10, fontSize: 13, color: C.ink, background: C.bg, borderRadius: 8, padding: "8px 10px", lineHeight: 1.45 },
  chip: { border: `1px solid ${C.border}`, background: "#fff", color: C.sub, borderRadius: 999, padding: "4px 11px", fontSize: 12, fontWeight: 600, cursor: "pointer" },
  chipOn: { borderColor: C.accent, color: C.accent, background: "#fff" },
  input: { width: "100%", boxSizing: "border-box", border: `1px solid ${C.border}`, borderRadius: 8, padding: "8px 10px", fontSize: 13 },
};
