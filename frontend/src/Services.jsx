/**
 * Services — the toy directory. Discovery isn't our differentiator (an incumbent
 * like findhelp provides this data); it's a hard-coded handful so a social worker can
 * pick a service and kick off a referral. Each card shows the service's preferred
 * contact mode, which prefills the referral's outreach method.
 */

import { useEffect, useState } from "react";
import { api } from "./api.js";
import { C, Btn, CHANNEL_LABEL } from "./ui.jsx";

export default function Services({ onStart }) {
  const [services, setServices] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => { api.services().then((d) => setServices(d.services)).catch((e) => setError(String(e))); }, []);

  if (error) return <div style={s.center}>Couldn’t load services: {error}</div>;
  if (!services) return <div style={s.center}>Loading…</div>;

  const byCat = {};
  for (const svc of services) (byCat[svc.category] ||= []).push(svc);

  return (
    <div style={s.wrap}>
      <div style={s.h1}>Services directory</div>
      <div style={s.sub}>Toy catalog · in production this comes from a partner integration</div>

      {Object.entries(byCat).map(([cat, list]) => (
        <div key={cat} style={{ marginTop: 22 }}>
          <div style={s.cat}>{cat}</div>
          <div style={s.grid}>
            {list.map((svc) => (
              <div key={svc.id} style={s.card}>
                <div style={s.cardHead}>
                  <span style={s.name}>{svc.name}</span>
                  <span style={s.mode}>{CHANNEL_LABEL[svc.preferred_channel] || svc.preferred_channel}</span>
                </div>
                <div style={s.desc}>{svc.description}</div>
                <div style={s.meta}>
                  {svc.phone && <div>📞 {svc.phone}</div>}
                  {svc.email && <div>✉️ {svc.email}</div>}
                  {svc.website && <div style={s.link}>🔗 {svc.website}</div>}
                </div>
                <div style={{ marginTop: 12 }}>
                  <Btn small onClick={() => onStart(svc)}>Start referral →</Btn>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

const s = {
  wrap: { padding: 24, maxWidth: 1100, margin: "0 auto" },
  center: { padding: 48, fontFamily: "system-ui", color: C.ink },
  h1: { fontSize: 22, fontWeight: 700, color: C.ink },
  sub: { fontSize: 13, color: C.sub, marginTop: 2 },
  cat: { fontSize: 12, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: C.sub, marginBottom: 10 },
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 14 },
  card: { background: "#fff", border: `1px solid ${C.border}`, borderRadius: 12, padding: 16, boxShadow: "0 1px 6px rgba(0,0,0,0.04)" },
  cardHead: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 },
  name: { fontSize: 15, fontWeight: 600, color: C.ink },
  mode: { fontSize: 12, color: C.accent, fontWeight: 600, whiteSpace: "nowrap" },
  desc: { fontSize: 13, color: C.sub, marginTop: 6, lineHeight: 1.4 },
  meta: { fontSize: 12, color: C.ink, marginTop: 10, display: "flex", flexDirection: "column", gap: 3 },
  link: { color: C.accent, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
};
