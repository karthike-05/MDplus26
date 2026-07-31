import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import ReviewUI from "./ReviewUI.jsx";
import Dashboard from "./Dashboard.jsx";
import Services from "./Services.jsx";
import ChooseService from "./ChooseService.jsx";
import Initiate from "./Initiate.jsx";
import Integration from "./Integration.jsx";
import ReferralDetail from "./ReferralDetail.jsx";
import { api } from "./api.js";
import { C } from "./ui.jsx";

// ?referral=ref_1001 deep-links to that referral's detail (keeps demo links working).
const DEEP = new URLSearchParams(location.search).get("referral");

function App() {
  // view: {name: 'dashboard'|'services'|'initiate'|'detail'|'review', ...params}
  const [view, setView] = useState(DEEP ? { name: "detail", id: DEEP } : { name: "dashboard" });

  const nav = [
    ["dashboard", "Dashboard"],
    ["services", "Services"],
    ["initiate", "New referral"],
    ["integration", "Integration"],
  ];

  return (
    <div style={{ fontFamily: "system-ui, sans-serif", minHeight: "100vh", background: C.bg }}>
      <header style={h.bar}>
        <span style={h.brand}>Catalyst · <span style={{ color: C.sub, fontWeight: 500 }}>Relay</span></span>
        <nav style={{ display: "flex", gap: 4 }}>
          {nav.map(([key, label]) => (
            <button
              key={key}
              onClick={() => setView({ name: key })}
              style={{ ...h.tab, ...(view.name === key ? h.tabActive : {}) }}
            >
              {label}
            </button>
          ))}
        </nav>
      </header>

      {view.name === "dashboard" && (
        <Dashboard
          onReview={(id) => setView({ name: "review", id })}
          onOpen={(id) => setView({ name: "detail", id })}
          onChoose={(id) => setView({ name: "choose", id })}
          onNew={() => setView({ name: "initiate" })}
        />
      )}
      {view.name === "choose" && (
        <ChooseService
          referralId={view.id}
          onBack={() => setView({ name: "dashboard" })}
          onChosen={() => setView({ name: "dashboard" })}
        />
      )}
      {view.name === "integration" && <Integration />}
      {view.name === "services" && <Services onStart={(svc) => setView({ name: "initiate", serviceId: svc.id })} />}
      {view.name === "initiate" && (
        <Initiate
          onDone={(id) => setView({ name: "choose", id })}
          onCancel={() => setView({ name: "dashboard" })}
        />
      )}
      {view.name === "detail" && (
        <ReferralDetail
          referralId={view.id}
          onBack={() => setView({ name: "dashboard" })}
          onReview={(id) => setView({ name: "review", id })}
        />
      )}
      {view.name === "review" && (
        <ReviewLoader id={view.id} onBack={() => setView({ name: "detail", id: view.id })} />
      )}
    </div>
  );
}

function ReviewLoader({ id, onBack }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.review(id).then(setData).catch((e) => setError(String(e)));
  }, [id]);

  if (error) return <div style={{ padding: 24 }}>Couldn’t load review for {id}: {error}</div>;
  if (!data) return <div style={{ padding: 24 }}>Loading…</div>;

  return (
    <ReviewUI
      schema={data.schema}
      review={data.review}
      pageSize={data.pageSize}
      pageImageUrl={(p) => api.pageImageUrl(data.schema.form_id, p)}
      onSubmit={async (vals) => {
        try {
          const d = await api.submit(id, vals);
          const o = d.outcome || {};
          if (o.status === "success") {
            // The new state is on the response, not the outcome — fill_form never
            // mutates current_state (§7); the route advances it after submit.
            alert(`Submitted ✅  filled ${o.data?.filled_fields?.length ?? 0} fields → ${d.state}`);
            onBack();
          } else {
            alert(`Not submitted (${o.status}). Check: ${Object.keys(o.data?.problems || {}).join(", ")}`);
          }
        } catch (e) {
          alert(String(e));
        }
      }}
    />
  );
}

const h = {
  bar: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "12px 20px", background: "#fff", borderBottom: `1px solid ${C.border}`, position: "sticky", top: 0, zIndex: 10 },
  brand: { fontSize: 16, fontWeight: 700, color: C.ink },
  tab: { border: "none", background: "transparent", padding: "8px 14px", borderRadius: 8, fontSize: 14, fontWeight: 600, color: C.sub, cursor: "pointer" },
  tabActive: { background: C.bg, color: C.accent },
};

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App />
  </StrictMode>
);
