"""
One-off management command to fix avg_cost_price for every product that
has purchase batches, using the true weighted-average formula across ALL
their batches (not just the first purchase).

Run this ONCE after deploying the WAC fix in purchases/views.py, to
correct the 500+ batches already created before the fix existed.

USAGE:
    python manage.py recalculate_wac --dry-run     # preview changes, no writes
    python manage.py recalculate_wac                # apply changes
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum, F, DecimalField
from django.db.models.functions import Coalesce

from products.models import Product
from purchases.models import PurchaseBatch


class Command(BaseCommand):
    help = (
        'Recalculates avg_cost_price (WAC) for all products based on ALL '
        'their purchase batches, not just the first purchase. Fixes the '
        'frozen-avg_cost_price bug in PurchaseInvoicePDFUploadView.'
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
            agg = PurchaseBatch.objects.filter(product=product).aggregate(
                total_cost=Coalesce(
                    Sum(
                        F('quantity_received') * F('cost_price'),
                        output_field=DecimalField(max_digits=14, decimal_places=2)
                    ),
                    Decimal('0'),
                ),
                total_qty=Coalesce(Sum('quantity_received'), 0),
            )
            total_cost = agg['total_cost']
            total_qty = agg['total_qty']

            if not total_qty or total_qty <= 0:
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
            f"{unchanged} already correct. {skipped_zero_qty} skipped (zero total qty)."
        ))