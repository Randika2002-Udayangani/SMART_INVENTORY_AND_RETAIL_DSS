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

--dry-run FULLY SIMULATES the FEFO deduction in memory (no DB writes at
all -- not even a wrapped-and-rolled-back transaction) so shortfalls are
visible BEFORE you commit to a real run. This matters at catalog scale:
a naive dry-run that just counts sales tells you nothing about whether
stock actually covers them.

USAGE:
    python manage.py reconcile_stock_fefo --dry-run     # simulated preview
    python manage.py reconcile_stock_fefo                # apply

Optional filters (both can be combined):
    python manage.py reconcile_stock_fefo --product-id 42
    python manage.py reconcile_stock_fefo --dry-run --product-id 42
"""

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import F

from sales.models import ItemSalesRecord
from purchases.models import PurchaseBatch
from inventory.models import StockLedger
from inventory.services.fefo import deduct_stock_fefo


class Command(BaseCommand):
    help = (
        'One-time backfill: deducts PurchaseBatch.remaining_quantity via '
        'FEFO for every historical ItemSalesRecord that predates the fix '
        'to ItemLedgerPDFUploadView. Safe to re-run -- already-reconciled '
        'sales (tracked via StockLedger.reference_id) are skipped. '
        '--dry-run fully simulates the FEFO math in memory (no DB writes) '
        'so shortfalls are visible before committing.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Simulate the full FEFO deduction in memory and report '
                 'shortfalls -- writes nothing to the database.',
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

        if dry_run:
            self._simulate(sales, already_reconciled_ids, product_id)
            return

        self._apply(sales, already_reconciled_ids)

    # ── Real run: writes via the shared deduct_stock_fefo() function ──────
    def _apply(self, sales, already_reconciled_ids):
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
                    f"batches (short {result['shortfall']})."
                ))
            else:
                fully_deducted += 1

        self.stdout.write('')
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

    # ── Dry run: simulates FEFO depletion entirely in memory ──────────────
    def _simulate(self, sales, already_reconciled_ids, product_id):
        # Load every sellable batch (ACTIVE/PENDING_EXPIRY) into memory,
        # grouped by product, FEFO-ordered (earliest expiry first, no-
        # expiry batches last) -- same eligibility/ordering rules as
        # deduct_stock_fefo() in inventory/services/fefo.py. Kept in sync
        # deliberately; if that function's rules ever change, this
        # simulation must be updated to match or the preview will lie.
        batch_qs = PurchaseBatch.objects.filter(
            status__in=['ACTIVE', 'PENDING_EXPIRY'],
            remaining_quantity__gt=0,
        ).order_by(F('expiry_date').asc(nulls_last=True), 'id')
        if product_id:
            batch_qs = batch_qs.filter(product_id=product_id)

        # simulated_batches[product_id] = [ [batch_id, remaining_qty], ... ]
        # in FEFO order. A mutable list (not a tuple) so we can decrement
        # remaining_qty in place as the simulation consumes it.
        simulated_batches = defaultdict(list)
        for b in batch_qs.values('id', 'product_id', 'remaining_quantity'):
            simulated_batches[b['product_id']].append(
                [b['id'], b['remaining_quantity']]
            )

        processed = 0
        skipped_already_done = 0
        fully_covered = 0
        shortfall_count = 0
        total_shortfall_units = 0
        shortfall_detail = []

        for sale in sales:
            if sale.id in already_reconciled_ids:
                skipped_already_done += 1
                continue

            processed += 1
            remaining_to_deduct = sale.quantity_sold
            batches = simulated_batches.get(sale.product_id, [])

            for batch in batches:
                if remaining_to_deduct <= 0:
                    break
                take = min(batch[1], remaining_to_deduct)
                if take <= 0:
                    continue
                batch[1] -= take
                remaining_to_deduct -= take

            if remaining_to_deduct > 0:
                shortfall_count += 1
                total_shortfall_units += remaining_to_deduct
                if len(shortfall_detail) < 30:
                    shortfall_detail.append(
                        f"  {sale.product.product_name} on {sale.sale_date}: "
                        f"sold {sale.quantity_sold}, short {remaining_to_deduct}"
                    )
            else:
                fully_covered += 1

        for line in shortfall_detail:
            self.stdout.write(self.style.WARNING(line))
        if shortfall_count > len(shortfall_detail):
            self.stdout.write(self.style.WARNING(
                f"  ... and {shortfall_count - len(shortfall_detail)} more "
                f"shortfall(s) not shown."
            ))

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f"[DRY RUN — fully simulated, nothing written] "
            f"{processed} sale(s) checked ({skipped_already_done} already "
            f"reconciled, skipped)."
        ))
        self.stdout.write(
            f"  Fully covered by existing stock : {fully_covered}"
        )
        self.stdout.write(
            f"  Would have a shortfall          : {shortfall_count} "
            f"sale(s), {total_shortfall_units} total units short"
        )
        if shortfall_count > 0:
            self.stdout.write(self.style.WARNING(
                f"\nA real run would still apply successfully -- shortfalls "
                f"are logged, not fatal -- but {shortfall_count} sale(s) "
                f"would leave batches at zero without fully covering the "
                f"recorded sale. Review whether that magnitude is "
                f"expected (small, scattered gaps) or a sign more "
                f"corrupted data like the REVELLO CASHEW case is still "
                f"present before running for real."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nNo shortfalls in the simulation -- safe to drop "
                "--dry-run and run for real."
            ))