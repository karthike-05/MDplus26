/**
 * ReferralDetail — one referral's full picture: patient + service, the current step's
 * action, and the outreach timeline (every attempt any channel recorded, §5b). The
 * timeline is the story the tool tells a social worker: what was tried, what came back.
 */

import { useEffect, useState } from "react";
import { api } from "./api.js";
import { C, Badge, Btn, RowActions, PatientResponse, CHANNEL_LABEL } from "./ui.jsx";

const fmt = (iso) => {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString(undefined, { hour: "numeric", minute: "2-digit", month: "short", day: "numeric" }); }
  catch { return ""; }
};
const DOT = { success: C.ok, needs_human: C.warn, failed: C.danger };

export default function ReferralDetail({ referralId, onBack, onReview }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  // Which scheduler owns transitions, so RowActions can offer the buttons or name the
  // owner instead of firing a guaranteed 409 (CLAUDE.md §7a).
  const [live, setLive] = useState(false);

  const load = () => api.referral(referralId).then(setData).catch((e) => setError(String(e)));
  useEffect(() => { setData(null); load(); }, [referralId]);
  useEffect(() => { api.dbMode().then((d) => setLive(d?.mode === "supabase")).catch(() => {}); }, []);

  if (error) return <div style={s.center}>Couldn’t load: {error}</div>;
  if (!data) return <div style={s.center}>Loading…</div>;

  const { referral, patient, service, attempts, patient_response, display_state } = data;
  // The action widget expects a dashboard-shaped row.
  const row = {
    referral_id: referral.id,
    current_state: display_state || referral.current_state,
    outreach_channel: referral.outreach_channel || "form",
  };

  return (
    <div style={s.wrap}>
      <Btn tone="ghost" small onClick={onBack}>← Dashboard</Btn>

      <div style={s.head}>
        <div>
          <div style={s.h1}>{patient.name}</div>
          <div style={s.sub}>{service?.name || referral.service_name} · {CHANNEL_LABEL[referral.outreach_channel] || referral.outreach_channel}</div>
        </div>
        <Badge state={display_state || referral.current_state} />
      </div>

      <div style={s.cols}>
        {/* left: facts + action */}
        <div style={s.panel}>
          <div style={s.sectionLabel}>Patient</div>
          {["dob", "phone", "address", "medicaid_id"].map((k) => patient[k] && (
            <div key={k} style={s.kv}><span style={s.k}>{k}</span><span>{patient[k]}</span></div>
          ))}
          {service && (
            <>
              <div style={{ ...s.sectionLabel, marginTop: 16 }}>Service contact</div>
              {service.phone && <div style={s.kv}><span style={s.k}>phone</span><span>{service.phone}</span></div>}
              {service.email && <div style={s.kv}><span style={s.k}>email</span><span>{service.email}</span></div>}
            </>
          )}
          <div style={{ ...s.sectionLabel, marginTop: 16 }}>Patient response</div>
          <div style={{ marginTop: 6 }}><PatientResponse response={patient_response} /></div>

          <div style={{ ...s.sectionLabel, marginTop: 16 }}>Next step</div>
          <div style={{ marginTop: 6 }}><RowActions row={row} onReview={onReview} onChange={load} live={live} /></div>
        </div>

        {/* right: timeline */}
        <div style={s.panel}>
          <div style={s.sectionLabel}>Outreach timeline</div>
          {attempts.length === 0 && <div style={{ color: C.sub, fontSize: 13, marginTop: 8 }}>No attempts yet.</div>}
          <div style={{ marginTop: 8 }}>
            {attempts.map((a, i) => (
              <div key={i} style={s.event}>
                <div style={{ ...s.dot, background: DOT[a.status] || C.sub }} />
                <div style={{ flex: 1 }}>
                  <div style={s.eventTop}>
                    <span style={s.eventChannel}>{CHANNEL_LABEL[a.channel] || a.channel}</span>
                    <span style={{ color: DOT[a.status] || C.sub, fontWeight: 600, fontSize: 12 }}>{a.status}</span>
                  </div>
                  <div style={s.eventSub}>
                    {a.from_state && <>from <b>{a.from_state}</b> · </>}{fmt(a.at)}
                  </div>
                  {a.data && Object.keys(a.data).length > 0 && (
                    <div style={s.eventData}>{summarize(a.data)}</div>
                  )}
                  {a.error && <div style={{ ...s.eventData, color: C.danger }}>{a.error}</div>}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

/** One readable line per attempt, across all three services. Each writes a different
 *  payload into the same `data` field (§5b), so this is where those shapes are decoded:
 *  form → us, phone → Voice/Retell, text → Messaging/Twilio, plus the inbound results
 *  those services post back to our adapters. */
function summarize(data) {
  // form (fill_form)
  if (data.output_path)
    return `filled ${(data.filled_fields || []).length} fields → ${String(data.output_path).split("/").slice(-2).join("/")}`;
  if (data.problems)
    return `needs ${Object.keys(data.problems).length} fix(es): ${Object.keys(data.problems).join(", ")}`;

  // phone (Voice / call_agent). A stubbed dispatch must never read as a placed call.
  if (data.stub)
    return `not actually placed — stubbed (${data.reason || "no CALL_AGENT_BASE_URL"})`;
  if (data.escalated) return `call_agent escalated: ${data.reason || "gave up"}`;
  if (data.placed) {
    const id = data.call_agent_response?.call_id;
    return id ? `call placed (${id})` : "call placed";
  }
  // inbound call result, posted back by call_agent
  if (data.confirmation_id || data.pickup_window || data.offered_datetime)
    return ["service confirmed", data.confirmation_id && `#${data.confirmation_id}`,
            data.pickup_window, data.offered_datetime && `offered ${data.offered_datetime}`]
      .filter(Boolean).join(" · ");

  // text (Messaging / patient_comms)
  if (data.intent) return data.intent.replace(/_/g, " ");
  if (data.event) return `patient event: ${String(data.event).replace(/_/g, " ")}`;
  if (data.reply_text) return `patient replied “${data.reply_text}”`;

  if (data.sent) return "email sent";
  const keys = Object.keys(data).filter((k) => k !== "stub");
  return keys.map((k) => `${k}: ${JSON.stringify(data[k])}`).join(" · ");
}

const s = {
  wrap: { padding: 24, maxWidth: 1000, margin: "0 auto" },
  center: { padding: 48, fontFamily: "system-ui", color: C.ink },
  head: { display: "flex", justifyContent: "space-between", alignItems: "center", margin: "14px 0 20px" },
  h1: { fontSize: 22, fontWeight: 700, color: C.ink },
  sub: { fontSize: 13, color: C.sub, marginTop: 2 },
  cols: { display: "grid", gridTemplateColumns: "300px 1fr", gap: 16, alignItems: "start" },
  panel: { background: "#fff", border: `1px solid ${C.border}`, borderRadius: 12, padding: 16, boxShadow: "0 1px 6px rgba(0,0,0,0.04)" },
  sectionLabel: { fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.4, color: C.sub },
  kv: { display: "flex", justifyContent: "space-between", gap: 10, padding: "6px 0", fontSize: 13, borderBottom: `1px solid ${C.bg}` },
  k: { color: C.sub, textTransform: "capitalize" },
  event: { display: "flex", gap: 10, padding: "10px 0", borderBottom: `1px solid ${C.bg}` },
  dot: { width: 10, height: 10, borderRadius: 999, marginTop: 5, flexShrink: 0 },
  eventTop: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  eventChannel: { fontSize: 13, fontWeight: 600, color: C.ink },
  eventSub: { fontSize: 12, color: C.sub, marginTop: 2 },
  eventData: { fontSize: 12, color: C.ink, marginTop: 4, background: C.bg, padding: "5px 8px", borderRadius: 6 },
};
