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
const API =
  import.meta.env.VITE_API_BASE ?? (import.meta.env.PROD ? "" : "http://localhost:8000");

async function j(path, opts) {
  const r = await fetch(API + path, opts);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
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
  pageImageUrl: (formId, page) => `${API}/api/form/${formId}/page/${page}.png`,
};
