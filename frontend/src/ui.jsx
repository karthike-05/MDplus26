// Shared palette, badges, buttons, and the per-state action map. Kept in one place
// so Dashboard / ReferralDetail / Services read as one system (matches ReviewUI).

import { useState } from "react";
import { api } from "./api.js";

export const C = {
  ink: "#1a202c",
  sub: "#718096",
  border: "#e2e8f0",
  bg: "#f7fafc",
  accent: "#2b6cb0",
  ok: "#2f855a",
  warn: "#c05621",
  danger: "#c53030",
  purple: "#6b46c1",
  teal: "#2c7a7b",
};

// state -> { label, color } for the status badge.
export const STATE_META = {
  created: { label: "New", color: C.sub },
  consent_pending: { label: "Awaiting opt-in", color: C.warn },
  consent_granted: { label: "Consented", color: C.accent },
  outreach_in_progress: { label: "Placing", color: C.accent },
  submitted: { label: "Submitted", color: C.purple },
  confirmed: { label: "Service accepted", color: C.teal },
  check_in_scheduled: { label: "Check-in sent", color: C.teal },
  completed: { label: "Completed", color: C.ok },
  needs_human: { label: "Needs worker", color: C.warn },
  escalated: { label: "Escalated", color: C.danger },
};

export const CHANNEL_LABEL = { form: "📄 Form", phone: "📞 Phone", text: "💬 Text", email: "✉️ Email" };

export function Badge({ state }) {
  const m = STATE_META[state] || { label: state, color: C.sub };
  return (
    <span style={{ background: m.color, color: "#fff", fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 999, whiteSpace: "nowrap" }}>
      {m.label}
    </span>
  );
}

export function Btn({ children, onClick, tone = "accent", disabled, small }) {
  const bg = { accent: C.accent, ok: C.ok, ghost: "#fff", danger: C.danger }[tone];
  const ghost = tone === "ghost";
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        background: ghost ? "#fff" : bg,
        color: ghost ? C.ink : "#fff",
        border: ghost ? `1px solid ${C.border}` : "none",
        padding: small ? "5px 10px" : "8px 14px",
        borderRadius: 8,
        fontSize: small ? 12 : 13,
        fontWeight: 600,
        cursor: disabled ? "default" : "pointer",
        opacity: disabled ? 0.5 : 1,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </button>
  );
}

// What the social worker can do next, by state. Sim signals stand in for the real
// inbound webhooks (patient/service replies) so the loop is demoable offline (§7).
export function actionFor(row) {
  const s = row.current_state;
  switch (s) {
    case "created":
      return { run: "Request consent" };
    case "consent_pending":
      return { wait: "Awaiting patient opt-in", sims: [["consent", "Patient opts in ✓"], ["decline", "Declines ✕"]] };
    case "consent_granted":
      return { run: "Place referral" };
    case "outreach_in_progress":
      return row.outreach_channel === "form" ? { review: "Review & submit" } : { run: "Place referral" };
    case "submitted":
      return { wait: "Awaiting service response", sims: [["response", "Service accepts ✓"], ["no_response", "No response ✕"]] };
    case "confirmed":
      return { run: "Schedule check-in" };
    case "check_in_scheduled":
      return { wait: "Awaiting patient", sims: [["used", "Patient replies “Y” ✓"], ["not_used", "Not used ✕"]] };
    case "completed":
      return { done: true };
    default:
      return { flag: true }; // needs_human / escalated
  }
}

// The one control that pushes a referral forward — reused by the dashboard rows and
// the referral detail. Runs the auto-tool (`run`), opens the review screen (`review`),
// or fires a simulated inbound signal, then calls onChange() to refresh.
export function RowActions({ row, onReview, onChange, small }) {
  const [busy, setBusy] = useState(false);
  const a = actionFor(row);
  const go = async (fn) => {
    setBusy(true);
    try {
      await fn();
      await onChange?.();
    } catch (e) {
      alert(String(e));
    } finally {
      setBusy(false);
    }
  };
  if (a.done) return <span style={{ color: C.ok, fontWeight: 600, fontSize: 13 }}>✅ Loop closed</span>;
  if (a.flag) return <span style={{ color: C.warn, fontWeight: 600, fontSize: 13 }}>⚠ Needs social worker</span>;
  if (a.review) return <Btn small={small} onClick={() => onReview?.(row.referral_id)}>{a.review}</Btn>;
  if (a.run)
    return (
      <Btn small={small} disabled={busy} onClick={() => go(() => api.run(row.referral_id))}>
        {busy ? "…" : a.run}
      </Btn>
    );
  if (a.sims)
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <span style={{ fontSize: 11, color: C.sub }}>{a.wait}</span>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {a.sims.map(([sig, label]) => (
            <Btn key={sig} small tone="ghost" disabled={busy} onClick={() => go(() => api.inbound(row.referral_id, sig))}>
              {label}
            </Btn>
          ))}
        </div>
      </div>
    );
  return null;
}
