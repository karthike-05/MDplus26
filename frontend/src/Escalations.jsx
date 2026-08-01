/**
 * Escalations — the social worker's queue of referrals the agent could not finish.
 *
 * WHY THIS SCREEN EXISTS (whats-left B2). `advance_referral` queues
 * `escalate_to_social_worker` on four paths: the patient declined consent, no eligible
 * resource remains, every channel on every candidate was exhausted, and the patient
 * reported they never used the service. Nothing polls that component on purpose — a
 * person is meant to be the poller. There just wasn't a screen, so nobody ever was: a
 * declined referral dropped out of the dashboard's active groups and sat in a queue with
 * no UI. A referral tool that silently loses its failures is precisely the thing this
 * product exists to replace, so this is a product hole, not a nice-to-have.
 *
 * Deliberately separate from the dashboard rather than a filter on it. The dashboard
 * answers "how is the caseload moving"; this answers "what is stuck on me right now",
 * and burying that in a group someone has to scroll to is how it got missed before.
 */

import { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { C, Btn } from "./ui.jsx";

const fmt = (iso) => {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    });
  } catch { return ""; }
};

// The DB writes these verbatim into `referrals.escalation_reason`. Mapping them to
// plain language (and to what the SW should actually DO) is the difference between a
// queue and a to-do list.
const REASON_HINT = [
  [/consent/i, "The patient declined or never answered the opt-in text. Call them, or close the referral."],
  [/no eligible|no candidate/i, "Ranking found nothing that fits. Widen the search or refer out of network."],
  [/exhausted|no unused channel|three attempts/i, "Every contact method for every candidate service failed. Try a service directly."],
  [/did not use|not utili/i, "The patient said they never used the service. Find out what blocked them — this is the case the product exists to catch."],
];

const hintFor = (reason) =>
  REASON_HINT.find(([re]) => re.test(reason || ""))?.[1]
  ?? "Review this referral and decide the next step by hand.";

export default function Escalations({ onOpen }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);
  const timer = useRef(null);

  const load = async (quiet = false) => {
    try {
      const d = await api.escalations();
      setRows(d.escalations);
      setError(null);
    } catch (e) {
      if (!quiet) setError(String(e));
    }
  };

  useEffect(() => {
    load();
    timer.current = setInterval(() => { if (!document.hidden) load(true); }, 5000);
    return () => clearInterval(timer.current);
  }, []);

  const resolve = async (row) => {
    setBusy(row.action_id);
    try {
      await api.resolveEscalation(row.referral_id, row.action_id);
      await load();
    } catch (e) {
      alert(String(e));
    } finally {
      setBusy(null);
    }
  };

  if (error) return <div style={s.center}>Couldn’t load escalations: {error}</div>;
  if (!rows) return <div style={s.center}>Loading…</div>;

  return (
    <div style={s.wrap}>
      <div style={s.head}>
        <div>
          <div style={s.h1}>Escalations</div>
          <div style={s.sub}>
            {rows.length === 0
              ? "Nothing needs you right now."
              : `${rows.length} referral${rows.length === 1 ? "" : "s"} the agent couldn’t finish`}
          </div>
        </div>
        <Btn tone="ghost" onClick={() => load()}>↻ Refresh</Btn>
      </div>

      {rows.length === 0 && (
        <div style={s.empty}>
          <div style={{ fontSize: 28, marginBottom: 8 }}>✓</div>
          Every referral is either moving on its own or already closed.
          <div style={{ fontSize: 12, color: C.sub, marginTop: 8 }}>
            Referrals land here when consent is declined, no eligible service remains,
            every channel has been tried, or the patient reports they never used the
            service.
          </div>
        </div>
      )}

      {rows.map((r) => (
        <div key={r.action_id} style={s.card}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
              <button style={s.name} onClick={() => onOpen?.(r.referral_id)}>
                {r.patient_name || r.referral_id.slice(0, 8)}
              </button>
              <span style={s.meta}>
                {r.need_category}
                {r.service_name ? ` · ${r.service_name}` : ""}
              </span>
              {r.queued_at && <span style={s.meta}>· {fmt(r.queued_at)}</span>}
            </div>
            <div style={s.reason}>{r.reason}</div>
            <div style={s.hint}>{hintFor(r.reason)}</div>
            {r.patient_phone && <div style={s.phone}>{r.patient_phone}</div>}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, flexShrink: 0 }}>
            <Btn small onClick={() => onOpen?.(r.referral_id)}>Open</Btn>
            <Btn small tone="ghost" disabled={busy === r.action_id}
                 onClick={() => resolve(r)}>
              {busy === r.action_id ? "…" : "Mark handled"}
            </Btn>
          </div>
        </div>
      ))}
    </div>
  );
}

const s = {
  wrap: { padding: 24, maxWidth: 1000, margin: "0 auto" },
  center: { padding: 48, fontFamily: "system-ui", color: C.ink },
  head: { display: "flex", justifyContent: "space-between", alignItems: "center", margin: "14px 0 20px" },
  h1: { fontSize: 22, fontWeight: 700, color: C.ink },
  sub: { fontSize: 13, color: C.sub, marginTop: 2 },
  empty: {
    background: "#fff", border: `1px solid ${C.border}`, borderRadius: 12,
    padding: "40px 24px", textAlign: "center", color: C.ink, fontSize: 14,
  },
  card: {
    display: "flex", gap: 16, alignItems: "flex-start",
    background: "#fff", border: `1px solid ${C.border}`, borderLeft: `3px solid ${C.warn}`,
    borderRadius: 12, padding: 16, marginBottom: 10,
    boxShadow: "0 1px 6px rgba(0,0,0,0.04)",
  },
  name: {
    background: "none", border: "none", padding: 0, cursor: "pointer",
    font: "inherit", fontWeight: 700, fontSize: 15, color: C.ink, textDecoration: "underline",
  },
  meta: { fontSize: 12, color: C.sub },
  reason: { fontSize: 13, color: C.warn, fontWeight: 600, marginTop: 6 },
  hint: { fontSize: 13, color: C.sub, marginTop: 4 },
  phone: { fontSize: 12, color: C.sub, marginTop: 6, fontVariantNumeric: "tabular-nums" },
};
