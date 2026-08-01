/**
 * DEV TOOLS — one switch for every control that is an engineering aid rather than
 * something a social worker (or a judge handed the URL) should see or be able to press.
 *
 * WHY THIS EXISTS. Two controls on the public deploy were actively dangerous rather than
 * merely confusing:
 *
 *   - The Mock/Supabase data-source toggle calls POST /api/db, which swaps the adapter
 *     **process-wide**. One person clicking "Mock" changes the data source for everyone
 *     using the deployment simultaneously, and the next visitor sees fixture data with
 *     nothing explaining why. It is a debugging control wired to global mutable state.
 *   - The Integration tab is a bus-debugging panel: dedup keys, poisoned actions,
 *     component ownership, unclaimed queues. Useful to us, unreadable to anyone else.
 *
 * HOW IT'S SET. Vite inlines `import.meta.env.VITE_*` at **build** time (see api.js), so
 * `VITE_DEV_TOOLS=1 npm run build` bakes it in — there is no way to flip it on a
 * deployed bundle at runtime, which is what we want for the public deploy. For local
 * work, `?dev=1` turns it on in the browser without a rebuild.
 *
 * Default OFF. A fresh clone and the Railway build both get the safe surface.
 */

const FLAG = String(import.meta.env.VITE_DEV_TOOLS ?? "").trim().toLowerCase();
const FROM_ENV = ["1", "true", "yes"].includes(FLAG);
const FROM_QUERY = new URLSearchParams(location.search).has("dev");

export const DEV_TOOLS = FROM_ENV || FROM_QUERY;
