from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from datetime import timedelta
from django.contrib.auth.hashers import make_password, check_password
from rest_framework_simplejwt.tokens import RefreshToken
from .models import AppUser, Role


def get_tokens_for_user(user):
    """Generate JWT access + refresh tokens for an AppUser."""
    refresh = RefreshToken()
    refresh['user_id']  = user.id
    refresh['username'] = user.username
    refresh['role']     = user.role.role_name
    return {
        'refresh': str(refresh),
        'access' : str(refresh.access_token),
    }


# ─────────────────────────────────────────────────────────────────
# POST /api/customer-auth/register/
# ─────────────────────────────────────────────────────────────────
class RegisterView(APIView):
    """
    Register a new system user.
    Body: { "username": "john", "password": "pass123", "role": "STAFF" }
    Valid roles: ADMIN, MANAGER, STAFF
    """

    def post(self, request):
        username  = request.data.get('username', '').strip()
        password  = request.data.get('password', '').strip()
        role_name = request.data.get('role', 'STAFF').upper().strip()

        # Validate required fields
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

        # Check username not already taken
        if AppUser.objects.filter(username=username).exists():
            return Response(
                {'error': f'Username "{username}" is already taken'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get or create the Role row
        role, _ = Role.objects.get_or_create(
            role_name=role_name,
            defaults={'description': f'{role_name} role'}
        )

        # Create user with hashed password
        user = AppUser.objects.create(
            username=username,
            password_hash=make_password(password),
            role=role,
            is_active=True,
            failed_login_count=0,
        )

        # Return tokens immediately so user can start using the system
        tokens = get_tokens_for_user(user)

        return Response({
            'message' : f'User "{username}" registered successfully',
            'user_id' : user.id,
            'username': user.username,
            'role'    : role.role_name,
            'tokens'  : tokens
        }, status=status.HTTP_201_CREATED)


# ─────────────────────────────────────────────────────────────────
# POST /api/customer-auth/login/
# ─────────────────────────────────────────────────────────────────
class LoginView(APIView):
    """
    Login endpoint for all system users.
    - Checks account lockout (3 failed attempts = 15 min lock)
    - Verifies hashed password
    - Resets failed_login_count on success
    - Returns JWT access + refresh tokens
    Body: { "username": "john", "password": "pass123" }
    """

    def post(self, request):
        username = request.data.get('username', '').strip()
        password = request.data.get('password', '').strip()

        if not username or not password:
            return Response(
                {'error': 'username and password are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Find user
        try:
            user = AppUser.objects.get(username=username)
        except AppUser.DoesNotExist:
            return Response(
                {'error': 'Invalid username or password'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Check if account is deactivated
        if not user.is_active:
            return Response(
                {'error': 'Account is deactivated. Contact admin.'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Check if account is locked
        if user.locked_until and user.locked_until > timezone.now():
            remaining = int(
                (user.locked_until - timezone.now()).total_seconds() / 60
            )
            return Response(
                {'error': f'Account locked. Try again in {remaining} minute(s).'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Verify password
        if not check_password(password, user.password_hash):
            user.failed_login_count += 1

            # Lock after 3 failures for 15 minutes
            if user.failed_login_count >= 3:
                user.locked_until = timezone.now() + timedelta(minutes=15)
                user.save()
                return Response(
                    {'error': 'Too many failed attempts. Account locked for 15 minutes.'},
                    status=status.HTTP_403_FORBIDDEN
                )

            user.save()
            attempts_left = 3 - user.failed_login_count
            return Response(
                {'error': f'Invalid username or password. {attempts_left} attempt(s) remaining.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Password correct — reset lockout fields
        user.failed_login_count = 0
        user.locked_until = None
        user.save()

        tokens = get_tokens_for_user(user)

        return Response({
            'message' : f'Welcome, {user.username}!',
            'user_id' : user.id,
            'username': user.username,
            'role'    : user.role.role_name,
            'tokens'  : tokens
        }, status=status.HTTP_200_OK)