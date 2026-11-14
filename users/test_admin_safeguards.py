import os

from django.contrib.auth.models import Group, User
from django.test import TestCase
from rest_framework.test import APIClient

from users.models import UserLoginSecurity


TEST_PASSWORD = os.environ.get('TEST_PASSWORD', 'testpass123')


class AdminSafeguardTests(TestCase):
    def setUp(self):
        admin_group, _ = Group.objects.get_or_create(name='ADMIN')
        self.admin = User.objects.create_user(
            username='safeguard-admin',
            password=TEST_PASSWORD,
        )
        self.admin.groups.add(admin_group)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def create_admin(self, username):
        user = User.objects.create_user(username=username, password=TEST_PASSWORD)
        user.groups.add(Group.objects.get(name='ADMIN'))
        return user

    def test_admin_cannot_deactivate_own_account(self):
        response = self.client.patch(
            f'/api/users/{self.admin.pk}/',
            {'is_active': False},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_last_active_admin_cannot_be_deactivated(self):
        target = self.create_admin('last-admin-deactivate')
        self.admin.is_active = False
        self.admin.save(update_fields=['is_active'])

        response = self.client.patch(
            f'/api/users/{target.pk}/',
            {'is_active': False},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        target.refresh_from_db()
        self.assertTrue(target.is_active)

    def test_last_active_admin_cannot_be_demoted(self):
        target = self.create_admin('last-admin-demote')
        self.admin.is_active = False
        self.admin.save(update_fields=['is_active'])

        response = self.client.patch(
            f'/api/users/{target.pk}/',
            {'role': 'STAFF'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        target.refresh_from_db()
        self.assertTrue(target.groups.filter(name='ADMIN').exists())

    def test_admin_can_be_deactivated_when_another_active_admin_exists(self):
        target = self.create_admin('admin-to-deactivate')

        response = self.client.patch(
            f'/api/users/{target.pk}/',
            {'is_active': False},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        target.refresh_from_db()
        self.assertFalse(target.is_active)

    def test_staff_role_can_still_be_changed(self):
        staff = User.objects.create_user(
            username='staff-to-promote',
            password=TEST_PASSWORD,
        )

        response = self.client.patch(
            f'/api/users/{staff.pk}/',
            {'role': 'MANAGER'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        staff.refresh_from_db()
        self.assertTrue(staff.groups.filter(name='MANAGER').exists())
        self.assertTrue(staff.is_staff)
        self.assertFalse(staff.is_superuser)

    def test_password_reset_still_works_and_clears_lockout(self):
        security = UserLoginSecurity.objects.create(
            user=self.admin,
            failed_login_count=5,
        )
        new_password = 'Reset-Password-123'

        response = self.client.patch(
            f'/api/users/{self.admin.pk}/',
            {'password': new_password},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.admin.refresh_from_db()
        security.refresh_from_db()
        self.assertTrue(self.admin.check_password(new_password))
        self.assertEqual(security.failed_login_count, 0)
        self.assertIsNone(security.locked_until)
