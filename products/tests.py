from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from inventory.models import DiscountRecommendation
from purchases.models import Purchase, PurchaseBatch
from suppliers.models import Supplier

from .models import Product
from .serializers import ProductPublicSerializer


class ProductDiscountSerializerTests(TestCase):
	def setUp(self):
		self.product = Product.objects.create(
			product_name='Near Expiry Milk',
			unit_price=Decimal('100.00'),
			cost_price=Decimal('60.00'),
		)
		supplier = Supplier.objects.create(supplier_name='Test Supplier')
		purchase = Purchase.objects.create(
			supplier=supplier,
			purchase_date=date.today(),
		)
		self.batch = PurchaseBatch.objects.create(
			purchase=purchase,
			product=self.product,
			quantity_received=10,
			cost_price=Decimal('60.00'),
			expiry_date=date.today() + timedelta(days=5),
			remaining_quantity=10,
			status='ACTIVE',
		)

	def test_public_serializer_exposes_eligible_discount(self):
		DiscountRecommendation.objects.create(
			product=self.product,
			batch=self.batch,
			days_until_expiry=5,
			current_price=Decimal('100.00'),
			recommended_discount_pct=Decimal('25.00'),
			recommended_price=Decimal('75.00'),
			recovery_sell=Decimal('750.00'),
			recovery_return=Decimal('600.00'),
			recovery_discard=Decimal('0.00'),
			best_action='DISCOUNT',
			status='PENDING',
		)

		data = ProductPublicSerializer(self.product).data

		self.assertEqual(data['discounted_price'], Decimal('75.00'))
		self.assertEqual(data['discount_percentage'], Decimal('25.00'))

	def test_ignored_discount_is_not_exposed(self):
		DiscountRecommendation.objects.create(
			product=self.product,
			batch=self.batch,
			days_until_expiry=5,
			current_price=Decimal('100.00'),
			recommended_discount_pct=Decimal('25.00'),
			recommended_price=Decimal('75.00'),
			recovery_sell=Decimal('750.00'),
			recovery_return=Decimal('600.00'),
			recovery_discard=Decimal('0.00'),
			best_action='DISCOUNT',
			status='IGNORED',
		)

		data = ProductPublicSerializer(self.product).data

		self.assertIsNone(data['discounted_price'])
		self.assertIsNone(data['discount_percentage'])
