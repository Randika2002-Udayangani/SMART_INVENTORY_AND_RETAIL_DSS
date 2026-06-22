from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.contrib.auth.models import User, Group
from rest_framework_simplejwt.tokens import RefreshToken
from .audit import log_action



# ============================================================
# REPLACE your existing `class RegisterView(APIView):` block in
# users/views.py with this whole class. It now handles both:
#   POST /api/users/  — create staff account (existing behaviour, unchanged)
#   GET  /api/users/  — list staff accounts, Admin only (new)
# Everything else in the file (other classes/imports) stays as-is.
# ============================================================

class RegisterView(APIView):
    """
    GET  /api/users/
        List all staff accounts. Admin only.

    POST /api/users/
        Creates a new staff account using Django's built-in User model —
        same table that TokenObtainPairView (/api/auth/login/) already checks.
        Body: { "username": "john", "password": "pass123", "role": "STAFF" }
        Valid roles: ADMIN, MANAGER, STAFF
    """

    def get(self, request):
        if not (request.user.is_superuser or request.user.groups.filter(name='ADMIN').exists()):
            return Response({'error': 'Admin access required'}, status=status.HTTP_403_FORBIDDEN)

        users = User.objects.all().order_by('id')
        data = [{
            'id': u.id,
            'username': u.username,
            'role': _get_role(u),
            'is_active': u.is_active,
            'date_joined': u.date_joined,
        } for u in users]
        return Response(data)

    def post(self, request):
        username  = request.data.get('username', '').strip()
        password  = request.data.get('password', '').strip()
        role_name = request.data.get('role', 'STAFF').upper().strip()

        if not username or not password:
            return Response(
                {'error': 'username and password are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if len(password) < 6:
            return Response(
                {'error': 'Password must be at least 6 characters'},
                status=status.HTTP_400_BAD_REQUEST
            )

        valid_roles = ['ADMIN', 'MANAGER', 'STAFF']
        if role_name not in valid_roles:
            return Response(
                {'error': f'role must be one of {valid_roles}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if User.objects.filter(username=username).exists():
            return Response(
                {'error': f'Username "{username}" is already taken'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create the real Django auth User — same table TokenObtainPairView checks
        user = User.objects.create_user(
            username=username,
            password=password,
            is_staff=(role_name in ['ADMIN', 'MANAGER']),
            is_superuser=(role_name == 'ADMIN'),
        )

        # Track role via Django Group (ADMIN / MANAGER / STAFF)
        group, _ = Group.objects.get_or_create(name=role_name)
        user.groups.add(group)

        return Response({
            'message' : f'User "{username}" registered successfully',
            'user_id' : user.id,
            'username': user.username,
            'role'    : role_name,
        }, status=status.HTTP_201_CREATED)


# ============================================================
# Helper function used by RegisterView.get() above and by
# MeView / UserDetailView further down this file.
# Place this ABOVE the RegisterView class (or anywhere at module
# level, as long as it's defined before it's called).
# ============================================================


def _get_role(user):
    if user.is_superuser:
        return 'ADMIN'
    group = user.groups.first()
    return group.name if group else 'STAFF'


class MeView(APIView):
    """GET /api/auth/me/ — current logged-in staff profile + role."""

    def get(self, request):
        user = request.user
        return Response({
            'id': user.id,
            'username': user.username,
            'role': _get_role(user),
            'is_active': user.is_active,
        })


class LogoutView(APIView):
    """
    POST /api/auth/logout/
    Body: { "refresh": "<refresh_token>" }
    Blacklists the refresh token. Requires
    'rest_framework_simplejwt.token_blacklist' in INSTALLED_APPS.
    """

    def post(self, request):
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response({'error': 'refresh token is required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            RefreshToken(refresh_token).blacklist()
        except Exception:
            return Response({'error': 'Invalid or already-expired refresh token'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'message': 'Logged out successfully'})


class ChangePasswordView(APIView):
    """
    POST /api/auth/change-password/
    Body: { "old_password": "...", "new_password": "..." }
    """

    def post(self, request):
        old_password = request.data.get('old_password', '')
        new_password = request.data.get('new_password', '')

        if not old_password or not new_password:
            return Response({'error': 'old_password and new_password are required'}, status=status.HTTP_400_BAD_REQUEST)
        if len(new_password) < 6:
            return Response({'error': 'New password must be at least 6 characters'}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        if not user.check_password(old_password):
            return Response({'error': 'old_password is incorrect'}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save()

        log_action(
            user=user, action='PASSWORD_CHANGE', table_name='auth_user',
            record_id=user.id, old_value=None, new_value=None, request=request,
        )
        return Response({'message': 'Password changed successfully'})




class UserDetailView(APIView):
    """PATCH /api/users/{id}/ — update username/role/is_active. Admin only."""

    def patch(self, request, pk):
        if not (request.user.is_superuser or request.user.groups.filter(name='ADMIN').exists()):
            return Response({'error': 'Admin access required'}, status=status.HTTP_403_FORBIDDEN)

        try:
            target = User.objects.get(pk=pk)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        old_value = {
            'username': target.username,
            'is_active': target.is_active,
            'role': _get_role(target),
        }

        new_username = request.data.get('username')
        new_is_active = request.data.get('is_active')
        new_role = request.data.get('role')

        if new_username:
            target.username = new_username
        if new_is_active is not None:
            target.is_active = bool(new_is_active)
        if new_role:
            new_role = new_role.upper()
            if new_role not in ['ADMIN', 'MANAGER', 'STAFF']:
                return Response({'error': 'role must be ADMIN, MANAGER, or STAFF'}, status=status.HTTP_400_BAD_REQUEST)
            target.groups.clear()
            group, _ = Group.objects.get_or_create(name=new_role)
            target.groups.add(group)
            target.is_staff = new_role in ['ADMIN', 'MANAGER']
            target.is_superuser = (new_role == 'ADMIN')

        target.save()

        new_value = {
            'username': target.username,
            'is_active': target.is_active,
            'role': _get_role(target),
        }

        log_action(
            user=request.user, action='UPDATE', table_name='auth_user',
            record_id=target.id, old_value=old_value, new_value=new_value, request=request,
        )

        return Response({'message': 'User updated', 'id': target.id, **new_value})
    