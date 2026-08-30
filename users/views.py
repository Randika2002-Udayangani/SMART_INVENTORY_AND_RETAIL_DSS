from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.contrib.auth.models import User, Group
from django.utils import timezone
from datetime import timedelta
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework.exceptions import AuthenticationFailed, APIException
from .audit import log_action
from .models import SystemConfig
from .models import AuditLog
from .models import UserLoginSecurity


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

        # Pull all lockout rows in one query instead of one query per user.
        security_by_user_id = {
            s.user_id: s for s in UserLoginSecurity.objects.filter(user__in=users)
        }

        now = timezone.now()
        data = []
        for u in users:
            security = security_by_user_id.get(u.id)
            is_locked = bool(
                security and security.locked_until and security.locked_until > now
            )
            data.append({
                'id': u.id,
                'username': u.username,
                'role': _get_role(u),
                'is_active': u.is_active,
                'date_joined': u.date_joined,
                'is_locked': is_locked,
                'locked_until': security.locked_until if is_locked else None,
                'failed_login_count': security.failed_login_count if security else 0,
            })
        return Response(data)

    def post(self, request):
        if not (request.user.is_superuser or request.user.groups.filter(name='ADMIN').exists()):
            return Response({'error': 'Admin access required'}, status=status.HTTP_403_FORBIDDEN)

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

        if request.user and request.user.is_authenticated:
            log_action(
                user=request.user, action='LOGOUT', table_name='auth_user',
                record_id=request.user.id, old_value=None, new_value=None,
                request=request,
            )
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
    """PATCH /api/users/{id}/ — update username/role/is_active/password. Admin only."""

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
        new_password = request.data.get('password')

        if new_password:
            new_password = new_password.strip()
            if len(new_password) < 6:
                return Response({'error': 'Password must be at least 6 characters'}, status=status.HTTP_400_BAD_REQUEST)
            target.set_password(new_password)
            # Admin-initiated reset — clear any lockout state so the new
            # password isn't blocked by an old failed-attempt count.
            security, _ = UserLoginSecurity.objects.get_or_create(user=target)
            security.failed_login_count = 0
            security.locked_until = None
            security.save()

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
        # Never write the actual password into Audit_Log — just record that
        # a reset happened, alongside whatever else changed in this request.
        if new_password:
            new_value['password_reset'] = True

        log_action(
            user=request.user, action='UPDATE', table_name='auth_user',
            record_id=target.id, old_value=old_value, new_value=new_value, request=request,
        )

        return Response({'message': 'User updated', 'id': target.id, **new_value})
    
# Default keys from API Design Doc v3.1, Section 20. Auto-seeded on
# first GET if missing — won't touch any other keys already in the
# table (e.g. 'last_item_ledger_sync', used separately by inventory).
DEFAULT_CONFIG = [
    ("min_margin_pct", "10", "F09 Discount Engine — profit floor"),
    ("expiry_alert_days", "30", "F09 Discount Engine + F16 Alerts"),
    ("min_order_value", "0", "F12 Online Order validation"),
    ("min_order_advance_hours", "24", "F12 Online Order validation"),
    ("max_order_advance_days", "7", "F12 Online Order validation"),
]
 
 
class SystemConfigListView(APIView):
    """GET /api/config/ — all key-value pairs. Any authenticated staff."""
 
    def get(self, request):
        existing_keys = set(SystemConfig.objects.values_list('key', flat=True))
        missing = [
            SystemConfig(key=k, value=v, description=d)
            for k, v, d in DEFAULT_CONFIG if k not in existing_keys
        ]
        if missing:
            SystemConfig.objects.bulk_create(missing)
 
        configs = SystemConfig.objects.all().order_by('key')
        data = [{
            'key': c.key,
            'value': c.value,
            'description': c.description,
            'updated_at': c.updated_at,
        } for c in configs]
        return Response(data)
 
 
