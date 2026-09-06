# inventory/management/commands/seed_discount_rules.py
#
# Without any DiscountRule rows, _find_matching_tier() always returns
# None for every batch, calculate_discounts() marks everything
# skipped_no_tier, and zero DiscountRecommendation rows are ever
# created — the Discount Engine page will look "empty" even with
# correct ACTIVE batch data. There was no seed path for this table
# anywhere (checked migrations, fixtures, management commands) —
# same gap seed_store_zones.py filled for StoreZone.
#
# Default tiers per API_Design_Document_v3.2 §14.1:
#   >90d = 0% (no tier — no discount needed, handled by returning None)
#   60-90d = 5%, 30-60d = 15%, 14-30d = 25%, 7-14d = 40%, <7d = 50%
#
# Boundary semantics match _find_matching_tier() exactly:
#   days_from_expiry_min <= days_until_expiry < days_from_expiry_max
#
# Idempotent — safe to re-run. Won't touch tiers a manager has since
# customised via the discount-rules CRUD endpoints (skips on exact
# min/max match rather than wiping and recreating).
#
# Run with:
#   python manage.py seed_discount_rules

from django.core.management.base import BaseCommand
from inventory.models import DiscountRule


DEFAULT_TIERS = [
    {"days_from_expiry_min": 60, "days_from_expiry_max": 90, "discount_percentage": 5,  "minimum_margin_pct": 10},
    {"days_from_expiry_min": 30, "days_from_expiry_max": 60, "discount_percentage": 15, "minimum_margin_pct": 10},
    {"days_from_expiry_min": 14, "days_from_expiry_max": 30, "discount_percentage": 25, "minimum_margin_pct": 10},
    {"days_from_expiry_min": 7,  "days_from_expiry_max": 14, "discount_percentage": 40, "minimum_margin_pct": 10},
    {"days_from_expiry_min": 0,  "days_from_expiry_max": 7,  "discount_percentage": 50, "minimum_margin_pct": 10},
]


class Command(BaseCommand):
    help = "Seed the default tiered discount rules (API Design Doc §14.1) if none exist yet."

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Also create any default tier that is missing even if other tiers already exist '
                 '(default behaviour skips entirely if the table is non-empty, to avoid duplicating '
                 'a manager\'s already-customised rule set).',
        )

    def handle(self, *args, **options):
        existing_count = DiscountRule.objects.count()

        if existing_count > 0 and not options['force']:
            self.stdout.write(self.style.WARNING(
                f"DiscountRule already has {existing_count} row(s) — skipping. "
                f"Pass --force to add any missing default tiers alongside existing ones."
            ))
            return

        created = 0
        skipped = 0
        for tier in DEFAULT_TIERS:
            obj, was_created = DiscountRule.objects.get_or_create(
                days_from_expiry_min=tier['days_from_expiry_min'],
                days_from_expiry_max=tier['days_from_expiry_max'],
                defaults={
                    'discount_percentage': tier['discount_percentage'],
                    'minimum_margin_pct': tier['minimum_margin_pct'],
                    'is_active': True,
                },
            )
            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(
                    f"Created tier: {tier['days_from_expiry_min']}-{tier['days_from_expiry_max']} days "
                    f"-> {tier['discount_percentage']}%"
                ))
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Created {created} tier(s), skipped {skipped} already-existing tier(s)."
        ))
        if created == 0:
            self.stdout.write(self.style.WARNING(
                "No tiers were created. If you expected new tiers, check for a min/max boundary "
                "collision with existing rows."
            ))