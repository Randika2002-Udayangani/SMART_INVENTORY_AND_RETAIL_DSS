from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.contrib.auth.models import User, Group


class RegisterView(APIView):
    """
    POST /api/users/
    Creates a new staff account using Django's built-in User model —
    same table that TokenObtainPairView (/api/auth/login/) already checks.
    Body: { "username": "john", "password": "pass123", "role": "STAFF" }
    Valid roles: ADMIN, MANAGER, STAFF
    """

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