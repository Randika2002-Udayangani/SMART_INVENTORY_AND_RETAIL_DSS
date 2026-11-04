
from django.conf import settings
from django.test import TestCase
from django.urls import reverse


class StaffLoginPathTests(TestCase):
    def test_admin_url_is_not_available(self):
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 404)

    def test_old_dashboard_login_url_is_not_available(self):
        response = self.client.get("/dashboard/login/")
        self.assertEqual(response.status_code, 404)

    def test_private_staff_login_path_is_available(self):
        path = f"/{settings.STAFF_LOGIN_PATH.strip('/')}/"
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "password")

    def test_dashboard_login_reverse_uses_private_path(self):
        self.assertEqual(
            reverse("dashboard_login"),
            f"/{settings.STAFF_LOGIN_PATH.strip('/')}/",
        )
