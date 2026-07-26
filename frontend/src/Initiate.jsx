/**
 * Initiate — start a referral (§12 "pick patient" beat, extended).
 *
 *   1. Find the patient by name + DOB (auto-populates on match, else create — synthetic).
 *   2. Pick the service; its preferred contact mode prefills and the SW can override.
 *   3. Create the referral (starts at `created`) and hand off to its detail view.
 *
 * The referral only gets *produced* here; the warm path (map/validate/review/inject)
 * and the scheduler take over from the dashboard. No warm-path code changes.
 */

import { useEffect, useState } from "react";
import { api } from "./api.js";
import { C, Btn, CHANNEL_LABEL } from "./ui.jsx";

const clean = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== "" && v != null));

export default function Initiate({ preselectedServiceId, onDone, onCancel }) {
  const [name, setName] = useState("");
  const [dob, setDob] = useState("");
  const [patient, setPatient] = useState(null);
  const [draft, setDraft] = useState(null); // create-form when no match
  const [services, setServices] = useState([]);
  const [ref, setRef] = useState({
    service_id: preselectedServiceId || "",
    outreach_channel: "",
    referring_clinic: "",
    appointment_date: "",
    appointment_time: "", // left blank -> preserves the "check this" review beat
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.services().then((d) => {
      setServices(d.services);
      const pick = d.services.find((x) => x.id === (preselectedServiceId || d.services[0]?.id));
      if (pick) setRef((r) => ({ ...r, service_id: pick.id, outreach_channel: pick.preferred_channel }));
    });
  }, [preselectedServiceId]);

  const svc = services.find((x) => x.id === ref.service_id);

  const call = async (fn) => {
    setBusy(true);
    setError(null);
    try { return await fn(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  const search = () =>
    call(async () => {
      const d = await api.findPatient(name, dob);
      if (d.found) { setPatient(d.patient); setDraft(null); }
      else setDraft({ name, dob, phone: "", referring_clinic: "", address: "", medicaid_id: "" });
    });

  const createPatient = () =>
    call(async () => {
      const d = await api.createPatient(clean(draft));
      setPatient(d.patient);
      setDraft(null);
    });

  const create = () =>
    call(async () => {
      const d = await api.createReferral({ patient_id: patient.id, ...clean(ref) });
      onDone?.(d.referral_id);
    });

  const onServiceChange = (id) => {
    const picked = services.find((x) => x.id === id);
    setRef((r) => ({ ...r, service_id: id, outreach_channel: picked?.preferred_channel || r.outreach_channel }));
  };

  return (
    <div style={s.wrap}>
      <div style={s.card}>
        <div style={s.top}>
          <div style={s.h1}>Start a referral</div>
          <Btn tone="ghost" small onClick={onCancel}>Cancel</Btn>
        </div>

        {/* 1 — patient */}
        <div style={s.step}>1 · Patient</div>
        <Row>
          <Field label="Name"><input style={s.input} value={name} onChange={(e) => setName(e.target.value)} placeholder="Maria Gonzalez" disabled={!!patient} /></Field>
          <Field label="Date of birth"><input style={s.input} value={dob} onChange={(e) => setDob(e.target.value)} placeholder="1958-03-12" disabled={!!patient} /></Field>
        </Row>

        {!patient && !draft && <Btn disabled={busy || !name || !dob} onClick={search}>{busy ? "Searching…" : "Find patient"}</Btn>}

        {draft && (
          <>
            <Note tone="warn">No match — add this patient (synthetic data only).</Note>
            <Row>
              <Field label="Phone"><input style={s.input} value={draft.phone} onChange={(e) => setDraft({ ...draft, phone: e.target.value })} /></Field>
              <Field label="Medicaid ID"><input style={s.input} value={draft.medicaid_id} onChange={(e) => setDraft({ ...draft, medicaid_id: e.target.value })} /></Field>
            </Row>
            <Field label="Address"><input style={s.input} value={draft.address} onChange={(e) => setDraft({ ...draft, address: e.target.value })} /></Field>
            {/* Phone + referring clinic are NOT NULL on the shared patients table, so
                the button stays disabled until both are filled — a rejected insert
                here would surface as an opaque 500. */}
            <Field label="Referring clinic"><input style={s.input} value={draft.referring_clinic} onChange={(e) => setDraft({ ...draft, referring_clinic: e.target.value })} /></Field>
            <Btn disabled={busy || !draft.phone.trim() || !draft.referring_clinic.trim()} onClick={createPatient}>{busy ? "Saving…" : "Create patient"}</Btn>
          </>
        )}

        {patient && (
          <>
            <Note tone="ok">{patient.name} · {patient.dob} · {patient.phone || "no phone"}</Note>

            {/* 2 — service + mode */}
            <div style={s.step}>2 · Service</div>
            <Row>
              <Field label="Service">
                <select style={s.input} value={ref.service_id} onChange={(e) => onServiceChange(e.target.value)}>
                  {services.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
                </select>
              </Field>
              <Field label="Contact mode (override)">
                <select style={s.input} value={ref.outreach_channel} onChange={(e) => setRef({ ...ref, outreach_channel: e.target.value })}>
                  {["form", "phone", "text", "email"].map((m) => <option key={m} value={m}>{CHANNEL_LABEL[m]}</option>)}
                </select>
              </Field>
            </Row>
            {svc && <div style={s.hint}>Service prefers {CHANNEL_LABEL[svc.preferred_channel]} · {svc.phone}</div>}

            <div style={s.step}>3 · Details</div>
            <Row>
              <Field label="Referring clinic"><input style={s.input} value={ref.referring_clinic} onChange={(e) => setRef({ ...ref, referring_clinic: e.target.value })} /></Field>
              <Field label="Appointment date"><input style={s.input} value={ref.appointment_date} onChange={(e) => setRef({ ...ref, appointment_date: e.target.value })} placeholder="08/05/2026" /></Field>
            </Row>

            <Btn disabled={busy || !ref.service_id} onClick={create}>{busy ? "Creating…" : "Create referral →"}</Btn>
          </>
        )}

        {error && <Note tone="warn">{error}</Note>}
      </div>
    </div>
  );
}

const Row = ({ children }) => <div style={s.row}>{children}</div>;
const Field = ({ label, children }) => (
  <label style={s.field}><span style={s.label}>{label}</span>{children}</label>
);
const Note = ({ tone, children }) => (
  <div style={{ ...s.note, color: tone === "ok" ? C.ok : C.warn, background: tone === "ok" ? "rgba(47,133,90,0.08)" : "rgba(192,86,33,0.08)" }}>{children}</div>
);

const s = {
  wrap: { padding: "40px 16px", display: "flex", justifyContent: "center" },
  card: { width: "100%", maxWidth: 620, background: "#fff", border: `1px solid ${C.border}`, borderRadius: 14, padding: 28, boxShadow: "0 2px 16px rgba(0,0,0,0.06)" },
  top: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 },
  h1: { fontSize: 20, fontWeight: 700, color: C.ink },
  step: { fontSize: 12, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: C.sub, margin: "18px 0 10px" },
  row: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 },
  field: { display: "flex", flexDirection: "column", gap: 6, marginBottom: 12 },
  label: { fontSize: 12, fontWeight: 600, color: C.sub, textTransform: "uppercase", letterSpacing: 0.3 },
  input: { boxSizing: "border-box", padding: "9px 11px", fontSize: 14, border: `1px solid ${C.border}`, borderRadius: 8, outline: "none", width: "100%", background: "#fff" },
  hint: { fontSize: 12, color: C.sub, marginTop: -4, marginBottom: 4 },
  note: { marginTop: 8, marginBottom: 8, fontSize: 13, fontWeight: 500, padding: "9px 12px", borderRadius: 8 },
};
