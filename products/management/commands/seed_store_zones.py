# products/management/commands/seed_store_zones.py
# REPLACES the earlier version entirely — that one created a zone per
# category (the 1:1 mistake). Group zones are now created automatically
# by assign_zones_from_groups() every time the calculate endpoint runs,
# so this command only needs to handle the three special-purpose zones,
# which have no category to derive a name from.
#
# Run with:
#   python manage.py seed_store_zones

from django.core.management.base import BaseCommand
from products.models import StoreZone


SPECIAL_ZONES = [
    {
        "zone_name": "Entrance Display",
        "description": "High-visibility zone near the entrance/checkout — for top-performing products",
        "traffic_level": "High",
        "zone_type": "HIGH_TRAFFIC",
    },
    {
        "zone_name": "End of Aisle Promo",
        "description": "End-of-aisle promotional placement — for near-expiry stock",
        "traffic_level": "Medium",
        "zone_type": "PROMOTIONAL",
    },
    {
        "zone_name": "Discount Bin",
        "description": "Clearance/discount area — for slow-moving stock",
        "traffic_level": "Low",
        "zone_type": "DISCOUNT_BIN",
    },
]


class Command(BaseCommand):
    help = "Seeds the 3 special-purpose StoreZone rows (HIGH_TRAFFIC / PROMOTIONAL / DISCOUNT_BIN)."

    def handle(self, *args, **options):
        created = 0
        for spec in SPECIAL_ZONES:
            zone, was_created = StoreZone.objects.get_or_create(
                zone_type=spec["zone_type"],
                defaults={
                    "zone_name": spec["zone_name"],
                    "description": spec["description"],
                    "traffic_level": spec["traffic_level"],
                },
            )
            if was_created:
                created += 1
                self.stdout.write(f"  Created: {zone.zone_name} ({zone.zone_type})")
            else:
                self.stdout.write(f"  Already exists: {zone.zone_name} ({zone.zone_type})")

        self.stdout.write(self.style.SUCCESS(f"\nDone. {created} special zone(s) created."))
        self.stdout.write(
            "Category group zones (Personal Care, Household & Cleaning, etc.) are "
            "created automatically the first time POST /api/zones/recommendations/calculate/ "
            "runs — nothing else to seed manually."
        )