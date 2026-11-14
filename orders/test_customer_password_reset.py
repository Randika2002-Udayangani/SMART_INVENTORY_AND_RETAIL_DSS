
import re
from datetime import timedelta

from django.contrib.auth.hashers import make_password
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from orders.models import Customer, PasswordResetToken
from orders.password_reset import hash_token


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    DEFAULT_FROM_EMAIL='test@example.com',
    SITE_URL='http://testserver',
)
class CustomerPasswordResetTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.customer = Customer.objects.create(
            name='Reset Test Customer',
            email='reset-test@example.com',
            password_hash=make_password('OldPass123'),
        )
        self.forgot_url = reverse('customer-forgot-password')
        self.reset_url = reverse('customer-reset-password')
        self.login_url = reverse('customer-login')

    def request_reset(self, email=None):
        return self.client.post(
            self.forgot_url,
            {'email': email or self.customer.email},
            format='json',
        )

    def email_token(self, message=None):
        message = message or mail.outbox[-1]
        match = re.search(r'token=([A-Za-z0-9_-]+)', message.body)
        self.assertIsNotNone(match, 'Reset token was not found in email body')
        return match.group(1)

    def create_token(self, raw='test-reset-token', expires_at=None, used_at=None):
        return PasswordResetToken.objects.create(
            customer=self.customer,
            token_hash=hash_token(raw),
            expires_at=expires_at or (timezone.now() + timedelta(minutes=30)),
            used_at=used_at,
        )

    def reset_password(self, token, password='NewPass123'):
        return self.client.post(
            self.reset_url,
            {'token': token, 'new_password': password},
            format='json',
        )

    def test_forgot_password_requires_email(self):
        response = self.client.post(self.forgot_url, {}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'Email is required.')
        self.assertEqual(len(mail.outbox), 0)

    def test_registered_email_gets_generic_response_and_reset_email(self):
        response = self.request_reset()
        self.assertEqual(response.status_code, 200)
        self.assertIn('If that email is registered', response.data['message'])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.customer.email])
        self.assertIn('reset-password/', mail.outbox[0].body)
        self.email_token()

    def test_unknown_email_gets_same_generic_response_without_email(self):
        response = self.request_reset('unknown@example.com')
        self.assertEqual(response.status_code, 200)
        self.assertIn('If that email is registered', response.data['message'])
        self.assertEqual(len(mail.outbox), 0)

    def test_email_send_failure_returns_generic_response(self):
        from unittest.mock import patch

        with patch(
            'orders.views.send_mail',
            side_effect=RuntimeError('Email service unavailable'),
        ):
            response = self.request_reset()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data['message'],
            'If that email is registered, a password reset link has been sent.',
        )

    def test_inactive_customer_gets_generic_response_without_email(self):
        from unittest.mock import patch

        self.customer.is_active = False
        self.customer.save(update_fields=['is_active'])

        with patch('orders.views.send_mail') as mocked_send_mail:
            response = self.request_reset()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data['message'],
            'If that email is registered, a password reset link has been sent.',
        )
        mocked_send_mail.assert_not_called()
        self.assertEqual(
            PasswordResetToken.objects.filter(customer=self.customer).count(), 0
        )

    def test_forgot_password_normalizes_email(self):
        response = self.request_reset('  RESET-TEST@EXAMPLE.COM  ')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.customer.email])

    def test_rate_limit_allows_only_three_requests_per_hour(self):
        for _ in range(4):
            response = self.request_reset()
            self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(
            PasswordResetToken.objects.filter(customer=self.customer).count(), 3
        )

    def test_email_contains_raw_token_but_database_stores_only_hash(self):
        self.request_reset()
        raw = self.email_token()
        record = PasswordResetToken.objects.get(customer=self.customer)
        self.assertEqual(record.token_hash, hash_token(raw))
        self.assertNotEqual(record.token_hash, raw)
        self.assertEqual(len(record.token_hash), 64)

    def test_new_request_invalidates_previous_unused_token(self):
        self.request_reset()
        old_raw = self.email_token()
        self.request_reset()
        new_raw = self.email_token()
        self.assertNotEqual(old_raw, new_raw)
        old_response = self.reset_password(old_raw)
        self.assertEqual(old_response.status_code, 400)
        new_response = self.reset_password(new_raw)
        self.assertEqual(new_response.status_code, 200)

    def test_successful_reset_marks_token_as_used(self):
        record = self.create_token()
        response = self.reset_password('test-reset-token')
        self.assertEqual(response.status_code, 200)
        record.refresh_from_db()
        self.assertIsNotNone(record.used_at)

    def test_invalid_token_is_rejected(self):
        response = self.reset_password('not-a-valid-token')
        self.assertEqual(response.status_code, 400)
        self.assertIn('invalid or has expired', response.data['error'])

    def test_expired_token_is_rejected(self):
        self.create_token(
            raw='expired-reset-token',
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        response = self.reset_password('expired-reset-token')
        self.assertEqual(response.status_code, 400)

    def test_already_used_token_is_rejected(self):
        self.create_token(
            raw='used-reset-token',
            used_at=timezone.now(),
        )
        response = self.reset_password('used-reset-token')
        self.assertEqual(response.status_code, 400)

    def test_inactive_customer_token_is_rejected(self):
        self.create_token(raw='inactive-reset-token')
        self.customer.is_active = False
        self.customer.save(update_fields=['is_active'])
        response = self.reset_password('inactive-reset-token')
        self.assertEqual(response.status_code, 400)

    def test_weak_password_is_rejected_without_consuming_token(self):
        record = self.create_token(raw='weak-password-token')
        response = self.reset_password('weak-password-token', 'abcdefgh')
        self.assertEqual(response.status_code, 400)
        record.refresh_from_db()
        self.assertIsNone(record.used_at)

    def test_successful_reset_clears_login_lockout_fields(self):
        self.customer.failed_login_count = 3
        self.customer.locked_until = timezone.now() + timedelta(minutes=15)
        self.customer.save(update_fields=['failed_login_count', 'locked_until'])
        self.create_token(raw='clear-lockout-token')
        response = self.reset_password('clear-lockout-token')
        self.assertEqual(response.status_code, 200)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.failed_login_count, 0)
        self.assertIsNone(self.customer.locked_until)

    def test_new_password_logs_in_and_old_password_fails(self):
        self.create_token(raw='login-verification-token')
        reset_response = self.reset_password('login-verification-token', 'NewPass123')
        self.assertEqual(reset_response.status_code, 200)

        new_login = self.client.post(
            self.login_url,
            {'email': self.customer.email, 'password': 'NewPass123'},
            format='json',
        )
        self.assertEqual(new_login.status_code, 200)
        self.assertIn('access', new_login.data)

        old_login = self.client.post(
            self.login_url,
            {'email': self.customer.email, 'password': 'OldPass123'},
            format='json',
        )
        self.assertEqual(old_login.status_code, 401)
