/**
 * Dashboard — the social worker's home, organised the way they actually triage:
 * what needs me, what's in flight, what closed.
 *
 * The differentiator lives in the two right-hand columns. "Service accepted" and
 * "patient used it" are separate facts (§7, §12), so the board shows the service's answer
 * and the PATIENT's answer side by side and never collapses them — a referral the org
 * approved but the patient never used is a *failure* that reads as a success everywhere
 * else in this industry.
 *
 * "Channels" is where all three services become visible in one place: form (us), phone
 * (Voice/Retell) and text (Messaging/Twilio) each leave attempt rows, so a failed phone
 * attempt followed by a form attempt shows as two chips.
 */

import { useEffect, useState } from "react";
import { api } from "./api.js";
import { C, Badge, Btn, RowActions, ChannelsTried, PatientResponse, CHANNEL_LABEL } from "./ui.jsx";

const fmtTime = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  } catch {
    return "—";
  }
};

const CONFIRM = {
  org_email: { text: "Service ✓", color: C.teal },
  patient_reply: { text: "Patient ✓", color: C.ok },
};

// How the board is grouped. Order matters: what needs a human comes first.
const GROUPS = [
  {
    key: "attention",
    title: "Needs you",
    blurb: "Blocked, escalated, or waiting on a form review",
    accent: C.warn,
    match: (r) =>
      r.needs_attention ||
      r.awaiting_sw_selection ||
      (r.current_state === "outreach_in_progress" && r.outreach_channel === "form"),
  },
  {
    key: "active",
    title: "In progress",
    blurb: "Outreach under way or awaiting a reply",
    accent: C.accent,
    match: (r) => r.current_state !== "completed",
  },
  {
    key: "closed",
    title: "Closed the loop",
    blurb: "The patient confirmed they actually used the service",
    accent: C.ok,
    match: (r) => r.current_state === "completed",
  },
];

