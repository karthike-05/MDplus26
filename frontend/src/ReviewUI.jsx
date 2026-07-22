/**
 * ReviewUI — the per-patient form-review screen (CLAUDE.md §6, §5d).
 *
 * Split screen: the extracted FIELDS on the left, the PDF FORM on the right.
 * Click a field on the left -> the corresponding region of the PDF is boxed/
 * highlighted on the right (and vice-versa). The reviewer confirms or edits each
 * value, then submits.
 *
 * Data contract (identical JSON to the Python side):
 *   - `review`  : ReviewPayload  { referral_id, form_id, values, needs_attention,
 *                                  pending_human, provenance }   (fill_form.prepare)
 *   - `schema`  : FormSchema     { form_id, target_type:"pdf", fields:[{ name,
 *                                  fill_policy, page, rect:[x0,y0,x1,y1], ... }] }
 *   - `pageImageUrl(page)` : URL of the rendered PDF page PNG (backend renders it
 *                            with PyMuPDF `page.get_pixmap`, CLAUDE.md §9).
 *   - `pageSize`: { width, height } of the PDF page in POINTS (fitz units), so
 *                 rects map to overlay boxes as percentages -> fully responsive.
 *
 * IMPORTANT (§5c): render EVERY field, including `human_only` (signatures/consent).
 * Those are shown "sign by hand", never auto-filled, and excluded from submission.
 */

import { useMemo, useState } from "react";

const C = {
  ok: "#2f855a",
  attention: "#c05621",
  human: "#6b46c1",
  border: "#e2e8f0",
  selBorder: "#2b6cb0",
  selBg: "rgba(43,108,168,0.10)",
  boxIdle: "rgba(43,108,168,0.35)",
  boxAttn: "rgba(192,86,33,0.55)",
  text: "#1a202c",
  sub: "#718096",
  bg: "#f7fafc",
};

function statusOf(field, review) {
  if (field.fill_policy === "human_only") return "human";
  if (review.needs_attention.includes(field.name)) return "attention";
  return "ok";
}

const STATUS_LABEL = {
  ok: "Auto-filled",
  attention: "Check this",
  human: "Sign by hand",
};

