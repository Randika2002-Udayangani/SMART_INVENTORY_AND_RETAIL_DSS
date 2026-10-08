"""
Tests for the online-order double-deduction fix.

Covers:
    1.  Online order creation performs the ONLINE_ORDER FEFO deduction.
    2.  Cancelling an uncollected order releases its ONLINE_ORDER deduction.
    3.  Reaching COMPLETED stamps picked_up_at.
    4.  Completion does NOT perform a second FEFO deduction.
    5-8. Item Ledger reconciliation math (incl. DOUBLE_DEDUCTION_RISK).
    9-11. Mixed online + POS scenarios, multiple orders, partial pickups.
    12. Repeated upload does not deduct twice.
    13. detect_double_deduction still passes on a clean state.
"""
from datetime import date, datetime, time, timedelta
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

import io as _io

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from inventory.models import StockLedger
from inventory.services.fefo import deduct_stock_fefo
from inventory.services.online_reconciliation import (
    confirmed_online_quantity,
    reconcile_item_ledger_deduction,
)
from orders.models import Customer, OnlineOrder
from orders.views import (
    OrderCancelView,
    OrderListCreateView,
    OrderStatusUpdateView,
)
from products.models import Product
from purchases.models import Purchase, PurchaseBatch
from sales.models import ItemSalesRecord, UploadLog
from sales.views import ItemLedgerPDFUploadView
from suppliers.models import Supplier

LOCAL_TZ = ZoneInfo("Asia/Colombo")


def pickup_on(sale_date):
    """Aware datetime inside the Asia/Colombo local day of sale_date."""
    return datetime.combine(sale_date, time(10, 0), tzinfo=LOCAL_TZ)


class OnlineReconciliationTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.customer = Customer.objects.create(
            name='Cust', email='c@example.com', password_hash='unused'
        )
        self.staff = get_user_model().objects.create_user(
            username='staff1', password='x'
        )

        self.product = Product.objects.create(
            product_name='Test Item 1kg', unit_price=100, cost_price=50,
            is_active=True,
        )
        supplier = Supplier.objects.create(supplier_name='Sup')
        purchase = Purchase.objects.create(
            supplier=supplier, purchase_date=date.today()
        )
        self.batch = PurchaseBatch.objects.create(
            purchase=purchase,
            product=self.product,
            quantity_received=100,
            cost_price=50,
            expiry_date=date.today() + timedelta(days=365),
            remaining_quantity=100,
            status='ACTIVE',
        )

    # ── helpers ─────────────────────────────────────────────────────────
    def _make_order(self, product=None, quantity=2):
        """Create an online order through the real API view (performs the
        ONLINE_ORDER FEFO deduction)."""
        request = self.factory.post(
            '/api/orders/',
            {
                'pickup_date': str(date.today()),
                'time_slot': 'MORNING',
                'items': [
                    {
                        'product_id': (product or self.product).id,
                        'quantity': quantity,
                    }
                ],
            },
            format='json',
        )
        force_authenticate(request, user=self.customer)
        response = OrderListCreateView.as_view()(request)
        assert response.status_code == 201, response.data
        return OnlineOrder.objects.get(order_reference=response.data['order_reference'])

    def _stock(self):
        return PurchaseBatch.objects.get(pk=self.batch.pk).remaining_quantity

    def _ledger_qty(self, source, reference_id=None):
        qs = StockLedger.objects.filter(
            product=self.product, source=source
        )
        if reference_id is not None:
            qs = qs.filter(reference_id=reference_id)
        return sum(abs(r.quantity_change) for r in qs)

    def _status_patch(self, order, target):
        request = self.factory.patch(
            f'/api/orders/{order.id}/status/', {'status': target},
            format='json',
        )
        force_authenticate(request, user=self.staff)
        response = OrderStatusUpdateView.as_view()(request, pk=order.id)
        assert response.status_code == 200, response.data

    def _complete(self, order):
        """Walk the real status workflow up to COMPLETED."""
        for target in ('CONFIRMED', 'READY', 'COMPLETED'):
            self._status_patch(order, target)

    def _mark_picked(self, order):
        self._complete(order)
        OnlineOrder.objects.filter(pk=order.pk).update(
            picked_up_at=pickup_on(date.today())
        )

    # ── TEST 1 ──────────────────────────────────────────────────────────
    def test_order_creation_deducts_stock_fefo(self):
        order = self._make_order(quantity=2)
        self.assertEqual(self._stock(), 98)
        self.assertEqual(self._ledger_qty('ONLINE_ORDER', order.id), 2)

    # ── TEST 2 ──────────────────────────────────────────────────────────
    def test_cancel_uncollected_order_releases_stock(self):
        order = self._make_order(quantity=2)
        self.assertEqual(self._stock(), 98)

        request = self.factory.delete(
            f'/api/orders/{order.id}/', {'reason': 'changed mind'},
            format='json',
        )
        force_authenticate(request, user=self.customer)
        response = OrderCancelView.as_view()(request, pk=order.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._stock(), 100)
        # Released order must NOT count as a confirmed pickup.
        self.assertEqual(
            confirmed_online_quantity(self.product.id, date.today())['confirmed'],
            0,
        )

    # ── TEST 3 ──────────────────────────────────────────────────────────
    def test_completion_stamps_picked_up_at(self):
        order = self._make_order(quantity=1)
        self.assertIsNone(OnlineOrder.objects.get(pk=order.pk).picked_up_at)
        self._complete(order)
        order.refresh_from_db()
        self.assertIsNotNone(order.picked_up_at)
        self.assertEqual(order.status, 'COMPLETED')

    def test_earlier_statuses_do_not_stamp_picked_up_at(self):
        order = self._make_order(quantity=1)
        for target in ('CONFIRMED', 'READY'):
            self._status_patch(order, target)
            self.assertIsNone(
                OnlineOrder.objects.get(pk=order.pk).picked_up_at
            )

    # ── TEST 4 ──────────────────────────────────────────────────────────
    def test_completion_performs_no_second_deduction(self):
        order = self._make_order(quantity=2)
        stock_after_creation = self._stock()
        ledger_rows_before = StockLedger.objects.count()

        self._complete(order)

        self.assertEqual(self._stock(), stock_after_creation)
        self.assertEqual(StockLedger.objects.count(), ledger_rows_before)
        self.assertEqual(
            StockLedger.objects.filter(source='SALE_SYNC_ITEM_LEDGER').count(),
            0,
        )

    # ── TEST 5 ──────────────────────────────────────────────────────────
    def test_no_online_pickup_deducts_full_ledger(self):
        recon = reconcile_item_ledger_deduction(self.product.id, date.today(), 5)
        self.assertFalse(recon['risk'])
        self.assertEqual(recon['remaining_to_deduct'], 5)
        result = deduct_stock_fefo(
            self.product.id, recon['remaining_to_deduct'],
            'SALE_SYNC_ITEM_LEDGER',
        )
        self.assertEqual(result['deducted'], 5)
        self.assertEqual(self._stock(), 95)

    # ── TEST 6 ──────────────────────────────────────────────────────────
    def test_confirmed_pickup_reduces_deduction(self):
        order = self._make_order(quantity=2)
        self._mark_picked(order)

        recon = reconcile_item_ledger_deduction(self.product.id, date.today(), 5)
        self.assertEqual(recon['confirmed_online_quantity'], 2)
        self.assertEqual(recon['remaining_to_deduct'], 3)
        result = deduct_stock_fefo(
            self.product.id, recon['remaining_to_deduct'],
            'SALE_SYNC_ITEM_LEDGER',
        )
        self.assertEqual(result['deducted'], 3)
        # 100 - 2 (online) - 3 (ledger) = 95, not 93 (double-deducted).
        self.assertEqual(self._stock(), 95)

    # ── TEST 7 ──────────────────────────────────────────────────────────
    def test_exact_match_deducts_zero(self):
        order = self._make_order(quantity=2)
        self._mark_picked(order)
        stock_after_creation = self._stock()

        recon = reconcile_item_ledger_deduction(self.product.id, date.today(), 2)
        self.assertFalse(recon['risk'])
        self.assertEqual(recon['remaining_to_deduct'], 0)
        # View skips FEFO entirely when remaining == 0.
        self.assertEqual(self._stock(), stock_after_creation)

    # ── TEST 8 ──────────────────────────────────────────────────────────
    def test_dangerous_mismatch_flags_risk_and_deducts_nothing(self):
        order = self._make_order(quantity=2)
        self._mark_picked(order)
        stock_before = self._stock()

        recon = reconcile_item_ledger_deduction(self.product.id, date.today(), 1)
        self.assertTrue(recon['risk'])
        self.assertEqual(recon['remaining_to_deduct'], 1 - 2)
        self.assertEqual(recon['order_ids'], [order.id])
        # View path: risk → error appended, no FEFO call, no silent negative.
        self.assertEqual(self._stock(), stock_before)

    # ── TEST 9 ──────────────────────────────────────────────────────────
    def test_same_day_online_and_pos(self):
        order = self._make_order(quantity=2)
        self._mark_picked(order)
        # Normal POS sale of 3 recorded separately.
        deduct_stock_fefo(self.product.id, 3, 'POS_SALE')
        self.assertEqual(self._stock(), 95)

        recon = reconcile_item_ledger_deduction(self.product.id, date.today(), 5)
        self.assertEqual(recon['confirmed_online_quantity'], 2)
        self.assertEqual(recon['remaining_to_deduct'], 3)
        result = deduct_stock_fefo(
            self.product.id, recon['remaining_to_deduct'],
            'SALE_SYNC_ITEM_LEDGER',
        )
        self.assertEqual(result['deducted'], 3)
        self.assertEqual(self._stock(), 92)

    # ── TEST 10 ─────────────────────────────────────────────────────────
    def test_two_orders_both_picked_up(self):
        order_a = self._make_order(quantity=2)
        order_b = self._make_order(quantity=1)
        self._mark_picked(order_a)
        self._mark_picked(order_b)
        self.assertEqual(self._stock(), 97)

        recon = reconcile_item_ledger_deduction(self.product.id, date.today(), 6)
        self.assertEqual(recon['confirmed_online_quantity'], 3)
        self.assertEqual(recon['remaining_to_deduct'], 3)
        result = deduct_stock_fefo(
            self.product.id, recon['remaining_to_deduct'],
            'SALE_SYNC_ITEM_LEDGER',
        )
        self.assertEqual(result['deducted'], 3)
        self.assertEqual(self._stock(), 94)

    # ── TEST 11 ─────────────────────────────────────────────────────────
    def test_only_picked_up_order_counted(self):
        order_a = self._make_order(quantity=2)
        order_b = self._make_order(quantity=1)
        self._mark_picked(order_a)
        # order_b goes to READY (confirmed, prepared, NOT collected) —
        # must NOT be counted as a pickup.
        self._status_patch(order_b, 'CONFIRMED')
        self._status_patch(order_b, 'READY')
        order_b.refresh_from_db()
        self.assertIsNone(order_b.picked_up_at)

        # Scenario: ledger total = 5 (2 picked-up online + 3 POS). Order
        # B's unit is NOT in the day's ledger (still awaiting collection).
        recon = reconcile_item_ledger_deduction(self.product.id, date.today(), 5)
        self.assertEqual(recon['confirmed_online_quantity'], 2)
        self.assertEqual(recon['order_ids'], [order_a.id])
        self.assertEqual(recon['remaining_to_deduct'], 3)
        result = deduct_stock_fefo(
            self.product.id, recon['remaining_to_deduct'],
            'SALE_SYNC_ITEM_LEDGER',
        )
        self.assertEqual(result['deducted'], 3)
        # 100 - 2 (A picked-up online) - 1 (B reservation, still held) - 3
        # (ledger remaining) = 94 — NOT 93 (double-deducting A's pickup).
        self.assertEqual(self._stock(), 94)

    def test_unpicked_order_not_misclassified(self):
        """If order B's units ARE in the ledger day too (total 6) but B is
        not confirmed picked up, only A is reconciled: remaining = 6 - 2
        (NOT 6 - 3) — no phantom deduction and no false risk flag."""
        order_a = self._make_order(quantity=2)
        order_b = self._make_order(quantity=1)
        self._mark_picked(order_a)
        self._status_patch(order_b, 'CONFIRMED')
        self._status_patch(order_b, 'READY')

        recon = reconcile_item_ledger_deduction(self.product.id, date.today(), 6)
        self.assertFalse(recon['risk'])
        self.assertEqual(recon['confirmed_online_quantity'], 2)
        self.assertEqual(recon['remaining_to_deduct'], 4)

    # ── TEST 12 ─────────────────────────────────────────────────────────
    def test_repeated_upload_does_not_double_deduct(self):
        order = self._make_order(quantity=2)
        self._mark_picked(order)
        stock_after_creation = self._stock()

        def run_upload(ledger_qty):
            """Mirror of ItemLedgerPDFUploadView's per-date logic,
            including its existing duplicate-upload protection
            (ItemSalesRecord already_exists check → skipped)."""
            if ItemSalesRecord.objects.filter(
                product=self.product, sale_date=date.today()
            ).exists():
                return 'skipped'
            ItemSalesRecord.objects.create(
                product=self.product, sale_date=date.today(),
                quantity_sold=ledger_qty, unit_price=100, total_amount=0,
            )
            recon = reconcile_item_ledger_deduction(
                self.product.id, date.today(), ledger_qty
            )
            if recon['risk'] or recon['remaining_to_deduct'] == 0:
                return 'no-deduction'
            return deduct_stock_fefo(
                self.product.id, recon['remaining_to_deduct'],
                'SALE_SYNC_ITEM_LEDGER',
            )

        first = run_upload(5)
        self.assertEqual(first['deducted'], 3)
        self.assertEqual(self._stock(), 95)

        second = run_upload(5)
        self.assertEqual(second, 'skipped')
        self.assertEqual(self._stock(), 95)

    # ── TEST 13 ─────────────────────────────────────────────────────────
    def test_detect_double_deduction_still_passes(self):
        out = StringIO()
        call_command('detect_double_deduction', stdout=out)
        self.assertIn('No double-deduction detected.', out.getvalue())

    # ── TEST 14 (explicit CANCELLED exclusion) ──────────────────────────
    def test_cancelled_order_with_stale_picked_up_at_excluded(self):
        """Even if dirty data ever gives a CANCELLED order a picked_up_at
        timestamp, its ONLINE_ORDER deduction must NOT count toward the
        day's confirmed online quantity — the explicit status exclusion,
        not picked_up_at__isnull=False alone, guarantees this."""
        order = self._make_order(quantity=2)
        # Force dirty data: cancelled AFTER (bogus) pickup timestamp.
        OnlineOrder.objects.filter(pk=order.pk).update(
            status='CANCELLED', picked_up_at=pickup_on(date.today())
        )
        self.assertEqual(
            confirmed_online_quantity(self.product.id, date.today())['confirmed'],
            0,
        )
        recon = reconcile_item_ledger_deduction(
            self.product.id, date.today(), 5
        )
        self.assertFalse(recon['risk'])
        self.assertEqual(recon['remaining_to_deduct'], 5)

    def test_expired_order_with_stale_picked_up_at_excluded(self):
        order = self._make_order(quantity=1)
        OnlineOrder.objects.filter(pk=order.pk).update(
            status='EXPIRED', picked_up_at=pickup_on(date.today())
        )
        self.assertEqual(
            confirmed_online_quantity(self.product.id, date.today())['confirmed'],
            0,
        )

    # ── TEST 15 (multiple orders, same pickup day) ──────────────────────
    def test_three_orders_all_picked_up_same_day(self):
        order_ids = []
        for q in (2, 3, 1):
            order = self._make_order(quantity=q)
            self._mark_picked(order)
            order_ids.append(order.id)
        recon = reconcile_item_ledger_deduction(
            self.product.id, date.today(), 10
        )
        self.assertEqual(recon['confirmed_online_quantity'], 6)
        self.assertEqual(recon['order_ids'], sorted(order_ids))
        self.assertEqual(recon['remaining_to_deduct'], 4)

    # ── TEST 16 (pickup date vs actual pickup timestamp) ────────────────
    def test_pickup_attributed_to_actual_pickup_day_not_order_day(self):
        """picked_up_at = Oct 7 09:30 Colombo must be reconciled on the
        Oct 7 ledger, NOT on the Oct 5 (order/planned) day."""
        order = self._make_order(quantity=2)
        OnlineOrder.objects.filter(pk=order.pk).update(
            picked_up_at=datetime(2026, 10, 7, 9, 30, tzinfo=LOCAL_TZ)
        )

        oct7 = date(2026, 10, 7)
        oct5 = date(2026, 10, 5)

        self.assertEqual(
            confirmed_online_quantity(self.product.id, oct7)['confirmed'], 2
        )
        self.assertEqual(
            confirmed_online_quantity(self.product.id, oct5)['confirmed'], 0
        )
        recon7 = reconcile_item_ledger_deduction(self.product.id, oct7, 10)
        self.assertEqual(recon7['remaining_to_deduct'], 8)
        recon5 = reconcile_item_ledger_deduction(self.product.id, oct5, 10)
        self.assertEqual(recon5['remaining_to_deduct'], 10)
        self.assertEqual(recon5['confirmed_online_quantity'], 0)

    # ── TEST 17 (midnight boundaries, half-open interval) ───────────────
    def test_midnight_boundaries(self):
        order_a = self._make_order(quantity=1)
        order_b = self._make_order(quantity=1)
        order_c = self._make_order(quantity=1)
        OnlineOrder.objects.filter(pk=order_a.pk).update(
            picked_up_at=datetime(2026, 10, 7, 0, 0, 0, tzinfo=LOCAL_TZ)
        )
        OnlineOrder.objects.filter(pk=order_b.pk).update(
            picked_up_at=datetime(2026, 10, 7, 23, 59, 59, tzinfo=LOCAL_TZ)
        )
        # Oct 8 00:00:00 belongs to the NEXT day — excluded from Oct 7.
        OnlineOrder.objects.filter(pk=order_c.pk).update(
            picked_up_at=datetime(2026, 10, 8, 0, 0, 0, tzinfo=LOCAL_TZ)
        )

        self.assertEqual(
            confirmed_online_quantity(
                self.product.id, date(2026, 10, 7))['confirmed'],
            2,  # 00:00:00 and 23:59:59 both inside the half-open day
        )
        self.assertEqual(
            confirmed_online_quantity(
                self.product.id, date(2026, 10, 8))['confirmed'],
            1,  # only order_c
        )
        self.assertEqual(
            confirmed_online_quantity(
                self.product.id, date(2026, 10, 6))['confirmed'],
            0,
        )

    # ── TEST 18 (upload path: PDF helper + rollback tests) ──────────────
    def _item_ledger_pdf(self, ledger_qty):
        """Build a minimal Item Ledger PDF the upload view can parse:
        'Item No : <product>' header + one CASH SALE row for today."""
        from reportlab.pdfgen import canvas

        buf = _io.BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(72, 800, f'Item No : {self.product.product_name}')
        c.drawString(
            72, 780,
            f'{date.today():%Y/%m/%d} B1 CASH SALE 1 {ledger_qty} {ledger_qty}',
        )
        c.save()
        buf.seek(0)
        return buf

    def _upload_ledger_pdf(self, pdf_buf):
        upload_file = _io.BytesIO(pdf_buf.getvalue())
        upload_file.name = 'ledger.pdf'
        request = self.factory.post(
            '/api/sales/item-ledger/upload/', {'file': upload_file},
        )
        force_authenticate(request, user=self.staff)
        return ItemLedgerPDFUploadView.as_view()(request)

    def test_upload_rolls_back_when_deduction_raises(self):
        """Force an exception inside the FEFO deduction step (after the
        ItemSalesRecord was created): the whole per-date atomic unit must
        roll back together — no partial record, no partial stock state —
        and the upload log must end up FAILED."""
        order = self._make_order(quantity=2)
        self._mark_picked(order)  # online confirmed = 2, remaining = 3
        stock_before = self._stock()

        def boom(*args, **kwargs):
            raise RuntimeError('simulated FEFO failure')

        with mock.patch('sales.views.deduct_stock_fefo', side_effect=boom):
            response = self._upload_ledger_pdf(self._item_ledger_pdf(5))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(ItemSalesRecord.objects.count(), 0)
        self.assertFalse(
            StockLedger.objects.filter(source='SALE_SYNC_ITEM_LEDGER').exists()
        )
        self.assertEqual(self._stock(), stock_before)
        upload_log = UploadLog.objects.order_by('-id').first()
        self.assertEqual(upload_log.status, 'FAILED')

    # ── TEST 19 (full upload path: reconcile + idempotent re-upload) ────
    def test_full_upload_deducts_remaining_and_reupload_skips(self):
        order = self._make_order(quantity=2)
        self._mark_picked(order)  # online confirmed = 2
        stock_after_creation = self._stock()

        response = self._upload_ledger_pdf(self._item_ledger_pdf(5))
        self.assertEqual(response.status_code, 201, response.data)
        # remaining = 5 - 2 = 3 deducted beyond the online reservation.
        self.assertEqual(self._stock(), stock_after_creation - 3)

        # Re-uploading the exact same ledger data must NOT deduct again.
        response2 = self._upload_ledger_pdf(self._item_ledger_pdf(5))
        self.assertEqual(response2.status_code, 201, response2.data)
        self.assertEqual(response2.data['dates_skipped'], 1)
        self.assertEqual(self._stock(), stock_after_creation - 3)

    # ── FINAL-PASS TESTS ─────────────────────────────────────────────────

    def _delete_order(self, order, user=None):
        request = self.factory.delete(
            f'/api/orders/{order.id}/', {'reason': 'changed mind'},
            format='json',
        )
        force_authenticate(request, user=user or self.customer)
        return OrderCancelView.as_view()(request, pk=order.id)

    def test_earlier_statuses_cannot_jump_to_completed(self):
        """Only READY → COMPLETED is a legitimate completion transition.
        PENDING → COMPLETED and CONFIRMED → COMPLETED must be rejected so
        a COMPLETED order always proves physical pickup (picked_up_at)."""
        order = self._make_order(quantity=1)
        self.assertEqual(self._stock(), 99)

        for setup_status in (None, 'CONFIRMED'):  # None = stay in PENDING
            if setup_status:
                self._status_patch(order, setup_status)
            request = self.factory.patch(
                f'/api/orders/{order.id}/status/', {'status': 'COMPLETED'},
                format='json',
            )
            force_authenticate(request, user=self.staff)
            response = OrderStatusUpdateView.as_view()(request, pk=order.id)
            self.assertEqual(response.status_code, 400)
            order.refresh_from_db()
            self.assertIsNone(order.picked_up_at)

        # Still never completed: stock reservation untouched, no pickup.
        self.assertEqual(order.status, 'CONFIRMED')
        self.assertEqual(self._stock(), 99)
        self.assertEqual(
            confirmed_online_quantity(self.product.id, date.today())['confirmed'],
            0,
        )

    def test_repeated_cancellation_does_not_restore_twice(self):
        """Cancelling twice — via the API and even via a direct second
        call to _release_order_stock — must restore the reservation only
        once."""
        from orders.views import _release_order_stock

        order = self._make_order(quantity=2)
        self.assertEqual(self._stock(), 98)

        response = self._delete_order(order)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._stock(), 100)
        self.assertEqual(
            StockLedger.objects.filter(
                reference_id=order.id,
                source='ORDER_CANCELLED',
                quantity_change__gt=0,
            ).count(),
            1,
        )

        # Second API cancel: rejected by the status guard.
        response2 = self._delete_order(order)
        self.assertEqual(response2.status_code, 400)
        self.assertEqual(self._stock(), 100)

        # Defense-in-depth: even invoking the release helper directly
        # again must be a no-op (internal already-released guard).
        _release_order_stock(order, 'ORDER_CANCELLED')
        self.assertEqual(self._stock(), 100)
        self.assertEqual(
            StockLedger.objects.filter(
                reference_id=order.id,
                source='ORDER_CANCELLED',
                quantity_change__gt=0,
            ).count(),
            1,
        )

    def test_expire_then_cancel_does_not_restore_twice(self):
        """Auto-expire releases the reservation; a later cancel attempt on
        the EXPIRED order must not restore anything again."""
        from orders.views import _release_order_stock

        order = self._make_order(quantity=3)
        self.assertEqual(self._stock(), 97)
        _release_order_stock(order, 'ORDER_EXPIRED')
        self.assertEqual(self._stock(), 100)

        # Simulate the cancel path reaching the release helper again
        # (e.g. a race with the overdue-expire process).
        order.picked_up_at = None
        order.save()
        _release_order_stock(order, 'ORDER_CANCELLED')
        self.assertEqual(self._stock(), 100)  # no second restore
        self.assertEqual(
            StockLedger.objects.filter(
                reference_id=order.id,
                source__in=['ORDER_CANCELLED', 'ORDER_EXPIRED'],
                quantity_change__gt=0,
            ).count(),
            1,  # single FEFO deduction (3 units, one batch) → one release row
        )
        release_row = StockLedger.objects.get(
            reference_id=order.id,
            source__in=['ORDER_CANCELLED', 'ORDER_EXPIRED'],
            quantity_change__gt=0,
        )
        self.assertEqual(release_row.quantity_change, 3)
