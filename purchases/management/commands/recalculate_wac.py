"""
One-off management command to fix avg_cost_price for every product that
has purchase batches, using the true weighted-average formula across ALL
their ACTIVE batches (not just the first purchase).

Run this ONCE after deploying the WAC fix in purchases/views.py, to
correct the 500+ batches already created before the fix existed.

USAGE:
    python manage.py recalculate_wac --dry-run     # preview changes, no writes
    python manage.py recalculate_wac                # apply changes

Fix (Nipuni, 2026-08-22) -- this command was the one WAC implementation
left out of sync with the other two (purchases/serializers.py Fix 7+8,
purchases/views.py _recalculate_avg_cost_price). Brought in line on both
axes Randika flagged:

  1. status='ACTIVE' filter -- previously counted PENDING_EXPIRY batches
     into avg_cost_price with no filter at all. Since R9 means every batch
     from the PDF invoice pipeline starts as PENDING_EXPIRY, running this
     command would have silently undone the ACTIVE-only fix in the other
     two implementations for every product, the moment anyone ran it.

  2. remaining_quantity instead of quantity_received -- matches Fix 7 in
     serializers.py. WAC should reflect current stock reality (what's
     actually still on the shelf), not original arrival quantity. A batch
     that's mostly sold through shouldn't still weight its full original
     quantity into the cost average -- this feeds F05 profit calculation
     and F09's discount profit floor directly.

Edge case (Logic Report, same convention as serializers.py Fix 7+8):
if a product's ACTIVE batches sum to zero remaining units (e.g. fully
sold through, or all batches still PENDING_EXPIRY), avg_cost_price is
left UNCHANGED, not reset to 0 -- the last known cost basis stays as a
useful reference. This is different from a product that's never had any
purchase batches at all (avg_cost_price legitimately has no basis and
should be 0) -- that case is handled separately by cleanup tooling, not
this command, since this command only touches products that already have
at least one batch on record.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum, F, DecimalField
from django.db.models.functions import Coalesce

from products.models import Product
from purchases.models import PurchaseBatch


class Command(BaseCommand):
    help = (
        'Recalculates avg_cost_price (WAC) for all products based on their '
        'ACTIVE purchase batches only, using remaining_quantity -- matches '
        'purchases/serializers.py Fix 7+8 and purchases/views.py '
        '_recalculate_avg_cost_price exactly, so all three WAC code paths '
        'in the system now agree.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would change without saving anything.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        product_ids_with_batches = (
            PurchaseBatch.objects.values_list('product_id', flat=True)
            .exclude(product_id__isnull=True)  # R7 batches have product=NULL, skip
            .distinct()
        )
        products = Product.objects.filter(id__in=product_ids_with_batches)

        self.stdout.write(f"Found {products.count()} products with purchase batches.\n")

        updated = 0
        unchanged = 0
        skipped_zero_qty = 0

        for product in products:
            agg = PurchaseBatch.objects.filter(
                product=product,
                status='ACTIVE',          # Fix: was unfiltered, counted PENDING_EXPIRY
            ).aggregate(
                total_cost=Coalesce(
                    Sum(
                        F('remaining_quantity') * F('cost_price'),   # Fix 7: was quantity_received
                        output_field=DecimalField(max_digits=14, decimal_places=2)
                    ),
                    Decimal('0'),
                ),
                total_qty=Coalesce(Sum('remaining_quantity'), 0),    # Fix 7: was quantity_received
            )
            total_cost = agg['total_cost']
            total_qty = agg['total_qty']

            if not total_qty or total_qty <= 0:
                # Matches serializers.py convention: no ACTIVE stock with
                # remaining units -> keep existing avg_cost_price as-is,
                # don't reset it. It's still meaningful reference data.
                skipped_zero_qty += 1
                continue

            new_avg = (total_cost / total_qty).quantize(Decimal('0.01'))
            old_avg = product.avg_cost_price

            if old_avg != new_avg:
                self.stdout.write(
                    f"  {product.product_name}: {old_avg} -> {new_avg} "
                    f"(total_qty={total_qty}, total_cost={total_cost})"
                )
                if not dry_run:
                    product.avg_cost_price = new_avg
                    product.save(update_fields=['avg_cost_price'])
                updated += 1
            else:
                unchanged += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f"{'[DRY RUN] Would update' if dry_run else 'Updated'} {updated} products. "
            f"{unchanged} already correct. {skipped_zero_qty} skipped "
            f"(no ACTIVE batches with remaining stock -- avg_cost_price left unchanged)."
        ))