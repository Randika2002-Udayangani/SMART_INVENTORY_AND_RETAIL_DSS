from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from products.models import Product
from sales.models import ItemSalesRecord


class LifecycleSalesSeriesTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(
            product_name='Lifecycle chart product', cost_price=10, unit_price=15,
        )
        ItemSalesRecord.objects.create(
            product=self.product,
            sale_date=date(2026, 1, 3),
            quantity_sold=6,
            unit_price=Decimal('15.00'),
            total_amount=Decimal('90.00'),
        )
        user = get_user_model().objects.create_user('lifecycle-history-user', password='testpass123')
        self.client = APIClient()
        self.client.force_authenticate(user=user)

    def test_history_uses_requested_dates_and_includes_daily_sales(self):
        response = self.client.get(
            f'/api/analytics/lifecycle/{self.product.id}/history/',
            {'date_from': '2026-01-01', 'date_to': '2026-01-04'},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['date_from'], '2026-01-01')
        self.assertEqual(body['date_to'], '2026-01-04')
        self.assertEqual(len(body['sales_series']), 4)
        self.assertEqual(body['sales_series'][2], {
            'date': '2026-01-03', 'sales_velocity': 6.0,
        })
