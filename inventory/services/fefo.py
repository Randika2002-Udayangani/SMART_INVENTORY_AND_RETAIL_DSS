"""
FEFO (First-Expired-First-Out) stock deduction — single source of truth.

Root cause this fixes: ItemLedgerPDFUploadView (sales/views.py) has NEVER
deducted stock on sale, despite the API Design Document documenting FEFO
deduction as part of that endpoint. Confirmed by searching every
StockLedger.objects.create() call site in the codebase -- only
purchases/serializers.py (batch creation), orders/views.py (order
reservations), and inventory/views.py (manual adjustment + expiry
auto-detect) ever touch it. Sales ingestion was never one of them. Every
real sale since the pipeline was built has left PurchaseBatch.remaining_quantity
untouched, which is very likely the dominant cause behind abnormal current-
stock readings (e.g. REVELLO CASHEW 50g showing 5,764 units).

This function is used by BOTH:
    1. sales/views.py -- ItemLedgerPDFUploadView, for every FUTURE upload
    2. inventory/management/commands/reconcile_stock_fefo.py -- ONE-TIME
       backfill for every sale recorded before this fix existed

Keeping this as one shared function (rather than writing the deduction
logic twice) means the live pipeline and the historical backfill can
never silently drift apart from each other.

Batch eligibility: status ACTIVE or PENDING_EXPIRY. PENDING_EXPIRY batches
are unclassified sellable inventory due to a status-transition bug in the
purchase ingestion pipeline, NOT genuinely expiring stock -- confirmed
with Randika. Filtering to ACTIVE only here would silently repeat the
exact status-filter bug already fixed in reorder_logic.py / health_score.py.

FEFO order: expiry_date ascending. Batches with no expiry_date are placed
LAST (nulls_last) -- we can't meaningfully prioritise a batch whose
expiry we don't know, so it's only touched once every dated batch is
exhausted.
"""

from django.db import transaction
from django.db.models import F

from purchases.models import PurchaseBatch
from inventory.models import StockLedger


def deduct_stock_fefo(product_id, quantity, source, reference_id=None):
    """
    Deducts `quantity` units from a product's sellable batches in FEFO
    order. A single sale may span multiple batches if the earliest-
    expiring batch doesn't have enough remaining_quantity to cover it.

    Args:
        product_id   : Product.id
        quantity     : positive int -- units to deduct
        source       : str, stored on StockLedger.source, e.g.
                       'SALE_SYNC_ITEM_LEDGER' (live pipeline) or
                       'SALE_SYNC_RECONCILIATION' (historical backfill)
        reference_id : optional int, e.g. ItemSalesRecord.id -- stored on
                       StockLedger.reference_id for traceability back to
                       the sale that triggered this deduction

    Returns:
        {
            'deducted'        : int,  # actually deducted (== quantity unless stock ran out)
            'shortfall'       : int,  # quantity that could NOT be covered (0 = fully covered)
            'batches_touched' : [ {'batch_id': int, 'quantity_change': int,
                                    'new_remaining': int}, ... ],
        }

    Does NOT raise on shortfall. An oversell (sale quantity exceeding all
    sellable stock for that product) is a real possibility with historical
    data recorded before this fix existed -- the caller knows whether it's
    processing a live sale or a historical backfill and is better placed
    to decide how to report/handle it than this shared function is.
    """
    if quantity <= 0:
        return {'deducted': 0, 'shortfall': 0, 'batches_touched': []}

    with transaction.atomic():
        # select_for_update: prevents two concurrent deductions (e.g. a
        # live upload running at the same time as the reconciliation
        # command) from both reading the same remaining_quantity and
        # double-deducting against it.
        batches = list(
            PurchaseBatch.objects
            .select_for_update()
            .filter(
                product_id=product_id,
                status__in=['ACTIVE', 'PENDING_EXPIRY'],
                remaining_quantity__gt=0,
            )
            .order_by(F('expiry_date').asc(nulls_last=True), 'id')
        )

        remaining_to_deduct = quantity
        batches_touched = []
        ledger_entries = []

        for batch in batches:
            if remaining_to_deduct <= 0:
                break

            take = min(batch.remaining_quantity, remaining_to_deduct)
            if take <= 0:
                continue

            new_remaining = batch.remaining_quantity - take
            batch.remaining_quantity = new_remaining
            if new_remaining == 0:
                batch.status = 'DEPLETED'
            batch.save(update_fields=['remaining_quantity', 'status'])

            ledger_entries.append(
                StockLedger(
                    product_id=product_id,
                    batch=batch,
                    transaction_type='SALE_SYNC',
                    source=source,
                    quantity_change=-take,
                    reference_id=reference_id,
                )
            )
            batches_touched.append({
                'batch_id': batch.id,
                'quantity_change': -take,
                'new_remaining': new_remaining,
            })

            remaining_to_deduct -= take

        if ledger_entries:
            StockLedger.objects.bulk_create(ledger_entries)

    deducted = quantity - remaining_to_deduct
    return {
        'deducted': deducted,
        'shortfall': remaining_to_deduct,
        'batches_touched': batches_touched,
    }