export default function Dashboard({ onReview, onOpen, onChoose, onNew }) {
  const [rows, setRows] = useState(null);
  const [dbInfo, setDbInfo] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setBusy(true);
    try {
      const d = await api.dashboard();
      setRows(d.rows);
      setDbInfo(d.db ?? (await api.dbMode()));
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => { load(); }, []);

  const switchDb = async () => {
    const next = dbInfo?.mode === "supabase" ? "mock" : "supabase";
    setBusy(true);
    try {
      setDbInfo(await api.setDbMode(next));
      await load();
    } catch (e) {
      // The backend 400s when creds are absent rather than silently staying put, so a
      // failed switch is a real message instead of a no-op that looks like success.
      alert(`Could not switch to ${next}: ${e}`);
      setBusy(false);
    }
  };

  if (error)
    return (
      <div style={s.center}>
        Backend not reachable: {error}
        <div style={{ color: C.sub, marginTop: 8 }}>
          Start it with <code>uvicorn backend.main:app --reload</code>
        </div>
        <div style={{ marginTop: 16 }}><Btn onClick={load}>Retry</Btn></div>
      </div>
    );
  if (!rows) return <div style={s.center}>Loading…</div>;

  // First matching group wins, so each referral appears exactly once.
  const bucket = (r) => GROUPS.find((g) => g.match(r))?.key ?? "active";
  const grouped = GROUPS.map((g) => ({ ...g, rows: rows.filter((r) => bucket(r) === g.key) }));

  return (
    <div style={s.wrap}>
      <div style={s.head}>
        <div>
          <div style={s.h1}>Referrals</div>
          <div style={s.sub}>
            {rows.length} total · {grouped.find((g) => g.key === "closed").rows.length} closed the loop
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <DataSource info={dbInfo} onSwitch={switchDb} busy={busy} />
          <Btn tone="ghost" disabled={busy} onClick={load}>{busy ? "…" : "↻ Refresh"}</Btn>
          <Btn onClick={onNew}>+ New referral</Btn>
        </div>
      </div>

      {grouped.map((g) => (
        <section key={g.key} style={{ marginBottom: 26 }}>
          <div style={s.groupHead}>
            <span style={{ ...s.groupDot, background: g.accent }} />
            <span style={s.groupTitle}>{g.title}</span>
            <span style={s.groupCount}>{g.rows.length}</span>
            <span style={s.groupBlurb}>{g.blurb}</span>
          </div>

          {g.rows.length === 0 ? (
            <div style={s.empty}>Nothing here.</div>
          ) : (
            <table style={s.table}>
              <thead>
                <tr>
                  {["Patient", "Service", "Status", "Channels tried", "Patient response", "Confirmation", "Updated", "Next action"].map((h) => (
                    <th key={h} style={s.th}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {g.rows.map((r) => {
                  const conf = CONFIRM[r.confirmation_source];
                  return (
                    <tr key={r.referral_id} style={s.tr}>
                      <td style={s.tdName} onClick={() => onOpen(r.referral_id)}>{r.patient_name}</td>
                      <td style={s.td} onClick={() => onOpen(r.referral_id)}>
                        {r.service_name || "—"}
                        <div style={{ fontSize: 11, color: C.sub, marginTop: 2 }}>
                          via {CHANNEL_LABEL[r.outreach_channel] || r.outreach_channel}
                        </div>
                      </td>
                      <td style={s.td}><Badge state={r.current_state} /></td>
                      <td style={s.td}><ChannelsTried channels={r.channels_tried} count={r.attempt_count} /></td>
                      <td style={s.td}><PatientResponse response={r.patient_response} /></td>
                      <td style={s.td}>
                        {conf
                          ? <span style={{ color: conf.color, fontWeight: 600, fontSize: 12 }}>{conf.text}</span>
                          : <span style={{ color: C.sub }}>—</span>}
                      </td>
                      <td style={{ ...s.td, color: C.sub, fontSize: 12 }}>{fmtTime(r.updated_at)}</td>
                      <td style={s.td}>
                        {/* The SW selection gate is a REAL action in both modes — the
                            referral is parked waiting for exactly this person, so it
                            takes precedence over the "driven by the DB" note below. */}
                        {r.awaiting_sw_selection
                          ? <Btn small tone="ok" onClick={() => onChoose(r.referral_id)}>
                              Choose service →
                            </Btn>
                          : dbInfo?.mode === "supabase"
                          // Otherwise, live, advance_referral() owns the workflow (§7a) —
                          // our run / simulated-inbound buttons are not the driver there,
                          // and offering them would imply a control we don't have.
                          ? <span style={{ fontSize: 11, color: C.sub }}>driven by the DB scheduler</span>
                          : <RowActions row={r} onReview={onReview} onChange={load} small />}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </section>
      ))}
    </div>
  );
}

/** Which store the board is reading, and a one-click flip. Worth showing prominently:
 *  "no referrals" means something completely different on the mock than on Supabase. */
function DataSource({ info, onSwitch, busy }) {
  if (!info) return null;
  const live = info.mode === "supabase";
  const canSwitch = live || info.supabase_configured;
  return (
    <div style={s.dbWrap} title={`scheduler: ${info.scheduler}`}>
      <span style={{ ...s.dbDot, background: live ? C.teal : C.sub }} />
      <span style={s.dbLabel}>{live ? "Supabase (live)" : "Mock (offline)"}</span>
      <Btn
        small
        tone="ghost"
        disabled={busy || !canSwitch}
        onClick={onSwitch}
      >
        {canSwitch ? (live ? "Use mock" : "Use Supabase") : "Supabase not configured"}
      </Btn>
    </div>
  );
}

const s = {
  wrap: { padding: 24, maxWidth: 1240, margin: "0 auto" },
  center: { padding: 48, fontFamily: "system-ui", color: C.ink },
  head: { display: "flex", justifyContent: "space-between", alignItems: "flex-end", marginBottom: 22, gap: 16, flexWrap: "wrap" },
  h1: { fontSize: 22, fontWeight: 700, color: C.ink },
  sub: { fontSize: 13, color: C.sub, marginTop: 2 },
  dbWrap: { display: "flex", alignItems: "center", gap: 8, background: "#fff", border: `1px solid ${C.border}`, borderRadius: 999, padding: "4px 6px 4px 12px" },
  dbDot: { width: 8, height: 8, borderRadius: 999, display: "inline-block" },
  dbLabel: { fontSize: 12, fontWeight: 600, color: C.ink },
  groupHead: { display: "flex", alignItems: "center", gap: 8, marginBottom: 8 },
  groupDot: { width: 8, height: 8, borderRadius: 999, display: "inline-block" },
  groupTitle: { fontSize: 14, fontWeight: 700, color: C.ink },
  groupCount: { fontSize: 11, fontWeight: 700, color: C.sub, background: C.bg, border: `1px solid ${C.border}`, borderRadius: 999, padding: "1px 7px" },
  groupBlurb: { fontSize: 12, color: C.sub },
  empty: { fontSize: 13, color: C.sub, background: "#fff", border: `1px dashed ${C.border}`, borderRadius: 12, padding: "14px 16px" },
  table: { width: "100%", borderCollapse: "collapse", background: "#fff", borderRadius: 12, overflow: "hidden", boxShadow: "0 1px 8px rgba(0,0,0,0.05)" },
  th: { textAlign: "left", fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: C.sub, padding: "12px 14px", borderBottom: `1px solid ${C.border}`, background: C.bg },
  tr: { borderBottom: `1px solid ${C.border}` },
  td: { padding: "14px", fontSize: 14, color: C.ink, verticalAlign: "middle" },
  tdName: { padding: "14px", fontSize: 14, fontWeight: 600, color: C.accent, cursor: "pointer", verticalAlign: "middle" },
};
