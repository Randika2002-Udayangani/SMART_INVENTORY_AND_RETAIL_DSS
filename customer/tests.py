from django.test import TestCase
from django.urls import reverse


class PasswordPageRouteTests(TestCase):
    def test_forgot_password_page_returns_200(self):
        response = self.client.get(reverse('forgot-password-page'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'customer/forgot_password.html')

    def test_reset_password_page_returns_200(self):
        response = self.client.get(reverse('reset-password-page'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'customer/reset_password.html')
