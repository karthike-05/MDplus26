"""Toy services directory — synthetic (CLAUDE.md §2).

Stands in for what an incumbent aggregator (findhelp / Unite Us) provides: a catalog
of social services with contact info, forms, and links. Discovery/aggregation is NOT
our differentiator — completing the referral is — so this is a hard-coded handful.
In production this table is populated from a partner integration.

Each service advertises a ``preferred_channel`` (form | phone | text | email) that
prefills the referral's outreach method; the social worker can override per referral.
``form_id`` links form-channel services to a verified schema in contracts/schemas/.
"""

from __future__ import annotations

SERVICES: dict[str, dict] = {
    "svc_capmetro": {
        "id": "svc_capmetro",
        "name": "CapMetro Access NEMT",
        "category": "Transportation",
        "preferred_channel": "form",
        "form_id": "transport_intake",
        "phone": "(512) 369-6000",
        "email": "intake@capmetroaccess.example",
        "website": "https://capmetro.example/access",
        "address": "2910 E 5th St, Austin, TX 78702",
        "description": "Non-emergency medical transport for riders with disabilities.",
    },
    "svc_drive_senior": {
        "id": "svc_drive_senior",
        "name": "Drive A Senior ATX",
        "category": "Transportation",
        "preferred_channel": "phone",
        "form_id": None,
        "phone": "(512) 274-5040",
        "email": "rides@driveasenior.example",
        "website": "https://driveasenior.example",
        "address": "3710 Cedar St, Austin, TX 78705",
        "description": "Volunteer-driven rides to appointments for older adults.",
    },
    "svc_food_bank": {
        "id": "svc_food_bank",
        "name": "Central Texas Food Bank",
        "category": "Food assistance",
        "preferred_channel": "form",
        "form_id": "food_assistance",
        "phone": "(512) 684-2550",
        "email": "clientservices@ctfb.example",
        "website": "https://centraltexasfoodbank.example",
        "address": "6500 Metropolis Dr, Austin, TX 78744",
        "description": "SNAP application help and emergency food box referrals.",
    },
    "svc_meals_wheels": {
        "id": "svc_meals_wheels",
        "name": "Meals on Wheels Central Texas",
        "category": "Food assistance",
        "preferred_channel": "text",
        "form_id": None,
        "phone": "(512) 476-6325",
        "email": "intake@mowcentraltexas.example",
        "website": "https://mealsonwheelscentraltexas.example",
        "address": "3227 E 5th St, Austin, TX 78702",
        "description": "Home-delivered meals for homebound seniors.",
    },
    "svc_housing": {
        "id": "svc_housing",
        "name": "Housing Authority Utility Relief",
        "category": "Housing & utilities",
        "preferred_channel": "email",  # exercises the email expansion stub
        "form_id": None,
        "phone": "(512) 477-4488",
        "email": "utilityrelief@hacanet.example",
        "website": "https://hacanet.example/relief",
        "address": "1124 S IH 35, Austin, TX 78704",
        "description": "One-time utility and rent relief for qualifying households.",
    },
}
