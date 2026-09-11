"""
One-off management command to reconcile PurchaseBatch.remaining_quantity
against every historical ItemSalesRecord that was recorded before FEFO
deduction was wired into the sales ingestion pipeline.

Root cause: ItemLedgerPDFUploadView (sales/views.py) never deducted stock
on sale -- confirmed by searching every StockLedger.objects.create() call
site in the codebase; sales ingestion was never one of them. Every sale
recorded before this command existed left PurchaseBatch.remaining_quantity
untouched, which is very likely the dominant driver behind abnormal
current-stock readings (e.g. REVELLO CASHEW 50g showing 5,764 units).
See inventory/services/fefo.py for the shared deduction logic this
command uses -- the SAME function used by the live pipeline going
forward, so historical and future deductions can never drift apart.

Processes ItemSalesRecord rows in sale_date order (oldest first) --
critical for FEFO correctness. Deducting out of order would let a later
sale claim units from an early-expiring batch that an earlier sale should
have consumed first.

Tracks which ItemSalesRecord rows have already been reconciled via
StockLedger.reference_id (set by deduct_stock_fefo), so this command is
SAFE TO RE-RUN -- already-reconciled sales are skipped, not double-deducted.

USAGE:
    python manage.py reconcile_stock_fefo --dry-run     # preview only
    python manage.py reconcile_stock_fefo                # apply

Optional filters (both can be combined):
    python manage.py reconcile_stock_fefo --product-id 42
    python manage.py reconcile_stock_fefo --dry-run --product-id 42
"""

from django.core.management.base import BaseCommand

from sales.models import ItemSalesRecord
from inventory.models import StockLedger
from inventory.services.fefo import deduct_stock_fefo


class Command(BaseCommand):
    help = (
        'One-time backfill: deducts PurchaseBatch.remaining_quantity via '
        'FEFO for every historical ItemSalesRecord that predates the fix '
        'to ItemLedgerPDFUploadView. Safe to re-run -- already-reconciled '
        'sales (tracked via StockLedger.reference_id) are skipped.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deducted without writing anything.',
        )
        parser.add_argument(
            '--product-id',
            type=int,
            default=None,
            help='Limit reconciliation to a single product (e.g. to '
                 'verify against REVELLO CASHEW 50g before running '
                 'catalog-wide).',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        product_id = options['product_id']

        already_reconciled_ids = set(
            StockLedger.objects
            .filter(source='SALE_SYNC_RECONCILIATION', reference_id__isnull=False)
            .values_list('reference_id', flat=True)
        )

        sales = ItemSalesRecord.objects.select_related('product').order_by(
            'sale_date', 'id'
        )
        if product_id:
            sales = sales.filter(product_id=product_id)

        total = sales.count()
        self.stdout.write(f"Found {total} ItemSalesRecord row(s) to check.\n")

        processed = 0
        skipped_already_done = 0
        fully_deducted = 0
        shortfall_count = 0
        total_shortfall_units = 0

        for sale in sales:
            if sale.id in already_reconciled_ids:
                skipped_already_done += 1
                continue

            processed += 1

            if dry_run:
                # Dry run still needs to know what WOULD happen, but must
                # not actually write. deduct_stock_fefo always writes, so
                # for dry-run we only report the sale and move on --
                # a true dry-run preview of multi-batch FEFO math down to
                # the batch level isn't worth the complexity here; the
                # --product-id filter is the intended way to verify a
                # single product's numbers by hand before a full run.
                self.stdout.write(
                    f"  [DRY RUN] Would deduct {sale.quantity_sold} units "
                    f"of {sale.product.product_name} for {sale.sale_date} "
                    f"(ItemSalesRecord id={sale.id})"
                )
                continue

            result = deduct_stock_fefo(
                product_id=sale.product_id,
                quantity=sale.quantity_sold,
                source='SALE_SYNC_RECONCILIATION',
                reference_id=sale.id,
            )

            if result['shortfall'] > 0:
                shortfall_count += 1
                total_shortfall_units += result['shortfall']
                self.stdout.write(self.style.WARNING(
                    f"  SHORTFALL: {sale.product.product_name} on "
                    f"{sale.sale_date} -- sold {sale.quantity_sold}, only "
                    f"{result['deducted']} could be covered by sellable "
                    f"batches (short {result['shortfall']}). This product's "
                    f"stock likely started this reconciliation already "
                    f"understocked relative to recorded sales -- may need "
                    f"a manual review, not a code fix."
                ))
            else:
                fully_deducted += 1

        self.stdout.write('')
        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f"[DRY RUN] {processed} sale(s) would be processed "
                f"({skipped_already_done} already reconciled, skipped). "
                f"No changes written. Re-run with --product-id to verify "
                f"one product's batch-level numbers by hand, or drop "
                f"--dry-run to apply for real."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Done. {fully_deducted} sale(s) fully deducted, "
                f"{shortfall_count} had a shortfall ({total_shortfall_units} "
                f"total units short), {skipped_already_done} already "
                f"reconciled and skipped."
            ))
            if shortfall_count > 0:
                self.stdout.write(self.style.WARNING(
                    f"\n{shortfall_count} product/date combination(s) "
                    f"couldn't be fully covered by existing batches. This "
                    f"is expected if some historical sales genuinely "
                    f"outpaced what was ever purchased on record (data "
                    f"gap, not something this command can fix) -- review "
                    f"those products individually rather than re-running "
                    f"this command again."
                ))