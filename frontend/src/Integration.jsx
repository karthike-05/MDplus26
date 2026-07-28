/**
 * Integration — the four-service loop on one screen.
 *
 * This is the screen for a group walkthrough. The other views answer "how is this
 * patient's referral doing"; this one answers "is the system actually running, and if
 * not, whose part is stuck".
 *
 * It exists because every failure mode on the shared bus is SILENT. An action addressed
 * to a component nobody polls raises nothing. An empty `referral_service_candidates`
 * raises nothing. An unset `ORCHESTRATOR_BASE_URL` in someone else's deploy raises
 * nothing — the webhook simply never arrives. All four present identically as a board
 * that stops updating, which is the one thing you cannot debug in front of an audience.
 * So the backend names the cause (`blockers`) and this renders it at the top.
 *
 * Everything here is read-only. Advancing the loop is the DB scheduler's job (§7a).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { C, Btn } from "./ui.jsx";

const SEVERITY = {
  blocker: { color: C.danger, bg: "#fff5f5", icon: "●" },
  warning: { color: C.warn, bg: "#fffaf0", icon: "▲" },
  info: { color: C.sub, bg: C.bg, icon: "•" },
};

const ACTION_STATUS = {
  ready: { color: C.warn, label: "ready" },
  in_progress: { color: C.accent, label: "in progress" },
  blocked: { color: C.purple, label: "blocked" },
  completed: { color: C.ok, label: "completed" },
  failed: { color: C.danger, label: "failed" },
  cancelled: { color: C.sub, label: "cancelled" },
  pending: { color: C.sub, label: "pending" },
};

const short = (id) => (id ? String(id).slice(0, 8) : "—");
const time = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" });
  } catch {
    return "—";
  }
};

export default function Integration() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [auto, setAuto] = useState(true);
  const timer = useRef(null);

  const load = useCallback(async () => {
    try {
      setData(await api.system());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Poll while the tab is open. There's no realtime subscription yet (B6), and a static
  // snapshot is actively misleading on a screen whose whole purpose is "is it moving".
  useEffect(() => {
    if (!auto) return undefined;
    timer.current = setInterval(load, 4000);
    return () => clearInterval(timer.current);
  }, [auto, load]);

  if (error)
    return (
      <div style={s.wrap}>
        <div style={s.center}>
          Backend not reachable: {error}
          <div style={{ color: C.sub, marginTop: 8 }}>
            <code>uvicorn backend.main:app --reload</code>
          </div>
          <div style={{ marginTop: 16 }}><Btn onClick={load}>Retry</Btn></div>
        </div>
      </div>
    );
  if (!data) return <div style={s.wrap}><div style={s.center}>Loading…</div></div>;
  if (data.error)
    return (
      <div style={s.wrap}>
        <div style={s.center}>Couldn’t read the action queue: {data.error}</div>
      </div>
    );

  const { worker, db, components = [], queue = [], events = [], blockers = [] } = data;
  const live = db?.mode === "supabase";

  return (
    <div style={s.wrap}>
      <div style={s.head}>
        <div>
          <div style={s.h1}>Integration</div>
          <div style={s.sub}>
            {live ? "Live — the DB's advance_referral() owns the workflow"
                  : "Offline mock — our own scheduler owns the workflow"}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label style={s.auto}>
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            auto-refresh
          </label>
          <Btn tone="ghost" onClick={load}>↻ Refresh</Btn>
        </div>
      </div>

      {blockers.length > 0 && (
        <section style={{ marginBottom: 24 }}>
          {blockers.map((b, i) => {
            const meta = SEVERITY[b.severity] || SEVERITY.info;
            return (
              <div key={i} style={{ ...s.blocker, background: meta.bg, borderColor: meta.color }}>
                <span style={{ color: meta.color, fontWeight: 700 }}>{meta.icon}</span>
                <div>
                  <div style={{ fontWeight: 700, color: C.ink, fontSize: 14 }}>
                    {b.title}
                    <span style={s.owner}>{b.owner}</span>
                    <span style={s.tag}>{b.id}</span>
                  </div>
                  <div style={{ fontSize: 13, color: C.sub, marginTop: 3 }}>{b.detail}</div>
                </div>
              </div>
            );
          })}
        </section>
      )}

      <Worker worker={worker} />

      <section style={{ marginBottom: 26 }}>
        <div style={s.groupTitle}>Components on the bus</div>
        <div style={s.cards}>
          {components.map((c) => (
            <div key={c.name} style={s.card}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <code style={s.code}>{c.name}</code>
                <span style={{
                  ...s.pill,
                  color: c.polled_by_us ? C.ok : C.sub,
                  borderColor: c.polled_by_us ? C.ok : C.border,
                }}>
                  {c.polled_by_us ? "we poll it" : "they poll it"}
                </span>
              </div>
              <div style={{ fontSize: 12, color: C.sub, marginTop: 6 }}>{c.owner}</div>
              <div style={{ marginTop: 10, fontSize: 13 }}>
                <b style={{ color: c.open > 0 && !c.polled_by_us ? C.warn : C.ink }}>{c.open}</b>
                <span style={{ color: C.sub }}> open · {c.total} total</span>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section style={{ marginBottom: 26 }}>
        <div style={s.groupTitle}>
          Action queue <span style={s.hint}>referral_actions — the DB scheduler's outbox</span>
        </div>
        {queue.length === 0 ? (
          <div style={s.empty}>Queue is empty.</div>
        ) : (
          <table style={s.table}>
            <thead>
              <tr>{["Action", "Component", "Status", "Referral", "Created", "Error"].map((h) => (
                <th key={h} style={s.th}>{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {queue.map((a) => {
                const meta = ACTION_STATUS[a.action_status] || { color: C.sub, label: a.action_status };
                return (
                  <tr key={a.id} style={s.tr}>
                    <td style={s.td}><code style={s.code}>{a.action_type}</code></td>
                    <td style={s.td}><code style={s.codeDim}>{a.component}</code></td>
                    <td style={s.td}>
                      <span style={{ ...s.status, color: meta.color, borderColor: meta.color }}>
                        {meta.label}
                      </span>
                    </td>
                    <td style={{ ...s.td, color: C.sub, fontSize: 12 }}>{short(a.referral_id)}</td>
                    <td style={{ ...s.td, color: C.sub, fontSize: 12 }}>{time(a.created_at)}</td>
                    <td style={{ ...s.td, color: C.danger, fontSize: 12 }}>{a.error || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <section>
        <div style={s.groupTitle}>
          Inbound webhooks <span style={s.hint}>integration_events — what Voice and Messaging sent us</span>
        </div>
        {events.length === 0 ? (
          <div style={s.empty}>
            Nothing received yet. If the other services are running, check that
            {" "}<code>ORCHESTRATOR_BASE_URL</code> (Voice) and <code>ORG_BACKEND_URL</code>{" "}
            (Messaging) point at this backend — unset, both skip silently.
          </div>
        ) : (
          <table style={s.table}>
            <thead>
              <tr>{["From", "Event", "Referral", "Status", "Received"].map((h) => (
                <th key={h} style={s.th}>{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {events.map((e, i) => (
                <tr key={i} style={s.tr}>
                  <td style={s.td}><code style={s.codeDim}>{e.provider}</code></td>
                  <td style={s.td}>{e.event_type}</td>
                  <td style={{ ...s.td, color: C.sub, fontSize: 12 }}>{short(e.referral_id)}</td>
                  <td style={{ ...s.td, fontSize: 12,
                               color: e.processing_status === "processed" ? C.ok : C.danger }}>
                    {e.processing_status}{e.error ? ` — ${e.error}` : ""}
                  </td>
                  <td style={{ ...s.td, color: C.sub, fontSize: 12 }}>{time(e.received_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

/** The worker's own vitals. A stopped worker and an idle queue look identical from the
 *  outside — that's the whole reason this is on screen rather than in logs. */
