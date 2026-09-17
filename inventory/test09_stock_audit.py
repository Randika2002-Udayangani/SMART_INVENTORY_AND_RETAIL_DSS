"""
Tests for the stock-audit fixes (2026-09):
    1. Recording a DAMAGE loss now actually reduces stock.
    2. get_available_stock() and get_current_stock() now agree, since
       the former delegates to the latter instead of duplicating logic.

Run with: python manage.py test inventory.test09_stock_audit
"""

from decimal import Decimal
from datetime import date, timedelta

from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework_simplejwt.tokens import RefreshToken

from products.models import Product
from suppliers.models import Supplier
from purchases.models import Purchase, PurchaseBatch
from inventory.models import StockLedger, LossRecord
from inventory.services.stock import get_available_stock
from inventory.services.reorder_logic import get_current_stock


def _jwt_auth_headers(user):
    """SimpleJWT auth for the test client -- force_login() won't work
    here since this API authenticates via JWT, not Django sessions."""
    token = RefreshToken.for_user(user)
    return {'HTTP_AUTHORIZATION': f'Bearer {token.access_token}'}


class DamageLossReducesStockTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='x')
        self.supplier = Supplier.objects.create(
            supplier_name='Test Supplier', contact_number='0770000000'
        )
        self.product = Product.objects.create(
            product_name='Audit Test Product',
            unit_price=Decimal('100.00'),
            cost_price=Decimal('60.00'),
            avg_cost_price=Decimal('60.00'),
            is_active=True,
        )
        self.purchase = Purchase.objects.create(
            supplier=self.supplier,
            purchase_date=date.today() - timedelta(days=30),
            invoice_number='TEST-INV-001',
            total_amount=Decimal('6000.00'),
        )
        self.batch = PurchaseBatch.objects.create(
            purchase=self.purchase,
            product=self.product,
            quantity_received=100,
            remaining_quantity=100,
            cost_price=Decimal('60.00'),
            status='ACTIVE',
        )

    def test_damage_loss_reduces_batch_and_writes_ledger_entry(self):
        stock_before = get_current_stock(self.product.id)
        self.assertEqual(stock_before, 100)

        headers = _jwt_auth_headers(self.user)
        response = self.client.post('/api/losses/', {
            'product_id': self.product.id,
            'loss_type': 'DAMAGE',
            'loss_quantity': 10,
            'notes': 'Audit test damage',
        }, **headers)
        self.assertEqual(response.status_code, 201)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.remaining_quantity, 90)

        stock_after = get_current_stock(self.product.id)
        self.assertEqual(stock_after, 90)

        ledger_entry = StockLedger.objects.filter(
            product=self.product, source='DAMAGE_LOSS'
        ).first()
        self.assertIsNotNone(ledger_entry)
        self.assertEqual(ledger_entry.quantity_change, -10)
        self.assertEqual(ledger_entry.transaction_type, 'MANUAL_ADJUSTMENT')

        loss_record = LossRecord.objects.filter(product=self.product).first()
        self.assertIsNotNone(loss_record)
        self.assertEqual(loss_record.loss_quantity, 10)

    def test_damage_loss_shortfall_does_not_go_negative(self):
        headers = _jwt_auth_headers(self.user)
        response = self.client.post('/api/losses/', {
            'product_id': self.product.id,
            'loss_type': 'DAMAGE',
            'loss_quantity': 150,  # more than the 100 available
        }, **headers)
        self.assertEqual(response.status_code, 201)
        self.assertIn('warning', response.json())

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.remaining_quantity, 0)
        self.assertGreaterEqual(self.batch.remaining_quantity, 0)


class StockCalculationConsistencyTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            supplier_name='Test Supplier 2', contact_number='0770000001'
        )
        self.product = Product.objects.create(
            product_name='Consistency Test Product',
            unit_price=Decimal('50.00'),
            cost_price=Decimal('30.00'),
            avg_cost_price=Decimal('30.00'),
            is_active=True,
        )
        self.purchase = Purchase.objects.create(
            supplier=self.supplier,
            purchase_date=date.today() - timedelta(days=10),
            invoice_number='TEST-INV-002',
            total_amount=Decimal('1500.00'),
        )
        PurchaseBatch.objects.create(
            purchase=self.purchase, product=self.product,
            quantity_received=50, remaining_quantity=50,
            cost_price=Decimal('30.00'), status='ACTIVE',
        )
        PurchaseBatch.objects.create(
            purchase=self.purchase, product=self.product,
            quantity_received=20, remaining_quantity=20,
            cost_price=Decimal('30.00'), status='PENDING_EXPIRY',
        )

    def test_get_available_stock_matches_get_current_stock(self):
        """
        The two functions must now return the same number for the same
        product -- this was the core bug the audit found (two competing
        stock definitions). Both should count ACTIVE + PENDING_EXPIRY.
        """
        available = get_available_stock(self.product.id)
        current = get_current_stock(self.product.id)
        self.assertEqual(available, current)
        self.assertEqual(available, 70)
