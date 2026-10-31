"""
Schema tests for customer login lockout + password reset tokens.
These only prove the new database fields/table exist and have safe defaults;
the lockout and reset LOGIC (and its tests) is added in later pull requests.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from orders.models import Customer, PasswordResetToken


class CustomerLockoutFieldsTests(TestCase):
    def test_new_customer_starts_unlocked_with_zero_failures(self):
        c = Customer.objects.create(name='A', email='a@example.com', password_hash='x')
        c.refresh_from_db()
        self.assertEqual(c.failed_login_count, 0)
        self.assertIsNone(c.locked_until)

    def test_lockout_fields_can_be_saved(self):
        c = Customer.objects.create(name='B', email='b@example.com', password_hash='x')
        until = timezone.now() + timedelta(minutes=15)
        c.failed_login_count = 5
        c.locked_until = until
        c.save()
        c.refresh_from_db()
        self.assertEqual(c.failed_login_count, 5)
        self.assertEqual(c.locked_until, until)


class PasswordResetTokenModelTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(name='C', email='c@example.com', password_hash='x')

    def test_token_is_created_unused(self):
        t = PasswordResetToken.objects.create(
            customer=self.customer,
            token_hash='a' * 64,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        self.assertIsNone(t.used_at)
        self.assertEqual(self.customer.password_reset_tokens.count(), 1)

    def test_token_hash_must_be_unique(self):
        from django.db import IntegrityError, transaction
        expires = timezone.now() + timedelta(minutes=30)
        PasswordResetToken.objects.create(customer=self.customer, token_hash='b' * 64, expires_at=expires)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PasswordResetToken.objects.create(customer=self.customer, token_hash='b' * 64, expires_at=expires)

    def test_tokens_are_deleted_with_the_customer(self):
        PasswordResetToken.objects.create(
            customer=self.customer, token_hash='c' * 64,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        self.customer.delete()
        self.assertEqual(PasswordResetToken.objects.count(), 0)