function Worker({ worker }) {
  if (!worker) return null;
  const dead = worker.enabled && !worker.running;
  return (
    <section style={{ marginBottom: 26 }}>
      <div style={s.groupTitle}>Our worker</div>
      <div style={{ ...s.card, borderColor: dead ? C.danger : C.border }}>
        <div style={{ display: "flex", gap: 20, flexWrap: "wrap", alignItems: "center" }}>
          <span style={{
            ...s.pill,
            color: worker.running ? C.ok : worker.enabled ? C.danger : C.sub,
            borderColor: worker.running ? C.ok : worker.enabled ? C.danger : C.border,
          }}>
            {worker.running ? "running" : worker.enabled ? "STOPPED" : "disabled"}
          </span>
          <Stat label="ticks" value={worker.ticks} />
          <Stat label="actions serviced" value={worker.actions_serviced} />
          <Stat label="reclaimed after a crash" value={worker.actions_reclaimed} />
          <Stat label="last tick" value={time(worker.last_tick_at)} />
          <Stat label="every" value={`${worker.poll_seconds}s`} />
        </div>
        <div style={{ fontSize: 12, color: C.sub, marginTop: 10 }}>
          polling{" "}
          {(worker.components || []).map((name, i) => (
            <span key={name}>
              {i > 0 && " + "}
              <code style={s.code}>{name}</code>
            </span>
          ))}
          {worker.orchestrator_tick && " · advancing all open referrals each tick"}
          {worker.claims_ranking && " · claiming rank_resources"}
        </div>
        {worker.last_error && (
          <div style={{ fontSize: 12, color: C.danger, marginTop: 8 }}>
            last error: {worker.last_error}
          </div>
        )}
      </div>
    </section>
  );
}

