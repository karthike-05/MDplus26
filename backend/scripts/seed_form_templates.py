"""Seed the live `form_templates` table from `contracts/schemas/*.json` (A6).

The shared DB provisioned a versioned, verification-aware home for our form schemas —
`schema_json`, `mapping_json`, `mapping_status`, `verified_at/by`, `active` — and it has
been empty since it was created. This fills it.

WHAT THIS DOES AND DOESN'T CHANGE. The JSON files stay authoritative (CLAUDE.md §5c):
`get_form_schema()` still loads from disk on every adapter, and nothing reads this table
yet. Seeding it is what makes the *cold path* possible later (§13 — upload a PDF, extract
a schema, verify once, cache) and what lets the other services discover which services
have a form at all, without reaching into our repo. If the two ever disagree, the file
wins; re-run this.

WHY A service_id IS REQUIRED. `form_templates.service_id` is NOT NULL with an FK to
`services`, so a template cannot exist unattached — a schema is always *some service's*
application form. Our schema JSON carries no service id (it predates the live schema and
the fixtures use `svc_capmetro`), so the mapping has to be supplied here. There is no
sensible default, and guessing would attach a transport form to an arbitrary service, so
the script refuses rather than inventing one.

    # see what's there and which services could own a template
    python -m backend.scripts.seed_form_templates --list

    # seed one schema against one service
    python -m backend.scripts.seed_form_templates \
        --form transport_intake --service-id 3f2a...  [--dry-run]

    # seed every schema against one service
    python -m backend.scripts.seed_form_templates --all --service-id 3f2a...

Needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY. Read-only without `--service-id`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from backend.db.mock import SCHEMA_DIR, _load_schemas

# `form_templates.source_type` is CHECK-constrained to web_form / pdf; our contract's
# TargetType is pdf / web. One rename, in one place.
SOURCE_TYPE_FOR_TARGET = {"pdf": "pdf", "web": "web_form"}

# Hand-authored and human-verified against the rendered form (§12), which is exactly
# what `verified` means in their vocabulary. The cold path will write `unverified` and
# wait for a reviewer.
MAPPING_STATUS = "verified"
VERIFIED_BY = "karthik_form (hand-authored, contracts/schemas)"
VERSION = "1"


def _client():
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        sys.exit("SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY must be set "
                 "(this script writes to the live DB).")
    from supabase import create_client

    return create_client(url, key)


def template_row(schema, service_id: str) -> dict:
    """One `form_templates` row from one FormSchema.

    `mapping_json` is the field -> source correspondence *alone*, not the whole schema:
    that's the part another service (or the cold path's verifier) actually needs to reason
    about, and keeping it separate is why the live table has two jsonb columns instead of
    one. `human_only` fields are included with a null source on purpose — "this field
    exists and the agent must never fill it" is information, and dropping them would make
    a reader think the form has fewer fields than it does.
    """
    return {
        "service_id": service_id,
        "name": schema.form_id,
        "version": VERSION,
        "source_type": SOURCE_TYPE_FOR_TARGET[schema.target_type],
        "source_url": schema.source_ref,
        "schema_json": json.loads(schema.model_dump_json()),
        "mapping_json": {
            f.name: {"source": f.source, "fill_policy": f.fill_policy,
                     "required": f.required, "format": f.format}
            for f in schema.fields
        },
        "mapping_status": MAPPING_STATUS,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "verified_by": VERIFIED_BY,
        "active": True,
    }


async def _list() -> None:
    c = _client()
    rows = c.table("form_templates").select(
        "id,name,version,service_id,source_type,mapping_status,active").execute().data or []
    print(f"form_templates: {len(rows)} row(s)")
    for r in rows:
        print(f"  {r['name']:24} v{r['version']:3} {r['source_type']:9} "
              f"{r['mapping_status']:11} service={r['service_id']}")

    print(f"\nschemas on disk ({SCHEMA_DIR}):")
    for s in _load_schemas(SCHEMA_DIR).values():
        print(f"  {s.form_id:24} {s.target_type:5} {len(s.fields):2} fields  {s.source_ref}")

    # Which services could own a template. A service whose application channel is
    # `online_form` is the one whose referrals route to us at all (advance_referral
    # step 10), so those are listed first — attaching a template anywhere else seeds a
    # row nothing will ever reach.
    chans = c.table("service_application_channels").select(
        "service_id,channel,priority").execute().data or []
    form_svcs = {r["service_id"] for r in chans if r["channel"] == "online_form"}
    names = {s["id"]: s["name"] for s in
             (c.table("services").select("id,name").execute().data or [])}
    print(f"\nservices with an `online_form` channel ({len(form_svcs)}) — referrals can "
          f"only reach us through these:")
    for sid in sorted(form_svcs, key=lambda s: names.get(s, "")):
        print(f"  {sid}  {names.get(sid, '?')}")


async def _seed(form_ids: list[str], service_id: str, dry_run: bool) -> None:
    schemas = _load_schemas(SCHEMA_DIR)
    unknown = [f for f in form_ids if f not in schemas]
    if unknown:
        sys.exit(f"unknown form_id(s): {unknown}; have {sorted(schemas)}")

    c = _client()
    if not (c.table("services").select("id").eq("id", service_id).execute().data):
        sys.exit(f"service '{service_id}' does not exist — service_id is an FK. "
                 f"Run --list to see the candidates.")

    for form_id in form_ids:
        row = template_row(schemas[form_id], service_id)
        if dry_run:
            print(f"[dry-run] would upsert {form_id} v{VERSION} -> service {service_id}")
            print(json.dumps({k: v for k, v in row.items()
                              if k not in ("schema_json", "mapping_json")}, indent=2))
            continue

        # No natural unique constraint exists on this table, so ON CONFLICT is not
        # available — a blind insert would silently accumulate a duplicate template per
        # run. Match on (service_id, name, version) by hand and update in place.
        existing = c.table("form_templates").select("id").eq(
            "service_id", service_id).eq("name", form_id).eq("version", VERSION).execute().data
        if existing:
            c.table("form_templates").update(row).eq("id", existing[0]["id"]).execute()
            print(f"updated  {form_id} v{VERSION} (id={existing[0]['id']})")
        else:
            res = c.table("form_templates").insert(row).execute()
            print(f"inserted {form_id} v{VERSION} (id={res.data[0]['id']})")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true",
                   help="show existing templates, schemas on disk, and candidate services")
    p.add_argument("--form", action="append", default=[],
                   help="form_id to seed (repeatable)")
    p.add_argument("--all", action="store_true", help="seed every schema in contracts/schemas")
    p.add_argument("--service-id", help="the owning service (FK, NOT NULL)")
    p.add_argument("--dry-run", action="store_true", help="print the rows, write nothing")
    args = p.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    if args.list or not (args.form or args.all):
        asyncio.run(_list())
        if not (args.form or args.all):
            print("\nNothing seeded. Pass --form <id> --service-id <uuid>, or --all.")
        return

    if not args.service_id:
        sys.exit("--service-id is required (form_templates.service_id is NOT NULL). "
                 "Run --list to see which services can own a template.")

    form_ids = sorted(_load_schemas(SCHEMA_DIR)) if args.all else args.form
    asyncio.run(_seed(form_ids, args.service_id, args.dry_run))


if __name__ == "__main__":
    main()
