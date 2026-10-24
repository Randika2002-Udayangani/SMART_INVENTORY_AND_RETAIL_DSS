"""
Audit log coverage tests.

1. get_client_ip(): real client IP behind a proxy, and not spoofable.
2. The actions that were missing from the audit log now write a row.
3. A guard test: every API write endpoint (POST / PUT / PATCH / DELETE) must
   either write to the audit log or be listed in EXEMPT below with a reason.
   Adding a new write endpoint without logging makes this test fail, so the
   gap cannot silently come back.
"""
import ast
import glob
import json
import os
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from orders.models import Customer
from orders.views import OrderListCreateView
from products.models import Brand, Category, Product
from purchases.models import Purchase, PurchaseBatch
from suppliers.models import Supplier
from users.audit import get_client_ip
from users.models import AuditLog


def _admin_user(username='audit-admin'):
    user = User.objects.create_user(username=username, password='Passw0rd-123')
    user.is_staff = True
    user.is_superuser = True
    user.save()
    user.groups.add(Group.objects.get_or_create(name='ADMIN')[0])
    return user


# ─────────────────────────────────────────────────────────────
# 1. Client IP
# ─────────────────────────────────────────────────────────────
class ClientIpTests(TestCase):
    def _request(self, forwarded=None, remote='10.0.0.1'):
        extra = {'REMOTE_ADDR': remote}
        if forwarded is not None:
            extra['HTTP_X_FORWARDED_FOR'] = forwarded
        return RequestFactory().get('/', **extra)

    def test_default_ignores_forwarded_header(self):
        request = self._request(forwarded='203.0.113.9')
        self.assertEqual(get_client_ip(request), '10.0.0.1')

    @override_settings(AUDIT_PROXY_COUNT=1)
    def test_one_proxy_uses_last_entry_and_ignores_forged_first_entry(self):
        request = self._request(forwarded='6.6.6.6, 203.0.113.9')
        self.assertEqual(get_client_ip(request), '203.0.113.9')

    @override_settings(AUDIT_PROXY_COUNT=2)
    def test_two_proxies_use_second_from_right(self):
        request = self._request(forwarded='203.0.113.9, 10.1.1.1')
        self.assertEqual(get_client_ip(request), '203.0.113.9')

    @override_settings(AUDIT_PROXY_COUNT=1)
    def test_missing_header_falls_back_to_remote_addr(self):
        self.assertEqual(get_client_ip(self._request()), '10.0.0.1')

    @override_settings(AUDIT_PROXY_COUNT=1)
    def test_garbage_entry_falls_back_to_remote_addr(self):
        request = self._request(forwarded='not-an-ip')
        self.assertEqual(get_client_ip(request), '10.0.0.1')

    @override_settings(AUDIT_PROXY_COUNT=3)
    def test_fewer_entries_than_proxies_falls_back_to_remote_addr(self):
        request = self._request(forwarded='203.0.113.9')
        self.assertEqual(get_client_ip(request), '10.0.0.1')


# ─────────────────────────────────────────────────────────────
# 2. Actions that were missing from the audit log
# ─────────────────────────────────────────────────────────────
class StaffAccountAndLoginAuditTests(TestCase):
    def setUp(self):
        self.admin = _admin_user()
        self.client = APIClient()

    def test_creating_a_staff_account_is_logged_without_the_password(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            '/api/users/',
            {'username': 'new-manager', 'password': 'Secret-123', 'role': 'MANAGER'},
            format='json',
        )
        self.assertEqual(response.status_code, 201)

        row = AuditLog.objects.get(
            action='CREATE', table_name='auth_user', record_id=response.data['user_id']
        )
        self.assertEqual(row.user, self.admin)
        self.assertEqual(row.new_value['username'], 'new-manager')
        self.assertEqual(row.new_value['role'], 'MANAGER')
        self.assertNotIn('Secret-123', json.dumps(row.new_value))

    def test_wrong_password_for_existing_user_is_logged(self):
        response = self.client.post(
            '/api/auth/login/',
            {'username': 'audit-admin', 'password': 'wrong-password'},
            format='json',
        )
        self.assertEqual(response.status_code, 401)

        row = AuditLog.objects.get(action='LOGIN_FAILED')
        self.assertEqual(row.user, self.admin)
        self.assertEqual(row.new_value['username'], 'audit-admin')
        self.assertEqual(row.new_value['failed_login_count'], 1)
        self.assertNotIn('wrong-password', json.dumps(row.new_value))

    def test_wrong_password_for_unknown_user_is_logged_with_no_user(self):
        response = self.client.post(
            '/api/auth/login/',
            {'username': 'nobody-here', 'password': 'whatever'},
            format='json',
        )
        self.assertEqual(response.status_code, 401)

        row = AuditLog.objects.get(action='LOGIN_FAILED')
        self.assertIsNone(row.user)
        self.assertIsNone(row.record_id)
        self.assertEqual(row.new_value['username'], 'nobody-here')
        self.assertIsNone(row.new_value['failed_login_count'])

    def test_successful_login_is_still_logged(self):
        response = self.client.post(
            '/api/auth/login/',
            {'username': 'audit-admin', 'password': 'Passw0rd-123'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action='LOGIN', user=self.admin).exists())
        self.assertFalse(AuditLog.objects.filter(action='LOGIN_FAILED').exists())