class SystemConfigDetailView(APIView):
    """
    GET /api/config/{key}/  — any authenticated staff
    PUT /api/config/{key}/  — Admin only. Body: {"value": "12"}. Writes Audit_Log.
    """
 
    def get(self, request, key):
        try:
            c = SystemConfig.objects.get(key=key)
        except SystemConfig.DoesNotExist:
            return Response({'error': f"Config key '{key}' not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            'key': c.key, 'value': c.value,
            'description': c.description, 'updated_at': c.updated_at,
        })
 
    def put(self, request, key):
        if not (request.user.is_superuser or request.user.groups.filter(name='ADMIN').exists()):
            return Response({'error': 'Admin access required'}, status=status.HTTP_403_FORBIDDEN)
 
        try:
            c = SystemConfig.objects.get(key=key)
        except SystemConfig.DoesNotExist:
            return Response({'error': f"Config key '{key}' not found"}, status=status.HTTP_404_NOT_FOUND)
 
        new_value = request.data.get('value')
        if new_value is None:
            return Response({'error': 'value is required'}, status=status.HTTP_400_BAD_REQUEST)
 
        old_value = c.value
        c.value = str(new_value)
        c.save()
 
        log_action(
            user=request.user, action='CONFIG_CHANGE', table_name='system_config',
            record_id=c.id, old_value={'value': old_value},
            new_value={'value': c.value}, request=request,
        )
 
        return Response({
            'key': c.key, 'value': c.value,
            'description': c.description, 'updated_at': c.updated_at,
        })
 
class AuditLogListView(APIView):
    """
    GET /api/audit-log/
    Admin only. Filter by ?user=<id>, ?table_name=, ?action=,
    ?date_from=YYYY-MM-DD, ?date_to=YYYY-MM-DD
    """
 
    def get(self, request):
        if not (request.user.is_superuser or request.user.groups.filter(name='ADMIN').exists()):
            return Response({'error': 'Admin access required'}, status=status.HTTP_403_FORBIDDEN)
 
        logs = AuditLog.objects.all().order_by('-timestamp')
 
        user_id = request.query_params.get('user')
        table_name = request.query_params.get('table_name')
        action = request.query_params.get('action')
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
 
        if user_id:
            logs = logs.filter(user_id=user_id)
        if table_name:
            logs = logs.filter(table_name=table_name)
        if action:
            logs = logs.filter(action=action)
        if date_from:
            logs = logs.filter(timestamp__date__gte=date_from)
        if date_to:
            logs = logs.filter(timestamp__date__lte=date_to)
 
        data = [{
            'id': log.id,
            'user_id': log.user_id,
            'username': log.user.username if log.user else None,
            'action': log.action,
            'table_name': log.table_name,
            'record_id': log.record_id,
            'ip_address': log.ip_address,
            'timestamp': log.timestamp,
        } for log in logs]
 
        return Response(data)
 
 
class AuditLogDetailView(APIView):
    """
    GET /api/audit-log/{id}/
    Admin only. Full entry including old_value/new_value JSON.
    """
 
    def get(self, request, pk):
        if not (request.user.is_superuser or request.user.groups.filter(name='ADMIN').exists()):
            return Response({'error': 'Admin access required'}, status=status.HTTP_403_FORBIDDEN)
 
        try:
            log = AuditLog.objects.get(pk=pk)
        except AuditLog.DoesNotExist:
            return Response({'error': 'Audit log entry not found'}, status=status.HTTP_404_NOT_FOUND)
 
        return Response({
            'id': log.id,
            'user_id': log.user_id,
            'username': log.user.username if log.user else None,
            'action': log.action,
            'table_name': log.table_name,
            'record_id': log.record_id,
            'old_value': log.old_value,
            'new_value': log.new_value,
            'ip_address': log.ip_address,
            'timestamp': log.timestamp,
        })


