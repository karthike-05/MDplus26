"""Print the live Supabase schema so we can align the *_COLS maps to the real
column names (docs/db-contract.md step 2). Read-only — touches no data.

    python -m backend.scripts.db_introspect

Prefers the REST API (SUPABASE_URL + SUPABASE_SERVICE_KEY) — the stable path — and
falls back to raw Postgres (SUPABASE_DB_URL) if only a DSN is set. Either way it
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

# What the ReferralDB contract needs to exist (db-contract.md). Names here are the
# DEFAULTS in supabase.py; if the live DB differs, that's exactly what we're mapping.
REQUIRED = {
    "patients": ["id"],
    "referrals": ["id", "patient_id", "form_id", "current_state"],
    "outreach_attempts": ["attempt_id", "referral_id", "channel", "status", "from_state", "data"],
    "social_services": ["id", "name", "preferred_channel"],
}


async def _api(url: str, key: str) -> None:
    """Probe each expected table via PostgREST: select one row to reveal its
    columns (or surface a missing-table / permission error)."""
    from supabase import acreate_client

    c = await acreate_client(url, key)
    print(f"\n=== Supabase REST API: {url} ===")
    for table, needed in REQUIRED.items():
        try:
            res = await c.table(table).select("*").limit(1).execute()
        except Exception as e:
            msg = str(e)
            print(f"\n{table}\n    [UNREACHABLE] {msg[:160]}")
            continue
        rows = res.data or []
        if not rows:
            print(f"\n{table}\n    (table exists but is EMPTY — columns unknown from a read)")
            continue
        present = set(rows[0].keys())
        print(f"\n{table}  ({len(present)} columns)")
        for col in sorted(present):
            print(f"    {col}")
        missing = [c2 for c2 in needed if c2 not in present]
        if missing:
            print(f"    -> missing required (by default names): {', '.join(missing)}")


async def _pg() -> None:
    dsn = os.getenv("SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("SUPABASE_DB_URL not set — add it to .env first.")

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
            WHERE tc.table_schema = 'public' AND tc.table_name = 'outreach_attempts'
              AND tc.constraint_type IN ('UNIQUE', 'PRIMARY KEY')
            """
        )
        has_uniq = any(u["column_name"] == "attempt_id" for u in uniq)
        print(f"\n  outreach_attempts.attempt_id UNIQUE/PK: "
              f"{'YES' if has_uniq else 'NO — upsert idempotency (§10) will duplicate rows'}")
    finally:
        await conn.close()


async def main() -> None:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if url and key:
        await _api(url, key)
    elif os.getenv("SUPABASE_DB_URL"):
        await _pg()
    else:
        raise SystemExit(
            "Set SUPABASE_URL + SUPABASE_SERVICE_KEY (API, preferred) or "
            "SUPABASE_DB_URL (direct Postgres) in .env first."
        )


if __name__ == "__main__":
    asyncio.run(main())
