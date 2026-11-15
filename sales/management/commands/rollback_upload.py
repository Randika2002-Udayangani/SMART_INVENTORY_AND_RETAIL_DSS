"""
Undo ONE upload (an UploadLog row): its item sales, its bills and the stock it moved.

    python manage.py rollback_upload <upload_log_id> --dry-run   # look first
    python manage.py rollback_upload <upload_log_id>             # do it

What it reverses
  * Item_Sales_Record rows created by that upload, and the stock they
    deducted (StockLedger rows with source SALE_SYNC_ITEM_LEDGER and
    reference_id = the record id are given back to their batches).
  * Daily_Bill_Summary rows created by that upload.
  * Opening-stock top-up batches (invoice OPENING-STOCK-RECONCILE-*) that
    were only used by this upload (nothing else deducted from them).
  * The UploadLog row itself, and any stored cash-flow review for it.

What it does NOT touch
  * Anything created by other uploads.
  * Products created automatically by the upload (they stay; delete them in
    the Products page if you do not want them).

Everything runs in one transaction: if anything fails nothing is changed.
Find the id with:  SELECT id, file_name, upload_type, status FROM upload_log ORDER BY id DESC;
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventory.models import StockLedger
from purchases.models import Purchase, PurchaseBatch
from sales.models import DailyBillSummary, ItemSalesRecord, UploadLog


class Command(BaseCommand):
    help = "Undo one upload: its sales, bills and the stock it deducted."

    def add_arguments(self, parser):
        parser.add_argument("upload_id", type=int)
        parser.add_argument("--dry-run", action="store_true", help="Show what would change; write nothing.")

    def handle(self, *args, **opts):
        upload_id = opts["upload_id"]
        dry = opts["dry_run"]
        try:
            log = UploadLog.objects.get(pk=upload_id)
        except UploadLog.DoesNotExist:
            raise CommandError(f"No upload_log with id {upload_id}")

        records = list(ItemSalesRecord.objects.filter(upload_id=upload_id))
        record_ids = [r.id for r in records]
        bills = DailyBillSummary.objects.filter(upload_id=upload_id)
        ledger = list(StockLedger.objects.filter(
            reference_id__in=record_ids, source="SALE_SYNC_ITEM_LEDGER",
            product_id__in={r.product_id for r in records},
        )) if record_ids else []
        # only ledger rows that really belong to these records (same product)
        rec_product = {r.id: r.product_id for r in records}
        ledger = [l for l in ledger if rec_product.get(l.reference_id) == l.product_id]

        self.stdout.write(f"Upload #{log.id}: {log.file_name} ({log.upload_type}, {log.status})")
        self.stdout.write(f"  item sales records : {len(records)}")
        self.stdout.write(f"  bill rows          : {bills.count()}")
        self.stdout.write(f"  stock ledger rows  : {len(ledger)} ({-sum(l.quantity_change for l in ledger)} units to give back)")
        if records:
            dates = sorted({r.sale_date for r in records})
            self.stdout.write(f"  sale dates         : {dates[0]} .. {dates[-1]}")

        if dry:
            self.stdout.write(self.style.WARNING("Dry run - nothing changed."))
            return

        with transaction.atomic():
            touched_batches = set()
            for l in ledger:
                if l.batch_id:
                    b = PurchaseBatch.objects.select_for_update().get(pk=l.batch_id)
                    b.remaining_quantity += -l.quantity_change
                    if b.remaining_quantity > 0 and b.status == "DEPLETED":
                        b.status = "ACTIVE"
                    b.save(update_fields=["remaining_quantity", "status"])
                    touched_batches.add(b.id)
                l.delete()

            # top-up batches that only this upload used: nothing else deducted from them
            removed_topups = 0
            for b in PurchaseBatch.objects.filter(
                    id__in=touched_batches,
                    purchase__invoice_number__startswith="OPENING-STOCK-RECONCILE-"):
                if (b.remaining_quantity == b.quantity_received
                        and not StockLedger.objects.filter(batch=b).exists()):
                    purchase_id = b.purchase_id
                    b.delete()
                    removed_topups += 1
                    if not PurchaseBatch.objects.filter(purchase_id=purchase_id).exists():
                        Purchase.objects.filter(pk=purchase_id).delete()

            ItemSalesRecord.objects.filter(upload_id=upload_id).delete()
            bills.delete()

            try:
                from sales.cash_flow_views import _clear_review
                _clear_review(upload_id)
            except Exception:
                pass
            log.delete()

        self.stdout.write(self.style.SUCCESS(
            f"Rolled back upload #{upload_id}: {len(records)} sales records, "
            f"{len(ledger)} ledger rows, {removed_topups} top-up batch(es) removed."))