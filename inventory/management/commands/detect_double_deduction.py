"""
Detection-only check for the online-order double-deduction bug.

Root cause (confirmed, not yet fixed): deduct_stock_fefo(source='ONLINE_ORDER')
fires at order creation. Separately, the Item Ledger upload pipeline calls
deduct_stock_fefo(source='SALE_SYNC_ITEM_LEDGER') for every cashier sale, with
no awareness of online orders. Since there's no delivery system, online order
pickups are rung up at the counter like a normal sale -- meaning the same
physical units can be deducted twice: once at order creation, once again when
that pickup's sale is uploaded via Item Ledger.

This command does NOT fix anything -- it only flags products where both
deduction sources hit on the same day, so a real occurrence is caught
instead of silently corrupting stock counts. Safe to run repeatedly;
makes no writes to StockLedger, PurchaseBatch, or ItemSalesRecord.

USAGE:
    python manage.py detect_double_deduction
"""

from django.core.management.base import BaseCommand
from inventory.models import StockLedger
from inventory.services.notifications import create_notification


class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        online_deductions = StockLedger.objects.filter(source='ONLINE_ORDER')

        flagged = []
        for online in online_deductions:
            same_day_upload = StockLedger.objects.filter(
                product_id=online.product_id,
                source='SALE_SYNC_ITEM_LEDGER',
                transaction_date__date=online.transaction_date.date(),
            ).exists()
            if same_day_upload:
                flagged.append(online.product_id)

        flagged = set(flagged)

        if not flagged:
            self.stdout.write("No double-deduction detected.")
            return

        for product_id in flagged:
            create_notification(
                type='DOUBLE_DEDUCTION_RISK', priority='HIGH',
                title='Possible duplicate stock deduction',
                message=f'Product {product_id} has both an ONLINE_ORDER and '
                         f'SALE_SYNC_ITEM_LEDGER deduction on the same day.',
                reference_table='product', reference_id=product_id,
            )
        self.stdout.write(f"Flagged {len(flagged)} product(s) — see notifications.")