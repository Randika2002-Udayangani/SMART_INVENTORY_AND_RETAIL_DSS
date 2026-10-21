from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from inventory.models import DiscountRecommendation
from purchases.models import Purchase, PurchaseBatch
from suppliers.models import Supplier

from .models import Brand, Category, Product


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

		response = APIClient().get(f'/api/products/{self.product.id}/')
		self.assertEqual(response.status_code, 200)
		data = response.data

		self.assertEqual(data['discounted_price'], Decimal('75.00'))
		self.assertEqual(data['discount_percentage'], Decimal('25.00'))
		self.assertEqual(data['discount']['price'], 75.0)

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

		response = APIClient().get(f'/api/products/{self.product.id}/')
		self.assertEqual(response.status_code, 200)
		data = response.data

		self.assertIsNone(data['discounted_price'])
		self.assertIsNone(data['discount_percentage'])

	def test_public_product_list_query_count_does_not_grow_per_product(self):
		category = Category.objects.create(category_name='Performance Test')
		brand = Brand.objects.create(brand_name='Performance Test')
		self.product.category = category
		self.product.brand = brand
		self.product.save(update_fields=['category', 'brand'])
		client = APIClient()

		with CaptureQueriesContext(connection) as one_product_queries:
			response = client.get('/api/products/?page_size=100')
		self.assertEqual(response.status_code, 200)

		Product.objects.bulk_create([
			Product(
				product_name=f'Performance Product {index}',
				unit_price=Decimal('10.00'),
				cost_price=Decimal('5.00'),
				category=category,
				brand=brand,
			)
			for index in range(59)
		])

		with CaptureQueriesContext(connection) as many_product_queries:
			response = client.get('/api/products/?page_size=100')
		self.assertEqual(response.status_code, 200)
		self.assertEqual(len(many_product_queries), len(one_product_queries))


class ProductAvailabilityRestockTests(TestCase):
	def test_future_pending_expiry_restock_is_available_to_customers(self):
		product = Product.objects.create(
			product_name='Restocked Product',
			unit_price=Decimal('50.00'),
			cost_price=Decimal('30.00'),
		)
		supplier = Supplier.objects.create(supplier_name='Restock Supplier')
		purchase = Purchase.objects.create(
			supplier=supplier,
			purchase_date=date.today(),
		)
		expiry_date = date.today() + timedelta(days=60)
		PurchaseBatch.objects.create(
			purchase=purchase,
			product=product,
			quantity_received=20,
			cost_price=Decimal('30.00'),
			expiry_date=date.today() - timedelta(days=1),
			remaining_quantity=20,
			status='ACTIVE',
		)
		PurchaseBatch.objects.create(
			purchase=purchase,
			product=product,
			quantity_received=12,
			cost_price=Decimal('30.00'),
			expiry_date=expiry_date,
			remaining_quantity=12,
			status='PENDING_EXPIRY',
		)

		response = APIClient().get(f'/api/products/{product.id}/availability/')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.data['status'], 'AVAILABLE')
		self.assertEqual(response.data['stock'], 12)
		self.assertEqual(response.data['earliest_expiry'], expiry_date.isoformat())
		product_response = APIClient().get(f'/api/products/{product.id}/')
		self.assertEqual(product_response.status_code, 200)
		self.assertEqual(product_response.data['expiry_date'], expiry_date.isoformat())

		client = APIClient()
		client.force_authenticate(
			user=get_user_model().objects.create_user(username='inventory-test-user')
		)
		inventory_response = client.get('/api/inventory/stock/')
		product_stock = next(
			row for row in inventory_response.data['results']
			if row['product_id'] == product.id
		)
		detail_response = client.get(f'/api/inventory/stock/{product.id}/')

		self.assertEqual(inventory_response.status_code, 200)
		self.assertEqual(detail_response.status_code, 200)
		self.assertEqual(product_stock['current_stock'], response.data['stock'])
		self.assertEqual(
			detail_response.data['total_current_stock'], response.data['stock']
		)

	def test_non_perishable_batch_without_expiry_is_counted_as_available(self):
		product = Product.objects.create(
			product_name='Rice Pack',
			unit_price=Decimal('120.00'),
			cost_price=Decimal('80.00'),
		)
		supplier = Supplier.objects.create(supplier_name='Dry Goods Supplier')
		purchase = Purchase.objects.create(
			supplier=supplier,
			purchase_date=date.today(),
		)
		PurchaseBatch.objects.create(
			purchase=purchase,
			product=product,
			quantity_received=9,
			cost_price=Decimal('80.00'),
			expiry_date=None,
			remaining_quantity=9,
			status='ACTIVE',
		)

		response = APIClient().get(f'/api/products/{product.id}/availability/')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.data['status'], 'LIMITED_STOCK')
		self.assertEqual(response.data['stock'], 9)
		self.assertIsNone(response.data['earliest_expiry'])
		product_response = APIClient().get(f'/api/products/{product.id}/')
		self.assertEqual(product_response.status_code, 200)
		self.assertIsNone(product_response.data['expiry_date'])