# ─────────────────────────────────────────────────────────────────
# F15 — Account lockout (API Design Doc v3.1 §4.1)
#
# 3 failed attempts → locked for 15 minutes, HTTP 423 with locked_until.
# Ties to UserLoginSecurity (real auth User), not the orphaned AppUser
# table — see model comment in users/models.py for why.
#
# NOTE: TokenObtainSerializer.validate() raises rest_framework's own
# AuthenticationFailed (not rest_framework_simplejwt's own subclass of
# it) — catching the wrong one here silently breaks the whole feature,
# since the except clause just never matches. Confirmed by testing the
# actual 3-failed-attempts flow, not just reading the code.
# ─────────────────────────────────────────────────────────────────

class AccountLocked(APIException):
    status_code = 423
    default_detail = 'Account locked due to too many failed login attempts.'
    default_code = 'account_locked'


class LockoutTokenObtainPairSerializer(TokenObtainPairSerializer):
    LOCKOUT_MAX_ATTEMPTS = 3
    LOCKOUT_DURATION_MINUTES = 15

    def validate(self, attrs):
        username = attrs.get(self.username_field)
        try:
            user = User.objects.get(**{self.username_field: username})
        except User.DoesNotExist:
            user = None

        if user:
            security, _ = UserLoginSecurity.objects.get_or_create(user=user)
            if security.locked_until and security.locked_until > timezone.now():
                raise AccountLocked(detail={
                    'error': 'Account locked due to too many failed login attempts.',
                    'locked_until': security.locked_until,
                })

        try:
            data = super().validate(attrs)
        except AuthenticationFailed:
            if user:
                security, _ = UserLoginSecurity.objects.get_or_create(user=user)
                security.failed_login_count += 1
                just_locked = False
                if security.failed_login_count >= self.LOCKOUT_MAX_ATTEMPTS:
                    security.locked_until = timezone.now() + timedelta(minutes=self.LOCKOUT_DURATION_MINUTES)
                    just_locked = True
                security.save()

                if just_locked:
                    log_action(
                        user=user, action='LOGIN_LOCKOUT', table_name='user_login_security',
                        record_id=user.id,
                        old_value={'failed_login_count': security.failed_login_count - 1},
                        new_value={
                            'failed_login_count': security.failed_login_count,
                            'locked_until': str(security.locked_until),
                        },
                        request=self.context.get('request'),
                    )
            raise

        # Successful login — reset the counter
        if user:
            security, _ = UserLoginSecurity.objects.get_or_create(user=user)
            security.failed_login_count = 0
            security.locked_until = None
            security.save()

            log_action(
                user=user, action='LOGIN', table_name='auth_user',
                record_id=user.id, old_value=None, new_value=None,
                request=self.context.get('request'),
            )

        return data


class LockoutTokenObtainPairView(TokenObtainPairView):
    serializer_class = LockoutTokenObtainPairSerializer


class UnlockUserView(APIView):
    """
    POST /api/users/<id>/unlock/  — Admin only.
    Resets failed_login_count to 0 and clears locked_until.
    """
    def post(self, request, pk):
        if not (request.user.is_superuser or request.user.groups.filter(name='ADMIN').exists()):
            return Response({'error': 'Admin access required'}, status=status.HTTP_403_FORBIDDEN)

        try:
            target = User.objects.get(pk=pk)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        security, _ = UserLoginSecurity.objects.get_or_create(user=target)
        old_data = {
            'failed_login_count': security.failed_login_count,
            'locked_until': str(security.locked_until) if security.locked_until else None,
        }
        security.failed_login_count = 0
        security.locked_until = None
        security.save()

        log_action(
            user=request.user, action='UNLOCK', table_name='user_login_security',
            record_id=target.id, old_value=old_data,
            new_value={'failed_login_count': 0, 'locked_until': None},
            request=request,
        )

        return Response({'message': f'Account for {target.username} unlocked successfully.'})