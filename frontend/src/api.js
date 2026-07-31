// Thin API client for the backend (backend/main.py). One place for all fetches.

// Vite inlines import.meta.env.VITE_* at BUILD time, so a deployed bundle needs
// VITE_API_BASE set in the build environment — setting it at runtime does nothing.
//
// The default is chosen per mode rather than hardcoded, because the two situations want
// opposite things:
//   - `npm run dev` (:5173) needs an absolute URL to reach the backend on :8000.
//   - a PRODUCTION build is served BY that backend (see the StaticFiles mount at the
//     bottom of backend/main.py), so "" makes every call same-origin and relative. That
//     is what lets the deployed URL change — a new tunnel, a new Railway domain —
//     without rebuilding the bundle.
const API = (
  import.meta.env.VITE_API_BASE ?? (import.meta.env.PROD ? "" : "http://localhost:8000")
).replace(/\/$/, "");

async function j(path, opts) {
  const r = await fetch(API + path, opts);
  if (!r.ok) {
    // FastAPI's HTTPException bodies are {"detail": "..."} — surface that message
    // (e.g. RankingUnavailable's clean text) instead of a generic "422 Unprocessable
    // Entity" that tells the SW nothing. Falls back to status text if the body isn't
    // JSON or has no detail field.
    let detail;
    try {
      detail = (await r.json())?.detail;
    } catch {
      // not JSON — fall through to the generic message below
    }
    throw new Error(detail || `${r.status} ${r.statusText}`);
  }
  return r.json();
}
const post = (path, body) =>
  j(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

export const api = {
  dashboard: () => j("/api/dashboard"),
  system: () => j("/api/system"),
  dbMode: () => j("/api/db"),
  setDbMode: (mode) => post("/api/db", { mode }),
  services: () => j("/api/services"),
  referral: (id) => j(`/api/referrals/${id}`),
  review: (id) => j(`/api/review/${id}`),
  run: (id) => post(`/api/referrals/${id}/run`),
  inbound: (id, signal) => post(`/api/referrals/${id}/inbound`, { signal }),
  findPatient: (name, dob) =>
    j(`/api/patients/find?name=${encodeURIComponent(name)}&dob=${encodeURIComponent(dob)}`),
  createPatient: (p) => post("/api/patients", p),
  createReferral: (r) => post("/api/referrals", r),
  submit: (id, values) => post(`/api/submit/${id}`, { values }),
  // The SW selection gate: run ranking, read the ranked shortlist, then record the
  // human's pick.
  rankReferral: (id) => post(`/api/referrals/${id}/rank`),
  ranking: (id) => j(`/api/referrals/${id}/ranking`),
  chooseService: (id, body) => post(`/api/referrals/${id}/choose-service`, body),
  // MILESTONE 1 — the ORG's answer, distinct from the patient having used the service.
  // Works live (writes the `enrolled` attempt advance_referral reads) and offline. The
  // org-email webhook will post to this same endpoint once ORG_BACKEND_URL points here.
  orgResponse: (id, decision, extra = {}) =>
    post("/api/org/response", { referral_id: id, decision, ...extra }),
  pageImageUrl: (formId, page) => `${API}/api/form/${formId}/page/${page}.png`,
};
