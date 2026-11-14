from django.test import TestCase
from rest_framework.test import APIRequestFactory

from orders.password_rules import validate_customer_password
from orders.views import CustomerRegisterView


class CustomerPasswordRuleTests(TestCase):
    def test_password_shorter_than_eight_characters_is_rejected(self):
        error = validate_customer_password('Ab1')
        self.assertIsNotNone(error)
        self.assertIn('8 characters', error)

    def test_password_without_letter_is_rejected(self):
        error = validate_customer_password('12345678')
        self.assertIsNotNone(error)
        self.assertIn('letter and one number', error)

    def test_password_without_number_is_rejected(self):
        error = validate_customer_password('abcdefgh')
        self.assertIsNotNone(error)
        self.assertIn('letter and one number', error)

    def test_common_password_password123_is_rejected(self):
        error = validate_customer_password('password123')
        self.assertIsNotNone(error)

    def test_common_password_password1_is_rejected(self):
        error = validate_customer_password('Password1')
        self.assertIsNotNone(error)

    def test_good_password_is_accepted(self):
        self.assertIsNone(validate_customer_password('GoodPass123'))

    def test_samanala_password_is_accepted(self):
        self.assertIsNone(validate_customer_password('Samanala2026'))

    def test_registration_rejects_weak_password_and_accepts_valid_password(self):
        factory = APIRequestFactory()
        register = CustomerRegisterView.as_view()

        weak_request = factory.post(
            '/api/customer-auth/register/',
            {'name': 'Weak Customer', 'email': 'weak-rules@example.com', 'password': 'abcdefgh'},
            format='json',
        )
        weak_response = register(weak_request)
        self.assertEqual(weak_response.status_code, 400)

        valid_request = factory.post(
            '/api/customer-auth/register/',
            {'name': 'Valid Customer', 'email': 'valid-rules@example.com', 'password': 'GoodPass123'},
            format='json',
        )
        valid_response = register(valid_request)
        self.assertEqual(valid_response.status_code, 201)
