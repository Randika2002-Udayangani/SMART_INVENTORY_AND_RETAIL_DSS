"""
Shared helpers for the F06 / F07 / F08 test files.

Why this exists
---------------
The views for lifecycle, losses, supplier returns and health scores use the
IsManagerOrAdmin permission class.  The old tests created a user with only
is_staff=True, so every request came back 403 even though login worked.
This helper creates a user that really has the MANAGER role, so the tests
exercise the endpoints the same way a real manager account does.

Place this file at:  inventory/testing_helpers.py
(The name deliberately does NOT start with "test" so Django's test runner
does not try to collect it as a test module.)
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

TEST_PASSWORD = os.environ.get('TEST_PASSWORD', 'testpass123')

# The app computes "today" in Sri Lanka time (see LOCAL_TZ in inventory/views.py),
# so the tests build their dates the same way to avoid off-by-one-day failures.
LOCAL_TZ = ZoneInfo('Asia/Colombo')


def local_today():
    return datetime.now(LOCAL_TZ).date()


def create_user_with_role(username, role=None, password=TEST_PASSWORD):
    """
    Create a user.  role='MANAGER' or 'ADMIN' puts the user in that group
    (and sets a `role` attribute too, if the user model has one).
    role=None creates a plain staff-level user with no manager/admin rights.
    """
    User = get_user_model()
    user = User.objects.create_user(
        username=username,
        password=password,
        is_staff=True,
    )
    if role:
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
        if hasattr(user, 'role'):
            try:
                user.role = role
                user.save()
            except (AttributeError, ValueError):
                pass
    return user


def login_headers(client, username, password=TEST_PASSWORD):
    """Log in through the real /api/auth/login/ endpoint, return auth header."""
    response = client.post(
        '/api/auth/login/',
        {'username': username, 'password': password},
        content_type='application/json',
    )
    if response.status_code != 200:
        raise AssertionError(
            f'Login failed for {username}: {response.status_code} {response.content!r}'
        )
    data = response.json()
    token = data.get('access') or data.get('token')
    return {'HTTP_AUTHORIZATION': f'Bearer {token}'}