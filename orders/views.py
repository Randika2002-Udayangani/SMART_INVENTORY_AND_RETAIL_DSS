from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.contrib.auth.hashers import make_password, check_password
from django.utils import timezone
from .models import Customer
from .tokens import get_tokens_for_customer
from .authentication import CustomerJWTAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken


class CustomerRegisterView(APIView):
    """
    POST /api/customer-auth/register/
    Public — no token needed.
    Customer self-registration.
    """
    permission_classes = []  # public

    def post(self, request):
        name = request.data.get('name')
        email = request.data.get('email')
        password = request.data.get('password')
        contact_number = request.data.get('contact_number', '')
        address = request.data.get('address', '')

        # Validate required fields
        if not name or not email or not password:
            return Response(
                {'error': 'name, email, and password are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check email uniqueness
        if Customer.objects.filter(email=email).exists():
            return Response(
                {'error': 'An account with this email already exists'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create customer with hashed password
        customer = Customer.objects.create(
            name=name,
            email=email,
            password_hash=make_password(password),
            contact_number=contact_number,
            address=address,
            is_active=True,
        )

        # Return tokens immediately so customer is logged in after registration
        tokens = get_tokens_for_customer(customer)

        return Response({
            'message': 'Registration successful',
            'customer_id': customer.id,
            'name': customer.name,
            'email': customer.email,
            **tokens
        }, status=status.HTTP_201_CREATED)


class CustomerLoginView(APIView):
    """
    POST /api/customer-auth/login/
    Public — no token needed.
    Returns same JWT format as staff login.
    """
    permission_classes = []  # public

    def post(self, request):
        email = request.data.get('email')
        password = request.data.get('password')

        if not email or not password:
            return Response(
                {'error': 'email and password are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            customer = Customer.objects.get(email=email, is_active=True)
        except Customer.DoesNotExist:
            return Response(
                {'error': 'Invalid email or password'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        if not check_password(password, customer.password_hash):
            return Response(
                {'error': 'Invalid email or password'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Update last login
        customer.last_login = timezone.now()
        customer.save()

        tokens = get_tokens_for_customer(customer)

        return Response({
            'message': 'Login successful',
            'customer_id': customer.id,
            'name': customer.name,
            'email': customer.email,
            **tokens
        })


# ============================================================
# In orders/views.py:
# 1) DELETE the existing stub `class CustomerProfileView(APIView): ...`
#    and replace it with the version below.
# 2) ADD these imports near the top, with your other imports:
#    from rest_framework.permissions import IsAuthenticated
#    from rest_framework_simplejwt.tokens import RefreshToken
#    from .authentication import CustomerJWTAuthentication
# 3) APPEND CustomerLogoutView and CustomerChangePasswordView below.
# ============================================================


class CustomerProfileView(APIView):
    """
    GET   /api/customer-auth/profile/   — view own profile
    PATCH /api/customer-auth/profile/   — partial update
          Body: any subset of {name, contact_number, address}
    Requires customer JWT (Authorization: Bearer <customer_access_token>).
    """
    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        c = request.user  # CustomerJWTAuthentication resolves this to a Customer
        return Response({
            'customer_id': c.id,
            'name': c.name,
            'email': c.email,
            'contact_number': c.contact_number,
            'address': c.address,
        })

    def patch(self, request):
        c = request.user

        if 'name' in request.data:
            c.name = request.data['name']
        if 'contact_number' in request.data:
            c.contact_number = request.data['contact_number']
        if 'address' in request.data:
            c.address = request.data['address']
        c.save()

        return Response({
            'message': 'Profile updated',
            'name': c.name,
            'contact_number': c.contact_number,
            'address': c.address,
        })


class CustomerLogoutView(APIView):
    """
    POST /api/customer-auth/logout/
    Body: { "refresh": "<refresh_token>" }
    Blacklists the refresh token. Requires
    'rest_framework_simplejwt.token_blacklist' in INSTALLED_APPS.
    """
    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response({'error': 'refresh token is required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            RefreshToken(refresh_token).blacklist()
        except Exception:
            return Response({'error': 'Invalid or already-expired refresh token'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'message': 'Logged out successfully'})


class CustomerChangePasswordView(APIView):
    """
    POST /api/customer-auth/change-password/
    Body: { "old_password": "...", "new_password": "..." }
    """
    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        c = request.user
        old_password = request.data.get('old_password', '')
        new_password = request.data.get('new_password', '')

        if not old_password or not new_password:
            return Response({'error': 'old_password and new_password are required'}, status=status.HTTP_400_BAD_REQUEST)
        if len(new_password) < 6:
            return Response({'error': 'New password must be at least 6 characters'}, status=status.HTTP_400_BAD_REQUEST)
        if not check_password(old_password, c.password_hash):
            return Response({'error': 'old_password is incorrect'}, status=status.HTTP_400_BAD_REQUEST)

        c.password_hash = make_password(new_password)
        c.save()
        return Response({'message': 'Password changed successfully'})