const Stat = ({ label, value }) => (
  <div>
    <div style={{ fontSize: 18, fontWeight: 700, color: C.ink }}>{value}</div>
    <div style={{ fontSize: 11, color: C.sub, textTransform: "uppercase", letterSpacing: 0.4 }}>{label}</div>
  </div>
);

const s = {
  wrap: { padding: 24, maxWidth: 1240, margin: "0 auto" },
  center: { padding: 48, color: C.ink },
  head: { display: "flex", justifyContent: "space-between", alignItems: "flex-end", marginBottom: 22, gap: 16, flexWrap: "wrap" },
  h1: { fontSize: 22, fontWeight: 700, color: C.ink },
  sub: { fontSize: 13, color: C.sub, marginTop: 2 },
  auto: { fontSize: 12, color: C.sub, display: "flex", alignItems: "center", gap: 5 },
  blocker: { display: "flex", gap: 10, alignItems: "flex-start", border: "1px solid", borderRadius: 12, padding: "12px 14px", marginBottom: 8 },
  owner: { fontSize: 11, fontWeight: 600, color: C.sub, marginLeft: 8 },
  tag: { fontSize: 10, fontWeight: 700, color: C.sub, background: "#fff", border: `1px solid ${C.border}`, borderRadius: 999, padding: "1px 6px", marginLeft: 6 },
  groupTitle: { fontSize: 14, fontWeight: 700, color: C.ink, marginBottom: 8 },
  hint: { fontSize: 12, fontWeight: 400, color: C.sub, marginLeft: 8 },
  cards: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 10 },
  card: { background: "#fff", border: `1px solid ${C.border}`, borderRadius: 12, padding: 14 },
  pill: { fontSize: 11, fontWeight: 700, border: "1px solid", borderRadius: 999, padding: "2px 8px" },
  status: { fontSize: 11, fontWeight: 700, border: "1px solid", borderRadius: 999, padding: "2px 8px" },
  code: { fontSize: 12, background: C.bg, borderRadius: 5, padding: "1px 5px", color: C.ink },
  codeDim: { fontSize: 12, background: C.bg, borderRadius: 5, padding: "1px 5px", color: C.sub },
  empty: { fontSize: 13, color: C.sub, background: "#fff", border: `1px dashed ${C.border}`, borderRadius: 12, padding: "14px 16px" },
  table: { width: "100%", borderCollapse: "collapse", background: "#fff", borderRadius: 12, overflow: "hidden", boxShadow: "0 1px 8px rgba(0,0,0,0.05)" },
  th: { textAlign: "left", fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: C.sub, padding: "10px 14px", borderBottom: `1px solid ${C.border}`, background: C.bg },
  tr: { borderBottom: `1px solid ${C.border}` },
  td: { padding: "10px 14px", fontSize: 13, color: C.ink, verticalAlign: "middle" },
};