class MasterDataAuditTests(TestCase):
    def setUp(self):
        self.admin = _admin_user()
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_brand_create_update_delete_are_logged(self):
        created = self.client.post('/api/brands/', {'brand_name': 'Audit Brand'}, format='json')
        self.assertEqual(created.status_code, 201)
        brand_id = created.data['id']

        updated = self.client.patch(
            f'/api/brands/{brand_id}/', {'brand_name': 'Audit Brand 2'}, format='json'
        )
        self.assertEqual(updated.status_code, 200)

        deleted = self.client.delete(f'/api/brands/{brand_id}/')
        self.assertEqual(deleted.status_code, 204)

        rows = {
            r.action: r
            for r in AuditLog.objects.filter(table_name='brand', record_id=brand_id)
        }
        self.assertEqual(set(rows), {'CREATE', 'UPDATE', 'DELETE'})
        self.assertEqual(rows['CREATE'].new_value['brand_name'], 'Audit Brand')
        self.assertEqual(rows['UPDATE'].old_value['brand_name'], 'Audit Brand')
        self.assertEqual(rows['UPDATE'].new_value['brand_name'], 'Audit Brand 2')
        self.assertEqual(rows['DELETE'].old_value['brand_name'], 'Audit Brand 2')
        self.assertIsNone(rows['DELETE'].new_value)
        self.assertTrue(all(r.user == self.admin for r in rows.values()))

    def test_category_create_is_logged(self):
        created = self.client.post(
            '/api/categories/', {'category_name': 'Audit Category'}, format='json'
        )
        self.assertEqual(created.status_code, 201)
        self.assertTrue(
            AuditLog.objects.filter(
                action='CREATE', table_name='category', record_id=created.data['id']
            ).exists()
        )

    def test_rating_summary_calculation_is_logged(self):
        response = self.client.post('/api/ratings/summary/calculate/')
        self.assertEqual(response.status_code, 200)
        row = AuditLog.objects.get(action='CALCULATE', table_name='product_rating_summary')
        self.assertEqual(row.user, self.admin)
        self.assertIn('period', row.new_value)


class OnlineOrderAuditTests(TestCase):
    def test_placing_an_online_order_is_logged_with_the_customer(self):
        customer = Customer.objects.create(
            name='Audit Customer', email='audit-customer@example.com', password_hash='unused'
        )
        product = Product.objects.create(
            product_name='Audit Milk 1L', unit_price=Decimal('250.00'),
            cost_price=Decimal('200.00'), is_active=True,
        )
        supplier = Supplier.objects.create(supplier_name='Audit Supplier')
        purchase = Purchase.objects.create(
            supplier=supplier, purchase_date=timezone.now().date() - timedelta(days=5)
        )
        PurchaseBatch.objects.create(
            purchase=purchase, product=product, quantity_received=10,
            cost_price=Decimal('200.00'), remaining_quantity=10, status='ACTIVE',
            expiry_date=timezone.now().date() + timedelta(days=60),
        )

        tomorrow = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        request = APIRequestFactory().post(
            '/api/orders/',
            {
                'pickup_date': tomorrow,
                'time_slot': 'MORNING',
                'items': [{'product_id': product.id, 'quantity': 2}],
            },
            format='json',
        )
        force_authenticate(request, user=customer)
        response = OrderListCreateView.as_view()(request)
        self.assertEqual(response.status_code, 201, getattr(response, 'data', None))

        row = AuditLog.objects.get(action='CREATE', table_name='online_order')
        self.assertIsNone(row.user)
        self.assertEqual(row.new_value['customer_id'], customer.id)
        self.assertEqual(row.new_value['items'][0]['product_id'], product.id)
        self.assertEqual(row.new_value['items'][0]['quantity'], 2)
        self.assertEqual(row.new_value['total_amount'], '500.00')


# ─────────────────────────────────────────────────────────────
# 3. Guard: every write endpoint logs, or is exempt with a reason
# ─────────────────────────────────────────────────────────────
WRITE_METHODS = {'post', 'put', 'patch', 'delete'}
GENERIC_HOOKS = {'perform_create', 'perform_update', 'perform_destroy',
                 'create', 'update', 'destroy'}
