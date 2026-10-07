"""
One-time catalog-wide opening-stock top-up.

Root cause this addresses: sales and purchase history in this dataset
were generated as two independent synthetic streams, never cross-
constrained against each other -- because nothing ever required them to
agree until reconcile_stock_fefo existed. The result: across almost the
entire catalog, total units sold slightly (or, for a handful of loose/
bulk/in-house-prepared items like shopping bags and samosas, drastically)
exceeds total units ever purchased. This isn't corrupted data to hunt
down row-by-row -- it's a legitimate one-time reconciliation gap, the
same shape already confirmed for REVELLO CASHEW 50g and the ~24-product
loose-item sample (single opening-stock batch, sku_code=None, real sales
history predating any real purchase record).

This command tops up exactly that gap: for every product where
sold > purchased, it inserts ONE new PurchaseBatch (grouped under one new
Purchase header, same "one invoice, many batch lines" shape as the
existing OPENING-STOCK-2026 entries) sized to the deficit plus a small
buffer. Products with zero or negative deficit are left untouched --
this command only ever ADDS stock, never removes or adjusts existing
batches.

Reuses the SAME supplier as the existing OPENING-STOCK-2026 purchase(s)
found in the DB, rather than guessing a new one -- keeps this
consistent with your team's established opening-stock convention.
Labeled with a distinct invoice_number (OPENING-STOCK-RECONCILE-<date>)
so it's clearly distinguishable from both the original opening-stock run
and any real supplier purchases in upload_log / audit history.

USAGE:
    python manage.py top_up_opening_stock --dry-run
    python manage.py top_up_opening_stock --dry-run --product-id 54
    python manage.py top_up_opening_stock                 # apply for real
    python manage.py top_up_opening_stock --buffer 15      # override default buffer (10 units)
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum

from products.models import Product
from purchases.models import Purchase, PurchaseBatch
from sales.models import ItemSalesRecord

DEFAULT_BUFFER = 10  # flat units added on top of the exact deficit, so
                      # reconciliation doesn't land a product at exactly
                      # zero remaining stock.
LOCAL_TZ = ZoneInfo("Asia/Colombo")


class Command(BaseCommand):
    help = (
        'One-time catalog-wide opening-stock top-up: inserts a new '
        'PurchaseBatch for every product where total sold exceeds total '
        'ever purchased, sized to the deficit plus a buffer. Products '
        'with zero/negative deficit are untouched. --dry-run reports '
        'the scope without writing anything.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report scope and totals without writing anything.',
        )
        parser.add_argument(
            '--product-id', type=int, default=None,
            help='Limit to a single product, e.g. to verify against '
                 'REVELLO CASHEW 50g (id=54) before running catalog-wide.',
        )
        parser.add_argument(
            '--buffer', type=int, default=DEFAULT_BUFFER,
            help=f'Flat units added on top of the exact deficit per '
                 f'product (default {DEFAULT_BUFFER}).',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        product_id = options['product_id']
        buffer_units = options['buffer']

        # ── Find the existing opening-stock supplier to reuse ──────────────
        reference_purchase = (
            Purchase.objects
            .filter(invoice_number__icontains='OPENING-STOCK')
            .order_by('purchase_date')
            .first()
        )
        if reference_purchase is None:
            raise CommandError(
                "No existing 'OPENING-STOCK' purchase found to determine "
                "which supplier to reuse. Check the invoice_number pattern "
                "matches what initial_stock.py actually used, or pass a "
                "supplier explicitly (not yet supported by this command -- "
                "add a --supplier-id argument if needed)."
            )
        supplier = reference_purchase.supplier
        self.stdout.write(
            f"Reusing supplier from existing opening-stock entry: "
            f"{supplier} (id={supplier.id})\n"
        )

        # ── Compute sold vs purchased per product ──────────────────────────
        sold_qs = ItemSalesRecord.objects.values('product_id').annotate(
            total=Sum('quantity_sold')
        )
        if product_id:
            sold_qs = sold_qs.filter(product_id=product_id)
        sold_map = {row['product_id']: row['total'] or 0 for row in sold_qs}

        purchased_qs = PurchaseBatch.objects.values('product_id').annotate(
            total=Sum('quantity_received')
        )
        purchased_map = {row['product_id']: row['total'] or 0 for row in purchased_qs}

        deficits = []
        for pid, sold in sold_map.items():
            purchased = purchased_map.get(pid, 0)
            deficit = sold - purchased
            if deficit > 0:
                deficits.append((pid, sold, purchased, deficit))

        deficits.sort(key=lambda x: -x[3])

        if not deficits:
            self.stdout.write(self.style.SUCCESS(
                "No products with a positive deficit found. Nothing to do."
            ))
            return

        products_by_id = {
            p.id: p for p in Product.objects.filter(
                id__in=[d[0] for d in deficits]
            ).only('id', 'product_name', 'avg_cost_price', 'cost_price')
        }

        total_units_to_add = 0
        zero_cost_count = 0
        batch_plan = []  # (product, quantity_to_add, cost_price)

        for pid, sold, purchased, deficit in deficits:
            product = products_by_id.get(pid)
            if product is None:
                continue  # shouldn't happen, but skip defensively

            qty_to_add = deficit + buffer_units
            cost_price = product.avg_cost_price or product.cost_price or 0
            if cost_price == 0:
                zero_cost_count += 1

            batch_plan.append((product, qty_to_add, cost_price))
            total_units_to_add += qty_to_add

        self.stdout.write(f"Products with positive deficit: {len(batch_plan)}")
        self.stdout.write(f"Total units to add (deficit + {buffer_units} buffer each): {total_units_to_add}")
        self.stdout.write(f"Products where cost_price will be 0.00 on the new batch: {zero_cost_count}")

        self.stdout.write("\nTop 20 by deficit size:")
        for product, qty_to_add, cost_price in batch_plan[:20]:
            self.stdout.write(
                f"  {product.product_name}: +{qty_to_add} units "
                f"(cost_price {cost_price})"
            )

        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                "\n[DRY RUN] Nothing written. Drop --dry-run to apply, "
                "or use --product-id to verify a single product first."
            ))
            return

        # ── Apply for real ──────────────────────────────────────────────────
        today = datetime.now(LOCAL_TZ).date()
        with transaction.atomic():
            new_purchase = Purchase.objects.create(
                supplier=supplier,
            purchase_date=today,
            invoice_number=f'OPENING-STOCK-RECONCILE-{today.isoformat()}',
                total_amount=sum(
                    qty * cost for _, qty, cost in batch_plan
                ),
            )

            batches = [
                PurchaseBatch(
                    purchase=new_purchase,
                    product=product,
                    quantity_received=qty_to_add,
                    remaining_quantity=qty_to_add,
                    cost_price=cost_price,
                    expiry_date=None,
                    status='ACTIVE',
                )
                for product, qty_to_add, cost_price in batch_plan
            ]
            PurchaseBatch.objects.bulk_create(batches)

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Created Purchase {new_purchase.invoice_number} "
            f"(id={new_purchase.id}) with {len(batches)} batch line(s), "
            f"{total_units_to_add} total units added."
        ))
        if zero_cost_count > 0:
            self.stdout.write(self.style.WARNING(
                f"\n{zero_cost_count} of these new batches have cost_price "
                f"0.00 (the product itself has no cost data). This mirrors "
                f"your existing zero-cost-product issue -- doesn't block "
                f"stock reconciliation, but profit/WAC figures for these "
                f"products remain unreliable until that's separately fixed."
            ))
        self.stdout.write(
            "\nNext: re-run 'python manage.py reconcile_stock_fefo --dry-run' "
            "catalog-wide -- shortfalls should drop substantially now."
        )