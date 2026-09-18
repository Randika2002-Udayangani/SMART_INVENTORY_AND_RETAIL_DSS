import json
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from rest_framework.test import APIClient

from products.models import Product

TEST_PASSWORD = os.environ.get('TEST_PASSWORD', 'testpass123')


class ProductReorderThresholdViewTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(
            product_name='Threshold test product', cost_price=10, unit_price=15,
        )
        self.client = APIClient()

    def test_admin_can_set_a_manual_reorder_threshold(self):
        user = get_user_model().objects.create_user('threshold-admin', password=TEST_PASSWORD)
        admin_group, _ = Group.objects.get_or_create(name='ADMIN')
        user.groups.add(admin_group)
        self.client.force_authenticate(user=user)

        response = self.client.patch(
            f'/api/products/{self.product.id}/reorder-threshold/',
            data=json.dumps({'reorder_threshold': 24}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.reorder_threshold, 24)

    def test_non_admin_cannot_set_a_manual_reorder_threshold(self):
        user = get_user_model().objects.create_user('threshold-staff', password=TEST_PASSWORD)
        self.client.force_authenticate(user=user)

        response = self.client.patch(
            f'/api/products/{self.product.id}/reorder-threshold/',
            data=json.dumps({'reorder_threshold': 24}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 403)