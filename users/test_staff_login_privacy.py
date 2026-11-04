"""
The private staff login path must never be revealed to someone who is not
logged in. Dashboard pages are served to anyone (the data needs a JWT), so
their HTML must not contain the staff login address.
"""
import re
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import get_resolver

SECRET = 'zz-private-test-word'
BASE_DIR = Path(settings.BASE_DIR)


@override_settings(STAFF_LOGIN_PATH=SECRET)
class StaffPathNotLeakedTests(TestCase):
    def _dashboard_urls(self):
        urls = ['/dashboard/']
        for pattern in get_resolver().url_patterns:
            if str(pattern.pattern) != 'dashboard/':
                continue
            for sub in pattern.url_patterns:
                route = str(sub.pattern)
                if '<' not in route and route:
                    urls.append('/dashboard/' + route)
        return sorted(set(urls))

    def test_no_dashboard_page_contains_the_staff_path(self):
        urls = self._dashboard_urls()
        self.assertGreater(len(urls), 10)   # make sure we really scanned them
        for url in urls:
            body = self.client.get(url).content.decode()
            self.assertNotIn(SECRET, body, f'{url} reveals the staff login path')
            self.assertNotIn('dashboard/login', body, f'{url} links the old login')

    def test_landing_and_customer_pages_do_not_contain_the_staff_path(self):
        for url in ['/', '/customer/login/', '/customer/register/', '/customer/products/']:
            body = self.client.get(url).content.decode()
            self.assertNotIn(SECRET, body, url)
            self.assertNotIn('dashboard/login', body, url)
            self.assertNotIn('Staff Login', body, url)


class DashboardTemplatesTests(SimpleTestCase):
    def test_no_template_hard_codes_the_old_login_address(self):
        for path in (BASE_DIR / 'templates' / 'dashboard').glob('*.html'):
            self.assertNotIn(
                '/dashboard/login', path.read_text(encoding='utf-8'), path.name
            )

    def test_login_page_remembers_its_own_address_for_redirects(self):
        login = (BASE_DIR / 'templates' / 'dashboard' / 'login.html').read_text(encoding='utf-8')
        self.assertIn("sessionStorage.setItem('staff_login_url'", login)


class StaffPathRequiredInProductionTests(SimpleTestCase):
    def _import_settings(self, **env):
        import os
        # Start from empty (not removed) values: settings.py loads the
        # developer's own .env file, and python-dotenv never overwrites a
        # variable that already exists, so this keeps the test independent
        # of whatever is in the local .env.
        full = dict(os.environ)
        full.update({'STAFF_LOGIN_PATH': '', 'DEBUG': ''})
        full.update(env)
        full.setdefault('SECRET_KEY', 'x')
        return subprocess.run(
            [sys.executable, '-c',
             'import os; os.environ["DJANGO_SETTINGS_MODULE"]="smart_inventory.settings";'
             'from django.conf import settings; print("PATH=" + settings.STAFF_LOGIN_PATH)'],
            cwd=BASE_DIR, env=full, capture_output=True, text=True,
        )

    def test_missing_variable_stops_the_app_when_debug_is_off(self):
        result = self._import_settings(DEBUG='False')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('STAFF_LOGIN_PATH must be set', result.stderr)

    def test_variable_is_used_when_set(self):
        result = self._import_settings(DEBUG='False', STAFF_LOGIN_PATH='/my-word/')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PATH=my-word', result.stdout)

    def test_development_fallback_only_when_debug_is_on(self):
        result = self._import_settings(DEBUG='True')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PATH=dev-staff-entry', result.stdout)
