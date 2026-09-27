from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from inventory.models import DiscountRecommendation
from products.models import Product
from purchases.models import Purchase, PurchaseBatch
from suppliers.models import Supplier


class PublicDiscountRecommendationEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='manager', password='secret123')
        self.supplier = Supplier.objects.create(supplier_name='Test Supplier')
        self.product_a = Product.objects.create(
            product_name='Milk 1L',
            unit_price=Decimal('400.00'),
            cost_price=Decimal('280.00'),
            avg_cost_price=Decimal('280.00'),
            is_active=True,
        )
        self.product_b = Product.objects.create(
            product_name='Bread',
            unit_price=Decimal('200.00'),
            cost_price=Decimal('120.00'),
            avg_cost_price=Decimal('120.00'),
            is_active=True,
        )
        purchase = Purchase.objects.create(
            supplier=self.supplier,
            purchase_date=date.today() - timedelta(days=10),
            invoice_number='INV-001',
            total_amount=Decimal('500.00'),
        )
        self.batch_a = PurchaseBatch.objects.create(
            purchase=purchase,
            product=self.product_a,
            quantity_received=20,
            remaining_quantity=20,
            cost_price=Decimal('280.00'),
            expiry_date=date.today() + timedelta(days=10),
            status='ACTIVE',
        )
        self.batch_b = PurchaseBatch.objects.create(
            purchase=purchase,
            product=self.product_b,
            quantity_received=20,
            remaining_quantity=20,
            cost_price=Decimal('120.00'),
            expiry_date=date.today() + timedelta(days=20),
            status='ACTIVE',
        )

    def _jwt_headers(self, user=None):
        token = RefreshToken.for_user(user or self.user)
        return {'HTTP_AUTHORIZATION': f'Bearer {token.access_token}'}

    def test_pending_recommendation_appears_with_minimum_customer_fields(self):
        DiscountRecommendation.objects.create(
            product=self.product_a,
            batch=self.batch_a,
            days_until_expiry=10,
            current_price=Decimal('400.00'),
            recommended_discount_pct=Decimal('20.00'),
            recommended_price=Decimal('320.00'),
            profit_protected=True,
            recovery_sell=Decimal('100.00'),
            recovery_return=Decimal('50.00'),
            best_action='DISCOUNT',
            status='PENDING',
        )

        response = self.client.get('/api/discounts/public/')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload), 1)
        item = payload[0]
        self.assertEqual(item['product_id'], self.product_a.id)
        self.assertEqual(float(item['recommended_discount_pct']), 20.0)
        self.assertEqual(float(item['recommended_price']), 320.0)
        self.assertNotIn('profit_protected', item)
        self.assertNotIn('best_action', item)

    def test_non_pending_recommendations_are_not_exposed(self):
        DiscountRecommendation.objects.create(
            product=self.product_a,
            batch=self.batch_a,
            days_until_expiry=10,
            current_price=Decimal('400.00'),
            recommended_discount_pct=Decimal('20.00'),
            recommended_price=Decimal('320.00'),
            profit_protected=True,
            recovery_sell=Decimal('100.00'),
            recovery_return=Decimal('50.00'),
            best_action='DISCOUNT',
            status='APPLIED',
        )
        DiscountRecommendation.objects.create(
            product=self.product_b,
            batch=self.batch_b,
            days_until_expiry=20,
            current_price=Decimal('200.00'),
            recommended_discount_pct=Decimal('10.00'),
            recommended_price=Decimal('180.00'),
            profit_protected=True,
            recovery_sell=Decimal('80.00'),
            recovery_return=Decimal('40.00'),
            best_action='DISCOUNT',
            status='IGNORED',
        )

        response = self.client.get('/api/discounts/public/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_sensitive_fields_are_not_exposed(self):
        DiscountRecommendation.objects.create(
            product=self.product_a,
            batch=self.batch_a,
            days_until_expiry=10,
            current_price=Decimal('400.00'),
            recommended_discount_pct=Decimal('15.00'),
            recommended_price=Decimal('340.00'),
            profit_protected=True,
            recovery_sell=Decimal('110.00'),
            recovery_return=Decimal('60.00'),
            best_action='DISCOUNT',
            status='PENDING',
        )

        response = self.client.get('/api/discounts/public/')

        self.assertEqual(response.status_code, 200)
        payload = response.json()[0]
        for field in ['profit_protected', 'recovery_sell', 'recovery_return', 'best_action', 'reviewed_by', 'reviewed_at']:
            self.assertNotIn(field, payload)

    def test_public_endpoint_is_read_only(self):
        response = self.client.post('/api/discounts/public/', {})
        self.assertEqual(response.status_code, 405)

        response = self.client.put('/api/discounts/public/', {}, content_type='application/json')
        self.assertEqual(response.status_code, 405)

    def test_existing_staff_endpoint_still_works(self):
        DiscountRecommendation.objects.create(
            product=self.product_a,
            batch=self.batch_a,
            days_until_expiry=10,
            current_price=Decimal('400.00'),
            recommended_discount_pct=Decimal('20.00'),
            recommended_price=Decimal('320.00'),
            profit_protected=True,
            recovery_sell=Decimal('100.00'),
            recovery_return=Decimal('50.00'),
            best_action='DISCOUNT',
            status='PENDING',
        )

        response = self.client.get('/api/discounts/recommendations/', **self._jwt_headers())

        self.assertEqual(response.status_code, 200)
        data = response.json()
        payload = data if isinstance(data, list) else data.get('results', [])
        self.assertGreaterEqual(len(payload), 1)