export default function ReviewUI({ schema, review, pageImageUrl, pageSize, onSubmit }) {
  // Editable copy of the proposed values (human_only stay blank).
  const [values, setValues] = useState(() => ({ ...review.values }));
  const [selected, setSelected] = useState(null);

  const pages = useMemo(
    () => [...new Set(schema.fields.map((f) => f.page ?? 1))].sort((a, b) => a - b),
    [schema.fields]
  );
  const [page, setPage] = useState(pages[0] ?? 1);

  const fieldsOnPage = schema.fields.filter((f) => (f.page ?? 1) === page);

  const setValue = (name, v) => setValues((prev) => ({ ...prev, [name]: v }));

  const unresolved = schema.fields.filter(
    (f) => review.needs_attention.includes(f.name) && !values[f.name]
  ).length;

  const submit = () => {
    // Never submit human_only values; the injector leaves them blank (§2).
    const reviewed = Object.fromEntries(
      schema.fields
        .filter((f) => f.fill_policy !== "human_only")
        .map((f) => [f.name, values[f.name] ?? null])
    );
    onSubmit?.(reviewed);
  };

  return (
    <div style={styles.root}>
      <header style={styles.header}>
        <div>
          <div style={styles.h1}>Review extracted fields</div>
          <div style={styles.sub}>
            {schema.form_id} · referral {review.referral_id}
          </div>
        </div>
        <div style={styles.headerRight}>
          {unresolved > 0 ? (
            <span style={{ ...styles.pill, background: C.attention }}>
              {unresolved} need attention
            </span>
          ) : (
            <span style={{ ...styles.pill, background: C.ok }}>All checked</span>
          )}
          <button
            style={{ ...styles.submit, opacity: unresolved ? 0.5 : 1 }}
            disabled={unresolved > 0}
            onClick={submit}
          >
            Confirm &amp; submit
          </button>
        </div>
      </header>

      <div style={styles.split}>
        {/* LEFT — fields */}
        <div style={styles.left}>
          {schema.fields.map((field) => {
            const st = statusOf(field, review);
            const isSel = selected === field.name;
            const isHuman = field.fill_policy === "human_only";
            return (
              <div
                key={field.name}
                onClick={() => {
                  setSelected(field.name);
                  setPage(field.page ?? 1);
                }}
                style={{
                  ...styles.fieldCard,
                  borderColor: isSel ? C.selBorder : C.border,
                  background: isSel ? C.selBg : "#fff",
                }}
              >
                <div style={styles.fieldTop}>
                  <span style={styles.fieldName}>{field.name}</span>
                  <span style={{ ...styles.badge, color: C[st] }}>
                    {STATUS_LABEL[st]}
                  </span>
                </div>

                {isHuman ? (
                  <div style={styles.humanBox}>✍️ Left blank for the reviewer to sign</div>
                ) : (
                  <input
                    style={styles.input}
                    value={values[field.name] ?? ""}
                    placeholder="—"
                    onChange={(e) => setValue(field.name, e.target.value)}
                    onFocus={() => {
                      setSelected(field.name);
                      setPage(field.page ?? 1);
                    }}
                  />
                )}

                {review.provenance[field.name] && (
                  <div style={styles.prov}>from {review.provenance[field.name]}</div>
                )}
              </div>
            );
          })}
        </div>

        {/* RIGHT — PDF page with overlay boxes */}
        <div style={styles.right}>
          {pages.length > 1 && (
            <div style={styles.pageTabs}>
              {pages.map((p) => (
                <button
                  key={p}
                  onClick={() => setPage(p)}
                  style={{
                    ...styles.pageTab,
                    ...(p === page ? styles.pageTabActive : {}),
                  }}
                >
                  Page {p}
                </button>
              ))}
            </div>
          )}
          <div style={styles.pageWrap}>
            <img
              src={pageImageUrl?.(page)}
              alt={`form page ${page}`}
              style={styles.pageImg}
            />
            {fieldsOnPage.map((field) => {
              if (!field.rect) return null;
              const [x0, y0, x1, y1] = field.rect;
              const isSel = selected === field.name;
              const attn = review.needs_attention.includes(field.name);
              return (
                <div
                  key={field.name}
                  onClick={() => setSelected(field.name)}
                  title={field.name}
                  style={{
                    position: "absolute",
                    left: `${(x0 / pageSize.width) * 100}%`,
                    top: `${(y0 / pageSize.height) * 100}%`,
                    width: `${((x1 - x0) / pageSize.width) * 100}%`,
                    height: `${((y1 - y0) / pageSize.height) * 100}%`,
                    border: `2px solid ${
                      isSel ? C.selBorder : attn ? C.boxAttn : C.boxIdle
                    }`,
                    background: isSel ? C.selBg : "transparent",
                    boxShadow: isSel ? `0 0 0 3px ${C.selBg}` : "none",
                    borderRadius: 2,
                    cursor: "pointer",
                    transition: "all 120ms ease",
                  }}
                />
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

const styles = {
  root: { fontFamily: "system-ui, sans-serif", color: C.text, height: "100vh", display: "flex", flexDirection: "column", background: C.bg },
  header: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 20px", borderBottom: `1px solid ${C.border}`, background: "#fff" },
  h1: { fontSize: 18, fontWeight: 600 },
  sub: { fontSize: 13, color: C.sub, marginTop: 2 },
  headerRight: { display: "flex", alignItems: "center", gap: 12 },
  pill: { color: "#fff", fontSize: 12, fontWeight: 600, padding: "4px 10px", borderRadius: 999 },
  submit: { background: C.selBorder, color: "#fff", border: "none", padding: "9px 16px", borderRadius: 8, fontSize: 14, fontWeight: 600, cursor: "pointer" },
  split: { display: "grid", gridTemplateColumns: "minmax(320px, 380px) 1fr", flex: 1, minHeight: 0 },
  left: { overflowY: "auto", padding: 16, display: "flex", flexDirection: "column", gap: 10, borderRight: `1px solid ${C.border}` },
  fieldCard: { border: "1px solid", borderRadius: 10, padding: "10px 12px", cursor: "pointer", transition: "all 120ms ease" },
  fieldTop: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 },
  fieldName: { fontSize: 13, fontWeight: 600 },
  badge: { fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.3 },
  input: { width: "100%", boxSizing: "border-box", padding: "7px 9px", fontSize: 14, border: `1px solid ${C.border}`, borderRadius: 6, outline: "none" },
  humanBox: { fontSize: 13, color: C.human, background: "rgba(107,70,193,0.08)", padding: "7px 9px", borderRadius: 6 },
  prov: { fontSize: 11, color: C.sub, marginTop: 5 },
  right: { overflowY: "auto", padding: 20, display: "flex", flexDirection: "column", alignItems: "center" },
  pageTabs: { display: "flex", gap: 6, marginBottom: 12 },
  pageTab: { border: `1px solid ${C.border}`, background: "#fff", padding: "5px 12px", borderRadius: 6, fontSize: 13, cursor: "pointer" },
  pageTabActive: { background: C.selBorder, color: "#fff", borderColor: C.selBorder },
  pageWrap: { position: "relative", width: "100%", maxWidth: 720, boxShadow: "0 2px 12px rgba(0,0,0,0.12)" },
  pageImg: { width: "100%", display: "block" },
};