GENERIC_WRITE_BASES = {
    'ListCreateAPIView', 'CreateAPIView', 'RetrieveUpdateDestroyAPIView',
    'UpdateAPIView', 'DestroyAPIView', 'RetrieveUpdateAPIView',
}

# "file:Class.METHOD": reason it does not write to the audit log.
EXEMPT = {
    'orders/views.py:CustomerRegisterView.POST': 'customer account, not a staff action',
    'orders/views.py:CustomerLoginView.POST': 'customer login, not a staff action',
    'orders/views.py:CustomerLogoutView.POST': 'customer logout, not a staff action',
    'orders/views.py:CustomerProfileView.PATCH': 'customer edits own profile',
    'orders/views.py:CustomerChangePasswordView.POST': 'customer changes own password',
    'orders/views.py:ChatbotQueryView.POST': 'chat messages are stored in ChatbotLog',
    'orders/views.py:RatingCreateView.POST': 'customer rating is stored with the customer',
    'inventory/views.py:NotificationDetailView.DELETE': 'notification housekeeping (noise)',
    'inventory/views.py:NotificationMarkReadView.PATCH': 'notification housekeeping (noise)',
    'products/views.py:ItemMasterUploadView.POST': 'recorded in UploadLog (uploaded_by, file, status)',
    'purchases/views.py:PurchaseInvoicePDFUploadView.POST': 'recorded in UploadLog (uploaded_by, file, status)',
    'inventory/views.py:SupplierReturnUploadView.POST': 'recorded in UploadLog (uploaded_by, file, status)',
}


def _call_name(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ''


def _scan_write_handlers():
    """Return {key: logs?} for every write handler in the project's views."""
    root = str(settings.BASE_DIR)
    paths = sorted(
        glob.glob(os.path.join(root, '*', 'views.py'))
        + glob.glob(os.path.join(root, '*', 'views', '*.py'))
    )
    result = {}
    for path in paths:
        rel = os.path.relpath(path, root).replace('\\', '/')
        with open(path, encoding='utf-8') as handle:
            tree = ast.parse(handle.read())

        def calls(node, names):
            return any(
                isinstance(n, ast.Call) and _call_name(n) in names
                for n in ast.walk(node)
            )

        helpers = {
            n.name for n in tree.body
            if isinstance(n, ast.FunctionDef) and calls(n, {'log_action'})
        }

        def logs(node):
            # logs directly, via a module helper, or by delegating to another
            # write method of the same view (e.g. put -> patch)
            return calls(node, {'log_action'} | helpers | WRITE_METHODS)

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bases = {
                    b.id if isinstance(b, ast.Name) else getattr(b, 'attr', '')
                    for b in node.bases
                }
                methods = {m.name: m for m in node.body if isinstance(m, ast.FunctionDef)}
                audited_by_mixin = 'AuditedWriteMixin' in bases
                for name, method in methods.items():
                    if name in WRITE_METHODS:
                        result[f'{rel}:{node.name}.{name.upper()}'] = logs(method)
                    elif name in GENERIC_HOOKS:
                        result[f'{rel}:{node.name}.{name}'] = logs(method)
                has_own_write = any(
                    n in methods for n in (WRITE_METHODS | GENERIC_HOOKS)
                )
                if bases & GENERIC_WRITE_BASES and not has_own_write:
                    result[f'{rel}:{node.name}.generic'] = audited_by_mixin
            elif isinstance(node, ast.FunctionDef):
                decorators = ' '.join(ast.unparse(d) for d in node.decorator_list)
                if 'api_view' in decorators and any(
                    m in decorators for m in ('POST', 'PUT', 'PATCH', 'DELETE')
                ):
                    result[f'{rel}:{node.name}.FUNCTION'] = logs(node)
    return result


class AuditCoverageGuardTests(TestCase):
    def test_every_write_endpoint_logs_or_is_exempt(self):
        handlers = _scan_write_handlers()
        self.assertGreater(len(handlers), 40, 'scan found too few handlers - scanner broken?')

        missing = sorted(k for k, logged in handlers.items() if not logged and k not in EXEMPT)
        self.assertEqual(
            missing, [],
            'These write endpoints do not write to the audit log. Call '
            'users.audit.log_action (or use AuditedWriteMixin), or add them '
            'to EXEMPT in users/test_audit_coverage.py with a reason:\n  '
            + '\n  '.join(missing),
        )

    def test_exempt_list_has_no_stale_entries(self):
        handlers = _scan_write_handlers()
        stale = sorted(k for k in EXEMPT if k not in handlers)
        self.assertEqual(stale, [], f'EXEMPT entries that no longer exist: {stale}')
