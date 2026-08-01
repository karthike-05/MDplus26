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

import { useState } from "react";
import { api } from "./api.js";
import { C, Btn } from "./ui.jsx";

const clean = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== "" && v != null));

// `mobility_needs` is free text on `patients` (no CHECK constraint) — this is a menu of
// the accessibility needs call_agent/transportation callers actually ask about, plus a
// write-in so the SW is never blocked by a need that isn't listed (§ patients columns).
const ACCESSIBILITY_OPTIONS = [
  { value: "", label: "None specified" },
  { value: "Wheelchair accessible vehicle required", label: "Wheelchair accessible vehicle" },
  { value: "Uses a mobility aid (cane, walker, or crutches)", label: "Mobility aid (cane, walker, crutches)" },
  { value: "Non-ambulatory — stretcher transport required", label: "Non-ambulatory / stretcher transport" },
  { value: "Blind or low vision", label: "Blind or low vision" },
  { value: "Deaf or hard of hearing", label: "Deaf or hard of hearing" },
  { value: "Cognitive or developmental disability support", label: "Cognitive/developmental disability support" },
  { value: "Service animal", label: "Service animal" },
  { value: "__other__", label: "Other (describe)" },
];

// Strict YYYY-MM-DD, digits only — the SW-facing intake form is stricter than the
// backend, which also tolerates MM/DD/YYYY (§ _normalize_dob) for other callers.
const DOB_RE = /^\d{4}-\d{2}-\d{2}$/;

// Strict E.164, US-only — +1 followed by exactly 10 digits. The backend's
// _normalize_phone already coerces looser input into this shape, but requiring it
// verbatim here means the SW sees the stored format, not a silent rewrite.
const PHONE_RE = /^\+1\d{10}$/;

// Matches the live `insurance_type` CHECK constraint exactly — any other value fails the
// insert on the real DB (verified via supabase MCP), so this can't drift into free text.
const INSURANCE_OPTIONS = [
  { value: "kancare_ffs", label: "KanCare (Fee-for-Service)" },
  { value: "kancare_sunflower", label: "KanCare — Sunflower Health Plan" },
  { value: "kancare_unitedhealthcare", label: "KanCare — UnitedHealthcare" },
  { value: "mo_healthnet", label: "MO HealthNet (Missouri Medicaid)" },
  { value: "healthy_blue", label: "Healthy Blue" },
  { value: "medicare", label: "Medicare" },
  { value: "uninsured", label: "Uninsured" },
  { value: "private", label: "Private insurance" },
  { value: "other", label: "Other" },
];

// Matches the live `preferred_contact_method` CHECK constraint.
const CONTACT_METHOD_OPTIONS = [
  { value: "sms", label: "Text (SMS)" },
  { value: "voice", label: "Phone call" },
  { value: "either", label: "Either" },
];

// The next four are free text on `patients` (no CHECK constraint) — same menu +
// write-in shape as ACCESSIBILITY_OPTIONS, just common values for each column.
const EDUCATION_OPTIONS = [
  { value: "", label: "Not specified" },
  { value: "Less than high school", label: "Less than high school" },
  { value: "High school diploma or GED", label: "High school diploma / GED" },
  { value: "Some college", label: "Some college" },
  { value: "Associate degree", label: "Associate degree" },
  { value: "Bachelor's degree", label: "Bachelor's degree" },
  { value: "Graduate or professional degree", label: "Graduate or professional degree" },
  { value: "__other__", label: "Other (describe)" },
];

const EMPLOYMENT_OPTIONS = [
  { value: "", label: "Not specified" },
  { value: "Employed full-time", label: "Employed full-time" },
  { value: "Employed part-time", label: "Employed part-time" },
  { value: "Unemployed", label: "Unemployed" },
  { value: "Retired", label: "Retired" },
  { value: "Unable to work / disabled", label: "Unable to work / disabled" },
  { value: "Student", label: "Student" },
  { value: "__other__", label: "Other (describe)" },
];

const MARITAL_OPTIONS = [
  { value: "", label: "Not specified" },
  { value: "Single", label: "Single" },
  { value: "Married", label: "Married" },
  { value: "Divorced", label: "Divorced" },
  { value: "Widowed", label: "Widowed" },
  { value: "Separated", label: "Separated" },
  { value: "__other__", label: "Other (describe)" },
];

