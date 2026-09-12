from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import ReorderRecommendation
from orders.models import Notification
from products.models import Product


class ReorderNotificationTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        manager_group = Group.objects.create(name='MANAGER')
        self.manager = get_user_model().objects.create_user(
            username='reorder_manager', password='testpass123'
        )
        self.manager.groups.add(manager_group)
        self.client.force_authenticate(user=self.manager)
        self.product = Product.objects.create(
            product_name='Test Coffee',
            sku_code='TEST-COFFEE',
            unit_price=10,
            cost_price=6,
        )

    def test_ordered_creates_one_shared_notification_per_recommendation(self):
        expected_priorities = {
            'CRITICAL': 'CRITICAL',
            'MEDIUM': 'MEDIUM',
            'NORMAL': 'MEDIUM',
        }

        for urgency, expected_priority in expected_priorities.items():
            with self.subTest(urgency=urgency):
                recommendation = ReorderRecommendation.objects.create(
                    product=self.product,
                    current_stock=2,
                    avg_daily_sales=1,
                    days_of_stock=2,
                    safety_stock=5,
                    suggested_quantity=10,
                    estimated_cost=60,
                    urgency=urgency,
                    status='PENDING',
                )

                response = self.client.patch(
                    f'/api/reorder/recommendations/{recommendation.id}/',
                    {'status': 'ORDERED'},
                    format='json',
                )

                self.assertEqual(response.status_code, 200)
                notification = Notification.objects.get(
                    reference_table='reorder_recommendation',
                    reference_id=recommendation.id,
                )
                self.assertEqual(notification.type, 'REORDER')
                self.assertEqual(notification.priority, expected_priority)
                self.assertIsNone(notification.user)
                self.assertIsNone(notification.customer)
                self.assertIn(self.manager.username, notification.message)
                self.assertIn(self.product.product_name, notification.message)
                self.assertIn(
                    notification,
                    Notification.objects.filter(customer__isnull=True, user=None),
                )

                repeated_response = self.client.patch(
                    f'/api/reorder/recommendations/{recommendation.id}/',
                    {'status': 'ORDERED'},
                    format='json',
                )

                self.assertEqual(repeated_response.status_code, 200)
                self.assertEqual(
                    Notification.objects.filter(
                        reference_table='reorder_recommendation',
                        reference_id=recommendation.id,
                    ).count(),
                    1,
                )

    def test_invalid_urgency_returns_400_without_update_or_notification(self):
        recommendation = ReorderRecommendation.objects.create(
            product=self.product,
            current_stock=2,
            avg_daily_sales=1,
            days_of_stock=2,
            safety_stock=5,
            suggested_quantity=10,
            estimated_cost=60,
            urgency='NOT_MAPPED',
            status='PENDING',
        )

        response = self.client.patch(
            f'/api/reorder/recommendations/{recommendation.id}/',
            {'status': 'ORDERED'},
            format='json',
        )

        recommendation.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertIn('No notification priority mapping', str(response.data))
        self.assertEqual(recommendation.status, 'PENDING')
        self.assertFalse(
            Notification.objects.filter(
                reference_table='reorder_recommendation',
                reference_id=recommendation.id,
            ).exists()
        )