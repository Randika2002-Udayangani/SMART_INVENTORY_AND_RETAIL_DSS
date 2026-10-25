"""
The Audit Log page must say WHO did each action, not "Unknown":
staff user, customer (with name), not-signed-in (failed login) or System.
"""
from datetime import date

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from orders.models import Customer, OnlineOrder
from users.audit import resolve_actors
from users.models import AuditLog


def _admin(username='actor-admin'):
    user = User.objects.create_user(username=username, password='Passw0rd-123')
    user.is_staff = True
    user.is_superuser = True
    user.save()
    user.groups.add(Group.objects.get_or_create(name='ADMIN')[0])
    return user


def _order(customer, reference='ORD-T-0001'):
    return OnlineOrder.objects.create(
        customer=customer, pickup_date=date(2026, 12, 1), pickup_time_slot='MORNING',
        order_reference=reference, status='PENDING', collection_deadline=date(2026, 12, 3),
    )


class ResolveActorsTests(TestCase):
    def setUp(self):
        self.admin = _admin()
        self.customer = Customer.objects.create(
            name='Aruni Perera', email='aruni@example.com', password_hash='x'
        )

    def _label(self, log):
        return resolve_actors([log])[log.id]

    def test_staff_row_shows_the_username(self):
        log = AuditLog.objects.create(user=self.admin, action='UPDATE', table_name='product', record_id=1)
        self.assertEqual(self._label(log), ('STAFF', 'actor-admin'))

    def test_customer_order_row_shows_customer_id_and_name(self):
        log = AuditLog.objects.create(
            user=None, action='CREATE', table_name='online_order', record_id=5,
            new_value={'customer_id': self.customer.id, 'total_amount': '500.00'},
        )
        self.assertEqual(
            self._label(log), ('CUSTOMER', f'Customer #{self.customer.id} – Aruni Perera')
        )

    def test_customer_cancel_row_is_resolved_through_the_order(self):
        order = _order(self.customer)
        log = AuditLog.objects.create(
            user=None, action='ORDER_CANCELLED', table_name='online_order',
            record_id=order.id, old_value={'status': 'PENDING'}, new_value={'status': 'CANCELLED'},
        )
        self.assertEqual(
            self._label(log), ('CUSTOMER', f'Customer #{self.customer.id} – Aruni Perera')
        )

    def test_deleted_customer_is_labelled_not_unknown(self):
        log = AuditLog.objects.create(
            user=None, action='CREATE', table_name='online_order', record_id=5,
            new_value={'customer_id': 99999},
        )
        self.assertEqual(self._label(log), ('CUSTOMER', 'Deleted customer #99999'))

    def test_customer_row_without_any_details_is_labelled_unavailable(self):
        log = AuditLog.objects.create(
            user=None, action='ORDER_CANCELLED', table_name='online_order',
            record_id=424242, new_value={'status': 'CANCELLED'},
        )
        self.assertEqual(self._label(log), ('CUSTOMER', 'Customer (details unavailable)'))

    def test_failed_login_for_unknown_user_is_not_signed_in(self):
        log = AuditLog.objects.create(
            user=None, action='LOGIN_FAILED', table_name='auth_user',
            new_value={'username': 'nobody-here', 'failed_login_count': None},
        )
        self.assertEqual(self._label(log), ('ANONYMOUS', 'Not signed in (tried: nobody-here)'))

    def test_other_rows_without_a_user_are_system(self):
        log = AuditLog.objects.create(user=None, action='CALCULATE', table_name='inventory_health_score')
        self.assertEqual(self._label(log), ('SYSTEM', 'System'))


class AuditLogApiActorTests(TestCase):
    def setUp(self):
        self.admin = _admin()
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.customer = Customer.objects.create(
            name='Aruni Perera', email='aruni2@example.com', password_hash='x'
        )

    def test_list_and_detail_include_actor_fields(self):
        log = AuditLog.objects.create(
            user=None, action='CREATE', table_name='online_order', record_id=7,
            new_value={'customer_id': self.customer.id},
        )
        listing = self.client.get('/api/audit-log/').data['results']
        row = next(r for r in listing if r['id'] == log.id)
        self.assertEqual(row['actor_type'], 'CUSTOMER')
        self.assertIn('Aruni Perera', row['actor_label'])
        self.assertIsNone(row['username'])      # existing field is unchanged
        self.assertIsNone(row['user_id'])

        detail = self.client.get(f'/api/audit-log/{log.id}/').data
        self.assertEqual(detail['actor_type'], 'CUSTOMER')
        self.assertIn('Aruni Perera', detail['actor_label'])

    def test_staff_rows_keep_username_and_user_id(self):
        AuditLog.objects.create(user=self.admin, action='UPDATE', table_name='product', record_id=1)
        row = self.client.get('/api/audit-log/').data['results'][0]
        self.assertEqual(row['username'], 'actor-admin')
        self.assertEqual(row['user_id'], self.admin.id)
        self.assertEqual(row['actor_label'], 'actor-admin')

    def test_number_of_queries_does_not_grow_with_the_number_of_rows(self):
        order = _order(self.customer)

        def add_rows(n):
            for i in range(n):
                AuditLog.objects.create(user=self.admin, action='UPDATE', table_name='product', record_id=i)
                AuditLog.objects.create(
                    user=None, action='ORDER_CANCELLED', table_name='online_order',
                    record_id=order.id, new_value={'status': 'CANCELLED'},
                )
                AuditLog.objects.create(
                    user=None, action='LOGIN_FAILED', table_name='auth_user',
                    new_value={'username': f'u{i}'},
                )

        add_rows(2)
        with CaptureQueriesContext(connection) as small:
            self.client.get('/api/audit-log/?page_size=100')
        add_rows(20)
        with CaptureQueriesContext(connection) as large:
            self.client.get('/api/audit-log/?page_size=100')
        self.assertEqual(len(small), len(large))

    def test_audit_page_uses_the_actor_label(self):
        page = self.client.get('/dashboard/audit-log/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'actor_label')
        self.assertNotContains(page, "log.username || 'Unknown'")
