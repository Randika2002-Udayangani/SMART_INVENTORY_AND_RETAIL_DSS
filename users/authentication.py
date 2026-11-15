from django.core.exceptions import ObjectDoesNotExist
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication

from users.models import UserLoginSecurity


PASSWORD_CHANGE_PATH = '/api/auth/change-password'


def enforce_required_password_change(request, authentication):
    if authentication is None or request.path_info.rstrip('/') == PASSWORD_CHANGE_PATH:
        return

    user, _ = authentication
    try:
        must_change_password = user.login_security.must_change_password
    except ObjectDoesNotExist:
        return

    if must_change_password:
        raise PermissionDenied({
            'code': 'password_change_required',
            'error': 'Change your temporary password before using the dashboard.',
        })


class StaffJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        authentication = super().authenticate(request)
        enforce_required_password_change(request, authentication)
        return authentication
