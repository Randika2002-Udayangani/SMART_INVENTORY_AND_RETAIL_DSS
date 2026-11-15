import os

from django.contrib.auth.models import Group, User
from django.test import TestCase
from rest_framework.test import APIClient

from users.models import UserLoginSecurity


TEST_PASSWORD = os.environ.get('TEST_PASSWORD', 'testpass123')
TEMP_PASSWORD = 'Temporary-Mango-River-736!'
NEW_PASSWORD = 'Cobalt-River-Mango-984!Lake'


class FirstLoginPasswordTests(TestCase):
    def setUp(self):
        admin_group, _ = Group.objects.get_or_create(name='ADMIN')
        self.admin = User.objects.create_user(
            username='password-policy-admin',
            password=TEST_PASSWORD,
        )
        self.admin.groups.add(admin_group)
        self.client = APIClient()

    def create_staff_user(self, username='first-login-staff'):
        user = User.objects.create_user(username=username, password=TEMP_PASSWORD)
        UserLoginSecurity.objects.create(user=user, must_change_password=True)
        return user

    def login_as(self, username, password):
        response = self.client.post(
            '/api/auth/login/',
            {'username': username, 'password': password},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access']}"
        )
        return response

    def test_admin_created_user_must_change_password_at_first_login(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(
            '/api/users/',
            {
                'username': 'new-first-login-user',
                'password': TEMP_PASSWORD,
                'role': 'STAFF',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(username='new-first-login-user')
        security = UserLoginSecurity.objects.get(user=user)
        self.assertTrue(security.must_change_password)

        self.client.force_authenticate(user=None)
        login = self.login_as(user.username, TEMP_PASSWORD)
        self.assertTrue(login.data['must_change_password'])
        self.assertEqual(login.data['username'], user.username)

    def test_admin_cannot_create_account_with_weak_password(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(
            '/api/users/',
            {'username': 'weak-password-user', 'password': 'short', 'role': 'STAFF'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username='weak-password-user').exists())

    def test_admin_cannot_create_account_with_common_or_user_similar_password(self):
        self.client.force_authenticate(user=self.admin)
        candidates = [
            ('common-password-user', 'password123456'),
            ('similar-password-account', 'similar-password-account-2026!!'),
        ]

        for username, password in candidates:
            with self.subTest(username=username):
                response = self.client.post(
                    '/api/users/',
                    {'username': username, 'password': password, 'role': 'STAFF'},
                    format='json',
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(User.objects.filter(username=username).exists())

    def test_admin_password_reset_requires_user_to_change_temporary_password(self):
        target = User.objects.create_user(
            username='admin-reset-target',
            password=TEST_PASSWORD,
        )
        UserLoginSecurity.objects.create(user=target, must_change_password=False)
        self.client.force_authenticate(user=self.admin)

        response = self.client.patch(
            f'/api/users/{target.pk}/',
            {'password': TEMP_PASSWORD},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        security = UserLoginSecurity.objects.get(user=target)
        self.assertTrue(security.must_change_password)

    def test_temporary_password_blocks_dashboard_apis_except_password_change(self):
        user = self.create_staff_user()
        self.login_as(user.username, TEMP_PASSWORD)

        response = self.client.get('/api/auth/me/')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'password_change_required')

        response = self.client.get('/api/products/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['code'], 'password_change_required')

    def test_user_can_change_temporary_password_and_then_use_dashboard(self):
        user = self.create_staff_user()
        self.login_as(user.username, TEMP_PASSWORD)

        response = self.client.post(
            '/api/auth/change-password/',
            {
                'old_password': TEMP_PASSWORD,
                'new_password': NEW_PASSWORD,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        user.refresh_from_db()
        security = UserLoginSecurity.objects.get(user=user)
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertFalse(security.must_change_password)

        response = self.client.get('/api/auth/me/')
        self.assertEqual(response.status_code, 200)

    def test_user_cannot_choose_a_weak_password(self):
        user = self.create_staff_user('weak-self-change')
        self.login_as(user.username, TEMP_PASSWORD)

        response = self.client.post(
            '/api/auth/change-password/',
            {'old_password': TEMP_PASSWORD, 'new_password': 'short'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(UserLoginSecurity.objects.get(user=user).must_change_password)

    def test_password_change_page_is_routable(self):
        response = self.client.get('/dashboard/change-password/')

        self.assertEqual(response.status_code, 200)

    def test_admin_cannot_reset_to_a_weak_password(self):
        target = self.create_staff_user('weak-admin-reset')
        self.client.force_authenticate(user=self.admin)

        response = self.client.patch(
            f'/api/users/{target.pk}/',
            {'password': 'short'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(target.check_password(TEMP_PASSWORD))
