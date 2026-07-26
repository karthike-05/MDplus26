"""Print the live Supabase schema so we can align the *_COLS maps to the real
column names (docs/db-contract.md step 2). Read-only — touches no data.

    python -m backend.scripts.db_introspect

Prefers the REST API (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY) — the stable path — and
falls back to raw Postgres (DATABASE_URL) if only a DSN is set. Either way it
reports which of our REQUIRED tables/columns exist.
"""

from __future__ import annotations

import asyncio
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

# What the ReferralDB contract needs to exist, keyed by the REAL table names now in
# supabase.py's TABLES map. Anything reported missing here is a genuine gap to migrate,
# not a naming mismatch — that's the point of keeping these in sync with the map.
REQUIRED = {
    "patients": ["id", "name", "date_of_birth"],
    "referrals": ["id", "patient_id", "service_id", "current_state"],
    "attempts": ["referral_id", "channel", "status", "structured_result",
                 "attempt_id", "from_state"],
    "services": ["id", "name", "need_category"],
}

# Tables the vendored channel services read/write, so the dump shows the whole shared
# bus rather than just our four. Grepped from backend/{call_agent,service_ranking,
# patient_comms}/ — the reason we care is that these are the rows a real end-to-end run
# has to agree with us about.
THEIRS = {
    "attempts": "outreach log — SHARED; ranking's responsiveness score reads it",
    "services": "our TABLES['social_services']",
    "form_templates": "our form-schema cache (TABLES has no entry — schemas load from JSON)",
    "referral_actions": "DB-side action queue (advance_referral / queue_referral_action)",
    "integration_events": "durable inbound-webhook log",
    "service_application_channels": "preferred_channel + form URL + contact phone",
    "phones": "service contact numbers (HSDS)",
    "ranking_results": "ranking output (upstream of our loop)",
    "sw_feedback": "SW's accept/reject label on a ranking",
    "escalations": "call_agent",
    "service_bookings": "call_agent",
    "service_requests": "call_agent / patient_comms",
    "organizations": "HSDS",
    "locations": "HSDS",
    "service_at_location": "HSDS",
    "service_areas": "HSDS",
    "schedules": "HSDS",
    "cost_options": "HSDS",
    "outreach": "patient_comms (its own table)",
}


async def _api(url: str, key: str) -> None:
    """Dump the schema via PostgREST's OpenAPI spec, then count rows per table.

    The spec (GET /rest/v1/) describes EVERY exposed table and column — including
    tables that are empty, which a `select *` can't reveal and which is exactly the
    case for the ones we most need to see (`attempts`, `ranking_results`). Read-only:
    one GET for the spec, then one zero-row GET per table for its count.
    """
    import httpx

    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    base = url.rstrip("/") + "/rest/v1"
    print(f"\n=== Supabase REST API: {url} ===")

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        spec = await client.get(f"{base}/")
        spec.raise_for_status()
        definitions = spec.json().get("definitions", {})

        counts: dict[str, str] = {}
        for table in definitions:
            # limit=0 returns no rows; Prefer: count=exact puts the total in Content-Range.
            try:
                res = await client.get(
                    f"{base}/{table}",
                    params={"select": "*", "limit": 0},
                    headers={"Prefer": "count=exact"},
                )
                counts[table] = res.headers.get("content-range", "?").split("/")[-1]
            except httpx.HTTPError:
                counts[table] = "?"

    print(f"\n{len(definitions)} table(s) exposed:\n")
    for table in sorted(definitions):
        note = THEIRS.get(table, "")
        print(f"{table}  [{counts.get(table, '?')} rows]{'   <- ' + note if note else ''}")
        props = definitions[table].get("properties", {})
        required = set(definitions[table].get("required", []))
        for col in sorted(props):
            fmt = props[col].get("format", props[col].get("type", "?"))
            pk = " PK" if "<pk/>" in str(props[col].get("description", "")) else ""
            print(f"    {col:<32} {fmt}{'  NOT NULL' if col in required else ''}{pk}")
        print()

    print("=== required-by-our-contract check ===")
    for table, needed in REQUIRED.items():
        if table not in definitions:
            print(f"  [MISSING TABLE] {table}")
            continue
        present = set(definitions[table].get("properties", {}))
        missing = [c for c in needed if c not in present]
        print(f"  {table}: {'OK' if not missing else 'missing cols -> ' + ', '.join(missing)}")


async def _pg() -> None:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set — add it to .env first.")

    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        cols = await conn.fetch(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
            """
        )
        by_table: dict[str, list[tuple[str, str]]] = {}
        for r in cols:
            by_table.setdefault(r["table_name"], []).append((r["column_name"], r["data_type"]))

        print(f"\n=== public schema: {len(by_table)} table(s) ===")
        for t, c in sorted(by_table.items()):
            print(f"\n{t}")
            for name, dtype in c:
                print(f"    {name:<24} {dtype}")

        print("\n=== required-by-our-contract check ===")
        for t, needed in REQUIRED.items():
            present = {name for name, _ in by_table.get(t, [])}
            if t not in by_table:
                print(f"  [MISSING TABLE] {t}")
                continue
            missing = [c for c in needed if c not in present]
            print(f"  {t}: {'OK' if not missing else 'missing cols -> ' + ', '.join(missing)}")

        uniq = await conn.fetch(
            """
            SELECT tc.constraint_type, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_schema = 'public' AND tc.table_name = 'attempts'
              AND tc.constraint_type IN ('UNIQUE', 'PRIMARY KEY')
            """
        )
        has_uniq = any(u["column_name"] == "attempt_id" for u in uniq)
        print(f"\n  attempts.attempt_id UNIQUE/PK: "
              f"{'YES' if has_uniq else 'NO — upsert idempotency (§10) will duplicate rows'}")
    finally:
        await conn.close()


async def main() -> None:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if url and key:
        await _api(url, key)
    elif os.getenv("DATABASE_URL"):
        await _pg()
    else:
        raise SystemExit(
            "Set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (API, preferred) or "
            "DATABASE_URL (direct Postgres) in .env first."
        )


if __name__ == "__main__":
    asyncio.run(main())
