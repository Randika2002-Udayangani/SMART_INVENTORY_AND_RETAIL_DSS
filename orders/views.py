from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.contrib.auth.hashers import make_password, check_password
from django.utils import timezone
from .models import Customer
from .tokens import get_tokens_for_customer


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


class CustomerProfileView(APIView):
    """
    GET /api/customer-auth/profile/
    Requires customer token.
    """

    def get(self, request):
        # For now return a simple response
        # Full customer token validation added in Week 4
        return Response({'message': 'Profile endpoint — token validation coming Week 4'})