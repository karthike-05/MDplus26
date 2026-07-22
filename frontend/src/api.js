// Thin API client for the backend (backend/main.py). One place for all fetches.

const API = "http://localhost:8000";

async function j(path, opts) {
  const r = await fetch(API + path, opts);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}
const post = (path, body) =>
  j(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

export const api = {
  base: API,
  dashboard: () => j("/api/dashboard"),
  services: () => j("/api/services"),
  service: (id) => j(`/api/services/${id}`),
  referral: (id) => j(`/api/referrals/${id}`),
  review: (id) => j(`/api/review/${id}`),
  forms: () => j("/api/forms"),
  run: (id) => post(`/api/referrals/${id}/run`),
  inbound: (id, signal) => post(`/api/referrals/${id}/inbound`, { signal }),
  findPatient: (name, dob) =>
    j(`/api/patients/find?name=${encodeURIComponent(name)}&dob=${encodeURIComponent(dob)}`),
  createPatient: (p) => post("/api/patients", p),
  createReferral: (r) => post("/api/referrals", r),
  submit: (id, values) => post(`/api/submit/${id}`, { values }),
  pageImageUrl: (formId, page) => `${API}/api/form/${formId}/page/${page}.png`,
};