const INCOME_OPTIONS = [
  { value: "", label: "Not specified" },
  { value: "Below poverty line", label: "Below poverty line" },
  { value: "Low income", label: "Low income" },
  { value: "Middle income", label: "Middle income" },
  { value: "Above average income", label: "Above average income" },
  { value: "Prefers not to say", label: "Prefers not to say" },
  { value: "__other__", label: "Other (describe)" },
];

// Only Transportation is wired end-to-end (it writes a `service_requests` row —
// backend/main.py's create_referral, § patients columns follow-up). Food, housing,
// etc. get added here once their own flows exist.
const CATEGORY_OPTIONS = [
  { value: "transportation", label: "Transportation" },
];

export default function Initiate({ onDone, onCancel }) {
  const [name, setName] = useState("");
  const [dob, setDob] = useState("");
  const [patient, setPatient] = useState(null);
  const [draft, setDraft] = useState(null); // create-form when no match
  const [ref, setRef] = useState({
    category: "transportation", // the only category wired up so far
    appointment_date: "",
    appointment_time: "", // left blank -> preserves the "check this" review beat
    requested_end_time: "",
    pickup_address: "",
    destination_address: "",
    pickup_notes: "",
    destination_notes: "",
    companion_required: undefined,
    interpreter_required: undefined,
    contact_email: "",
    emergency_contact: "",
    special_instructions: "",
    request_notes: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // Local UI state for the five menu+"Other" fields — NOT sent to the backend directly;
  // each just decides whether that field's write-in box shows. The value that's
  // actually submitted always lives on the matching draft.* key (§ patients columns).
  const [selects, setSelects] = useState({
    mobility_needs: "", education_level: "", employment_status: "",
    marital_status: "", income_status: "",
  });
  const setSelect = (key, v) => setSelects((prev) => ({ ...prev, [key]: v }));
  // Tri-state on purpose: undefined = never attempted, null = attempted and missed,
  // object = resolved. A plain null start made the "couldn't resolve that address"
  // warning fire for every EXISTING patient found via Find patient, where no geocode was
  // ever run.
  const [located, setLocated] = useState(undefined);

  const dobValid = DOB_RE.test(dob.trim());
  const phoneValid = PHONE_RE.test((draft?.phone || "").trim());

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
      setLocated(undefined);                 // a new search hasn't geocoded anything yet
      setSelects({ mobility_needs: "", education_level: "", employment_status: "", marital_status: "", income_status: "" });
      if (d.found) { setPatient(d.patient); setDraft(null); }
      else setDraft({
        name, dob, phone: "", referring_clinic: "", address: "", medicaid_id: "",
        household_size: "", preferred_language: "", education_level: "",
        employment_status: "", marital_status: "", income_status: "",
        insurance_type: "", preferred_contact_method: "", is_veteran: undefined,
        mobility_needs: "", need_description: "",
      });
    });

  const createPatient = () =>
    call(async () => {
      const d = await api.createPatient(clean(draft));
      setPatient(d.patient);
      // `patients` has no address column — the address is geocoded into postal_code /
      // county / lat-long, which is what Ranking's hard filter reads. If that misses,
      // say so HERE: unresolved, the referral reaches Ranking and dies with a 500 that
      // looks like their bug. See backend/intake/geocode.py.
      setLocated(d.geocoded ? d.location : null);
      setDraft(null);
    });

  // CONFIRM BEFORE A REAL TEXT GOES OUT. Creating a referral live kicks
  // advance_referral -> confirm_consent -> twilio, and Messaging's deployed poller sends
  // a REAL WhatsApp to whatever number is on this form, on the team's account. There was
  // no confirmation step, so one mistyped digit texted a stranger — and the person
  // clicking has no way to know the button does that. Naming the number is the point:
  // a generic "are you sure?" wouldn't catch the typo this exists to catch.
  const create = () => {
    const phone = (patient?.phone || "").trim();
    const ok = window.confirm(
      `Send a WhatsApp opt-in message to ${phone || "this patient"}?\n\n` +
      `${patient?.name || "The patient"} will receive a real text asking them to ` +
      `consent to this referral. Check the number is right — the message goes out ` +
      `immediately and can't be recalled.`
    );
    if (!ok) return;
    return call(async () => {
      const d = await api.createReferral({ patient_id: patient.id, ...clean(ref) });
      onDone?.(d.referral_id);
    });
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
          <Field label="Name" required><input style={s.input} value={name} onChange={(e) => setName(e.target.value)} placeholder="Maria Gonzalez" disabled={!!patient} /></Field>
          <Field label="Date of birth" required><input style={s.input} value={dob} onChange={(e) => setDob(e.target.value)} placeholder="1958-03-12" disabled={!!patient} /></Field>
        </Row>
        {!patient && dob && !dobValid && <div style={{ ...s.hint, color: C.warn }}>Use YYYY-MM-DD (numbers only), e.g. 1958-03-12</div>}

        {!patient && !draft && <Btn disabled={busy || !name.trim() || !dobValid} onClick={search}>{busy ? "Searching…" : "Find patient"}</Btn>}

        {draft && (
          <>
            <Note tone="warn">No match — add this patient (synthetic data only).</Note>
            <Row>
              <Field label="Phone" required>
                <input style={s.input} value={draft.phone} placeholder="+14085551234"
                       onChange={(e) => setDraft({ ...draft, phone: e.target.value })} />
              </Field>
              <Field label="Medicaid ID"><input style={s.input} value={draft.medicaid_id} onChange={(e) => setDraft({ ...draft, medicaid_id: e.target.value })} /></Field>
            </Row>
            {draft.phone && !phoneValid && <div style={{ ...s.hint, color: C.warn }}>Use +1 followed by 10 digits, e.g. +14085551234</div>}
            {/* Required, though `patients` has no address column: it's the input we
                geocode into postal_code / county / lat-long, and those are what
                Ranking's hard filter reads. Left blank, the referral reaches Ranking
                and dies with a 500. */}
            <Field label="Address" required>
              <input style={s.input} value={draft.address} placeholder="6330 Leavenworth Rd, Kansas City, KS 66104"
                     onChange={(e) => setDraft({ ...draft, address: e.target.value })} />
            </Field>
            <div style={s.hint}>Street, city, state — used to locate the patient for service matching.</div>
            {/* Phone + referring clinic are NOT NULL on the shared patients table, so
                the button stays disabled until both are filled — a rejected insert
                here would surface as an opaque 500. */}
            <Field label="Referring clinic" required><input style={s.input} value={draft.referring_clinic} onChange={(e) => setDraft({ ...draft, referring_clinic: e.target.value })} /></Field>

            <div style={s.step}>Additional details</div>
            <Row>
              <Field label="Household size">
                <input style={s.input} type="number" min="1" value={draft.household_size}
                       onChange={(e) => setDraft({ ...draft, household_size: e.target.value })} />
              </Field>
              <Field label="Preferred language">
                <input style={s.input} value={draft.preferred_language} placeholder="en"
                       onChange={(e) => setDraft({ ...draft, preferred_language: e.target.value })} />
              </Field>
            </Row>
            <Row>
              <SelectOther label="Education level" options={EDUCATION_OPTIONS}
                           selectValue={selects.education_level} onSelectChange={(v) => setSelect("education_level", v)}
                           value={draft.education_level} onValueChange={(v) => setDraft({ ...draft, education_level: v })} />
              <SelectOther label="Employment status" options={EMPLOYMENT_OPTIONS}
                           selectValue={selects.employment_status} onSelectChange={(v) => setSelect("employment_status", v)}
                           value={draft.employment_status} onValueChange={(v) => setDraft({ ...draft, employment_status: v })} />
            </Row>
            <Row>
              <SelectOther label="Marital status" options={MARITAL_OPTIONS}
                           selectValue={selects.marital_status} onSelectChange={(v) => setSelect("marital_status", v)}
                           value={draft.marital_status} onValueChange={(v) => setDraft({ ...draft, marital_status: v })} />
              <SelectOther label="Income status" options={INCOME_OPTIONS}
                           selectValue={selects.income_status} onSelectChange={(v) => setSelect("income_status", v)}
                           value={draft.income_status} onValueChange={(v) => setDraft({ ...draft, income_status: v })} />
            </Row>
            <Row>
              <Field label="Insurance type" required>
                <select style={s.input} value={draft.insurance_type}
                        onChange={(e) => setDraft({ ...draft, insurance_type: e.target.value })}>
                  <option value="" disabled>Select insurance type…</option>
                  {INSURANCE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </Field>
              <Field label="Preferred contact method" required>
                <select style={s.input} value={draft.preferred_contact_method}
                        onChange={(e) => setDraft({ ...draft, preferred_contact_method: e.target.value })}>
                  <option value="" disabled>Select a method…</option>
                  {CONTACT_METHOD_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </Field>
            </Row>
            <Row>
              <Field label="Veteran">
                <select style={s.input} value={draft.is_veteran === undefined ? "" : String(draft.is_veteran)}
                        onChange={(e) => {
                          const v = e.target.value;
                          setDraft({ ...draft, is_veteran: v === "" ? undefined : v === "true" });
                        }}>
                  <option value="">Not specified</option>
                  <option value="true">Yes</option>
                  <option value="false">No</option>
                </select>
              </Field>
              <SelectOther label="Accessibility needs" options={ACCESSIBILITY_OPTIONS} placeholder="Describe the accessibility need"
                           selectValue={selects.mobility_needs} onSelectChange={(v) => setSelect("mobility_needs", v)}
                           value={draft.mobility_needs} onValueChange={(v) => setDraft({ ...draft, mobility_needs: v })} />
            </Row>
            <Field label="Describe patient needs" required>
              <textarea style={s.textarea} rows={3} placeholder="What is this patient trying to get help with?"
                        value={draft.need_description}
                        onChange={(e) => setDraft({ ...draft, need_description: e.target.value })} />
            </Field>

            <Btn disabled={busy || !phoneValid || !draft.referring_clinic.trim() || !draft.address.trim()
                           || !draft.insurance_type || !draft.preferred_contact_method || !draft.need_description.trim()}
                 onClick={createPatient}>{busy ? "Saving…" : "Create patient"}</Btn>
          </>
        )}

        {patient && (
          <>
            <Note tone="ok">{patient.name} · {patient.dob} · {patient.phone || "no phone"}</Note>
            {located && (
              <Note tone="ok">
                Located: {[located.county, located.postal_code].filter(Boolean).join(" · ")}
                {" "}({Number(located.latitude).toFixed(4)}, {Number(located.longitude).toFixed(4)})
              </Note>
            )}
            {located === null && (
              <Note tone="warn">
                Couldn’t resolve that address to coordinates. The referral will still be
                created, but service ranking needs a location — expect it to stall.
              </Note>
            )}

            {/* 2 — service (the only section now: category drives which trip-detail
                boxes below it appear). Which service org actually handles this referral
                is picked on the next screen, after ranking — not here. */}
            <div style={s.step}>2 · Service</div>
            <Field label="Category" required>
              <select style={s.input} value={ref.category} onChange={(e) => setRef({ ...ref, category: e.target.value })}>
                {CATEGORY_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </Field>

            {/* Trip payload for `service_requests` (§6a) — Voice and fill_form both
                read this row, so it's created alongside the referral (backend/main.py). */}
            {ref.category === "transportation" && (
              <>
                <div style={s.step}>Transportation details</div>
                <Row>
                  <Field label="Pickup location" required>
                    <input style={s.input} value={ref.pickup_address} placeholder="6330 Leavenworth Rd, Kansas City, KS 66104"
                           onChange={(e) => setRef({ ...ref, pickup_address: e.target.value })} />
                  </Field>
                  <Field label="Dropoff location" required>
                    <input style={s.input} value={ref.destination_address} placeholder="Clinic or service address"
                           onChange={(e) => setRef({ ...ref, destination_address: e.target.value })} />
                  </Field>
                </Row>
                <Row>
                  <Field label="Pickup date" required>
                    <input style={s.input} value={ref.appointment_date} placeholder="2026-08-05"
                           onChange={(e) => setRef({ ...ref, appointment_date: e.target.value })} />
                  </Field>
                  <Field label="Pickup time">
                    <input style={s.input} value={ref.appointment_time} placeholder="14:30"
                           onChange={(e) => setRef({ ...ref, appointment_time: e.target.value })} />
                  </Field>
                </Row>
                <Row>
                  <Field label="Return / latest time">
                    <input style={s.input} value={ref.requested_end_time} placeholder="16:00"
                           onChange={(e) => setRef({ ...ref, requested_end_time: e.target.value })} />
                  </Field>
                  <Field label="Contact email">
                    <input style={s.input} value={ref.contact_email}
                           onChange={(e) => setRef({ ...ref, contact_email: e.target.value })} />
                  </Field>
                </Row>
                <Row>
                  <Field label="Pickup notes">
                    <input style={s.input} value={ref.pickup_notes} placeholder="e.g. use back entrance, ring buzzer 4"
                           onChange={(e) => setRef({ ...ref, pickup_notes: e.target.value })} />
                  </Field>
                  <Field label="Dropoff notes">
                    <input style={s.input} value={ref.destination_notes}
                           onChange={(e) => setRef({ ...ref, destination_notes: e.target.value })} />
                  </Field>
                </Row>
                <Row>
                  <Field label="Companion required">
                    <select style={s.input} value={ref.companion_required === undefined ? "" : String(ref.companion_required)}
                            onChange={(e) => {
                              const v = e.target.value;
                              setRef({ ...ref, companion_required: v === "" ? undefined : v === "true" });
                            }}>
                      <option value="">Not specified</option>
                      <option value="true">Yes</option>
                      <option value="false">No</option>
                    </select>
                  </Field>
                  <Field label="Interpreter required">
                    <select style={s.input} value={ref.interpreter_required === undefined ? "" : String(ref.interpreter_required)}
                            onChange={(e) => {
                              const v = e.target.value;
                              setRef({ ...ref, interpreter_required: v === "" ? undefined : v === "true" });
                            }}>
                      <option value="">Not specified</option>
                      <option value="true">Yes</option>
                      <option value="false">No</option>
                    </select>
                  </Field>
                </Row>
                <Field label="Emergency contact">
                  <input style={s.input} value={ref.emergency_contact} placeholder="Name and phone number"
                         onChange={(e) => setRef({ ...ref, emergency_contact: e.target.value })} />
                </Field>
                <Field label="Special instructions">
                  <textarea style={s.textarea} rows={2} value={ref.special_instructions}
                            onChange={(e) => setRef({ ...ref, special_instructions: e.target.value })} />
                </Field>
                <Field label="Request notes">
                  <textarea style={s.textarea} rows={2} value={ref.request_notes}
                            onChange={(e) => setRef({ ...ref, request_notes: e.target.value })} />
                </Field>
              </>
            )}

            <Btn disabled={busy || !ref.category
                           || (ref.category === "transportation" && (!ref.pickup_address.trim()
                               || !ref.destination_address.trim() || !ref.appointment_date.trim()))}
                 onClick={create}>{busy ? "Creating…" : "Create referral →"}</Btn>
          </>
        )}

        {error && <Note tone="warn">{error}</Note>}
      </div>
    </div>
  );
}

const Row = ({ children }) => <div style={s.row}>{children}</div>;
const Field = ({ label, children, required }) => (
  <label style={s.field}>
    <span style={s.label}>{label}{required && <span style={s.required}> *</span>}</span>
    {children}
  </label>
);
const Note = ({ tone, children }) => (
  <div style={{ ...s.note, color: tone === "ok" ? C.ok : C.warn, background: tone === "ok" ? "rgba(47,133,90,0.08)" : "rgba(192,86,33,0.08)" }}>{children}</div>
);

// A dropdown menu of common values + a write-in "Other" option. `selectValue` mirrors
// the <select>'s own choice (including "__other__"); `value` is the actual free-text
// column value that gets submitted — the two diverge only while "Other" is selected.
const SelectOther = ({ label, required, options, selectValue, onSelectChange, value, onValueChange, placeholder }) => (
  <Field label={label} required={required}>
    <select style={s.input} value={selectValue}
            onChange={(e) => {
              const v = e.target.value;
              onSelectChange(v);
              onValueChange(v === "__other__" ? "" : v);
            }}>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
    {selectValue === "__other__" && (
      <input style={{ ...s.input, marginTop: 8 }} placeholder={placeholder || "Please describe"}
             value={value} onChange={(e) => onValueChange(e.target.value)} />
    )}
  </Field>
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
  required: { color: C.danger, fontWeight: 700 },
  input: { boxSizing: "border-box", padding: "9px 11px", fontSize: 14, border: `1px solid ${C.border}`, borderRadius: 8, outline: "none", width: "100%", background: "#fff" },
  textarea: { boxSizing: "border-box", padding: "9px 11px", fontSize: 14, fontFamily: "inherit", border: `1px solid ${C.border}`, borderRadius: 8, outline: "none", width: "100%", background: "#fff", resize: "vertical" },
  hint: { fontSize: 12, color: C.sub, marginTop: -4, marginBottom: 4 },
  note: { marginTop: 8, marginBottom: 8, fontSize: 13, fontWeight: 500, padding: "9px 12px", borderRadius: 8 },
};
