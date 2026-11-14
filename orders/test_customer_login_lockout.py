from datetime import timedelta

from django.contrib.auth.hashers import make_password
from django.test import TestCase
from django.utils import timezone

from rest_framework.test import APIClient
from rest_framework import status

from .models import Customer


class CustomerLoginLockoutTests(TestCase):

    LOGIN_URL = "/api/customer-auth/login/"
    PASSWORD = "GoodPass123"

    def setUp(self):
        self.client = APIClient()

    def create_customer(self, email="customer@example.com", **kwargs):
        defaults = {
            "name": "Test Customer",
            "email": email,
            "password_hash": make_password(self.PASSWORD),
            "is_active": True,
            "failed_login_count": 0,
            "locked_until": None,
        }

        defaults.update(kwargs)

        return Customer.objects.create(**defaults)

    def login(self, email="customer@example.com", password=None):
        return self.client.post(
            self.LOGIN_URL,
            {
                "email": email,
                "password": password or self.PASSWORD,
            },
            format="json"
        )

    def test_first_wrong_password_returns_401(self):
        customer = self.create_customer()

        response = self.login(password="WrongPassword")

        customer.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(customer.failed_login_count, 1)
        self.assertIsNone(customer.locked_until)

    def test_second_wrong_password_returns_401(self):
        customer = self.create_customer()

        self.login(password="WrongPassword")
        response = self.login(password="WrongPassword")

        customer.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(customer.failed_login_count, 2)
        self.assertIsNone(customer.locked_until)

    def test_third_wrong_password_locks_account(self):
        customer = self.create_customer()

        self.login(password="WrongPassword")
        self.login(password="WrongPassword")
        response = self.login(password="WrongPassword")

        customer.refresh_from_db()

        self.assertEqual(
            response.status_code,
            status.HTTP_429_TOO_MANY_REQUESTS
        )

        self.assertEqual(customer.failed_login_count, 3)
        self.assertIsNotNone(customer.locked_until)

        self.assertAlmostEqual(
            customer.locked_until,
            timezone.now() + timedelta(minutes=15),
            delta=timedelta(seconds=5)
        )

        self.assertEqual(
            response.data["error"],
            "Too many failed login attempts. "
            "Please try again in 15 minutes."
        )

    def test_correct_password_while_locked_returns_429(self):
        customer = self.create_customer(
            failed_login_count=3,
            locked_until=timezone.now() + timedelta(minutes=15)
        )

        response = self.login()

        customer.refresh_from_db()

        self.assertEqual(
            response.status_code,
            status.HTTP_429_TOO_MANY_REQUESTS
        )
        self.assertEqual(customer.failed_login_count, 3)
        self.assertIsNotNone(customer.locked_until)

    def test_wrong_password_while_locked_does_not_change_account(self):
        lock_time = timezone.now() + timedelta(minutes=15)

        customer = self.create_customer(
            failed_login_count=3,
            locked_until=lock_time
        )

        response = self.login(password="WrongPassword")

        customer.refresh_from_db()

        self.assertEqual(
            response.status_code,
            status.HTTP_429_TOO_MANY_REQUESTS
        )
        self.assertEqual(customer.failed_login_count, 3)
        self.assertEqual(customer.locked_until, lock_time)

    def test_expired_lock_and_correct_password_resets_account(self):
        customer = self.create_customer(
            failed_login_count=3,
            locked_until=timezone.now() - timedelta(minutes=1)
        )

        response = self.login()

        customer.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(customer.failed_login_count, 0)
        self.assertIsNone(customer.locked_until)

    def test_expired_lock_and_wrong_password_starts_new_count(self):
        customer = self.create_customer(
            failed_login_count=3,
            locked_until=timezone.now() - timedelta(minutes=1)
        )

        response = self.login(password="WrongPassword")

        customer.refresh_from_db()

        self.assertEqual(
            response.status_code,
            status.HTTP_401_UNAUTHORIZED
        )
        self.assertEqual(customer.failed_login_count, 1)
        self.assertIsNone(customer.locked_until)

    def test_two_wrong_attempts_then_correct_password_resets_count(self):
        customer = self.create_customer()

        self.login(password="WrongPassword")
        self.login(password="WrongPassword")

        response = self.login()

        customer.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(customer.failed_login_count, 0)
        self.assertIsNone(customer.locked_until)

    def test_unknown_email_returns_401_without_creating_customer(self):
        initial_count = Customer.objects.count()

        response = self.login(
            email="unknown@example.com",
            password="WrongPassword"
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_401_UNAUTHORIZED
        )
        self.assertEqual(Customer.objects.count(), initial_count)
        self.assertFalse(
            Customer.objects.filter(
                email="unknown@example.com"
            ).exists()
        )

    def test_inactive_customer_returns_401(self):
        self.create_customer(is_active=False)

        response = self.login()

        self.assertEqual(
            response.status_code,
            status.HTTP_401_UNAUTHORIZED
        )

    def test_missing_email_or_password_returns_400(self):
        cases = [
            {"password": self.PASSWORD},
            {"email": "customer@example.com"},
            {},
        ]

        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post(
                    self.LOGIN_URL,
                    payload,
                    format="json"
                )

                self.assertEqual(
                    response.status_code,
                    status.HTTP_400_BAD_REQUEST
                )

    def test_lockout_is_isolated_between_customers(self):
        customer_one = self.create_customer(
            email="customer1@example.com"
        )

        customer_two = self.create_customer(
            email="customer2@example.com"
        )

        for _ in range(3):
            self.login(
                email="customer1@example.com",
                password="WrongPassword"
            )

        customer_one.refresh_from_db()
        customer_two.refresh_from_db()

        self.assertEqual(customer_one.failed_login_count, 3)
        self.assertIsNotNone(customer_one.locked_until)

        self.assertEqual(customer_two.failed_login_count, 0)
        self.assertIsNone(customer_two.locked_until)

        response = self.login(email="customer2@example.com")

        self.assertEqual(response.status_code, status.HTTP_200_OK)