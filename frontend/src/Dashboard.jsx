/**
 * Dashboard — the social worker's home. One row per referral, the loop closing live
 * in the "Status" column as you act. This is where the differentiator shows: "service
 * accepted" and "patient used it" are distinct, visible milestones (§7, §12).
 */

import { useEffect, useState } from "react";
import { api } from "./api.js";
import { C, Badge, Btn, RowActions, CHANNEL_LABEL } from "./ui.jsx";

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

export default function Dashboard({ onReview, onOpen, onNew }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);

  const load = () => api.dashboard().then((d) => setRows(d.rows)).catch((e) => setError(String(e)));
  useEffect(() => { load(); }, []);

  if (error)
    return (
      <div style={s.center}>
        Backend not reachable: {error}
        <div style={{ color: C.sub, marginTop: 8 }}>
          Start it with <code>uvicorn backend.main:app --reload</code>
        </div>
      </div>
    );
  if (!rows) return <div style={s.center}>Loading…</div>;

  return (
    <div style={s.wrap}>
      <div style={s.head}>
        <div>
          <div style={s.h1}>Referrals</div>
          <div style={s.sub}>{rows.length} active · click a row for the full timeline</div>
        </div>
        <Btn onClick={onNew}>+ New referral</Btn>
      </div>

      <table style={s.table}>
        <thead>
          <tr>
            {["Patient", "Service", "Mode", "Status", "Confirmation", "Updated", "Next action"].map((h) => (
              <th key={h} style={s.th}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const conf = CONFIRM[r.confirmation_source];
            return (
              <tr key={r.referral_id} style={s.tr}>
                <td style={s.tdName} onClick={() => onOpen(r.referral_id)}>{r.patient_name}</td>
                <td style={s.td} onClick={() => onOpen(r.referral_id)}>{r.service_name || "—"}</td>
                <td style={s.td}>{CHANNEL_LABEL[r.outreach_channel] || r.outreach_channel}</td>
                <td style={s.td}><Badge state={r.current_state} /></td>
                <td style={s.td}>{conf ? <span style={{ color: conf.color, fontWeight: 600, fontSize: 12 }}>{conf.text}</span> : <span style={{ color: C.sub }}>—</span>}</td>
                <td style={{ ...s.td, color: C.sub, fontSize: 12 }}>{fmtTime(r.updated_at)}</td>
                <td style={s.td}><RowActions row={r} onReview={onReview} onChange={load} small /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const s = {
  wrap: { padding: 24, maxWidth: 1100, margin: "0 auto" },
  center: { padding: 48, fontFamily: "system-ui", color: C.ink },
  head: { display: "flex", justifyContent: "space-between", alignItems: "flex-end", marginBottom: 16 },
  h1: { fontSize: 22, fontWeight: 700, color: C.ink },
  sub: { fontSize: 13, color: C.sub, marginTop: 2 },
  table: { width: "100%", borderCollapse: "collapse", background: "#fff", borderRadius: 12, overflow: "hidden", boxShadow: "0 1px 8px rgba(0,0,0,0.05)" },
  th: { textAlign: "left", fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: C.sub, padding: "12px 14px", borderBottom: `1px solid ${C.border}`, background: C.bg },
  tr: { borderBottom: `1px solid ${C.border}` },
  td: { padding: "14px", fontSize: 14, color: C.ink, verticalAlign: "middle" },
  tdName: { padding: "14px", fontSize: 14, fontWeight: 600, color: C.accent, cursor: "pointer", verticalAlign: "middle" },
};
