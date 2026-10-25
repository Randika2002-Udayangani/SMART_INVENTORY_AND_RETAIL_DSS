from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from inventory.models import DiscountRecommendation
from inventory.services.discount_engine import get_customer_visible_discounts
from inventory.views import ActiveDiscountListView
from orders.models import Customer
from products.models import Category, Product
from purchases.models import Purchase, PurchaseBatch
from suppliers.models import Supplier


def _create_active_deal():
    """One valid customer deal: an APPLIED recommendation over an ACTIVE,
    in-stock, not-yet-expired batch of an active product."""
    category = Category.objects.create(category_name='Hair Care')
    product = Product.objects.create(
        product_name='AYUSH ANTI DANDRUFF SHAMPOO 100ml',
        category=category,
        unit_price=Decimal('475.00'), cost_price=Decimal('400.00'),
        is_active=True,
    )
    supplier = Supplier.objects.create(supplier_name='Test Supplier')
    purchase = Purchase.objects.create(
        supplier=supplier, purchase_date=timezone.localdate(),
    )
    batch = PurchaseBatch.objects.create(
        purchase=purchase, product=product,
        quantity_received=10, cost_price=Decimal('400.00'),
        remaining_quantity=10, status='ACTIVE',
        expiry_date=timezone.localdate() + timedelta(days=15),
    )
    recommendation = DiscountRecommendation.objects.create(
        product=product, batch=batch,
        days_until_expiry=15,
        current_price=Decimal('475.00'),
        recommended_discount_pct=Decimal('7.84'),
        recommended_price=Decimal('437.77'),
        recovery_sell=Decimal('470.00'),
        recovery_return=Decimal('440.00'),
        best_action='DISCOUNT',
        status='APPLIED',
    )
    return product, batch, recommendation


class CustomerVisibleDiscountQueryTests(TestCase):
    """get_customer_visible_discounts() inclusion/exclusion rules.

    The shared helper must surface exactly the deals that are real for a
    customer: APPLIED + active product + ACTIVE batch + stock left + not
    past expiry. Stale rows (approved but now dead stock) must drop out.
    """

    def setUp(self):
        self.product, self.batch, self.rec = _create_active_deal()

    def _ids(self):
        return [rec.id for rec in get_customer_visible_discounts()]

    def test_healthy_applied_deal_is_included(self):
        self.assertEqual(self._ids(), [self.rec.id])

    def test_pending_recommendation_is_excluded(self):
        self.rec.status = 'PENDING'
        self.rec.save(update_fields=['status'])
        self.assertEqual(self._ids(), [])

    def test_ignored_recommendation_is_excluded(self):
        self.rec.status = 'IGNORED'
        self.rec.save(update_fields=['status'])
        self.assertEqual(self._ids(), [])

    def test_depleted_batch_is_excluded(self):
        # Recommendation stays APPLIED, but the batch is dead stock.
        self.batch.status = 'DEPLETED'
        self.batch.save(update_fields=['status'])
        self.assertEqual(self._ids(), [])

    def test_zero_stock_batch_is_excluded(self):
        self.batch.remaining_quantity = 0
        self.batch.save(update_fields=['remaining_quantity'])
        self.assertEqual(self._ids(), [])

    def test_expired_batch_is_excluded(self):
        self.batch.expiry_date = timezone.localdate() - timedelta(days=1)
        self.batch.save(update_fields=['expiry_date'])
        self.assertEqual(self._ids(), [])

    def test_batch_without_expiry_date_is_excluded(self):
        self.batch.expiry_date = None
        self.batch.save(update_fields=['expiry_date'])
        self.assertEqual(self._ids(), [])

    def test_inactive_product_is_excluded(self):
        self.product.is_active = False
        self.product.save(update_fields=['is_active'])
        self.assertEqual(self._ids(), [])


class ActiveDiscountAPITests(TestCase):
    """GET /api/discounts/active/ — public and customer-safe."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.product, self.batch, self.rec = _create_active_deal()

    def _get(self, user=None):
        request = self.factory.get('/api/discounts/active/')
        if user is not None:
            force_authenticate(request, user=user)
        return ActiveDiscountListView.as_view()(request)

    def test_url_resolves_to_public_view(self):
        match = resolve('/api/discounts/active/')
        self.assertEqual(match.url_name, 'discount-active-list')

    def test_anonymous_visitor_can_list_active_discounts(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_logged_in_customer_can_list_active_discounts(self):
        customer = Customer.objects.create(
            name='Deal Seeker', email='deals@example.com',
            password_hash='unused',
        )
        response = self._get(user=customer)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_payload_is_customer_safe(self):
        row = self._get().data[0]
        self.assertEqual(set(row.keys()), {
            'product_id', 'product_name', 'category_name', 'brand',
            'original_price', 'discount_percentage', 'discounted_price',
            'is_available', 'expiry_date',
        })
        for internal in (
            'recovery_sell', 'recovery_return', 'recovery_discard',
            'profit_protected', 'best_action', 'status', 'reviewed_by',
            'reviewed_at', 'calculated_date', 'current_price',
            'days_until_expiry', 'batch', 'product',
        ):
            self.assertNotIn(internal, row)

    def test_payload_values_match_fixture(self):
        row = self._get().data[0]
        self.assertEqual(row['product_id'], self.product.id)
        self.assertEqual(row['product_name'], 'AYUSH ANTI DANDRUFF SHAMPOO 100ml')
        self.assertEqual(row['category_name'], 'Hair Care')
        self.assertIsNone(row['brand'])
        # DRF DecimalField/DateField coerce to string in serialized output.
        self.assertEqual(Decimal(str(row['original_price'])), Decimal('475.00'))
        self.assertEqual(Decimal(str(row['discount_percentage'])), Decimal('7.84'))
        self.assertEqual(Decimal(str(row['discounted_price'])), Decimal('437.77'))
        self.assertTrue(row['is_available'])
        self.assertEqual(str(row['expiry_date']), str(self.batch.expiry_date))

    def test_stale_depleted_deal_is_not_returned(self):
        self.batch.status = 'DEPLETED'
        self.batch.remaining_quantity = 0
        self.batch.save(update_fields=['status', 'remaining_quantity'])
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.data), [])
