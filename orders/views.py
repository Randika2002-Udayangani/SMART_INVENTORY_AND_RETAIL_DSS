from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from django.contrib.auth.hashers import make_password, check_password
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

import json

from core.models import AuditLog
from .models import Customer
from .tokens import get_tokens_for_customer
from .chatbot import detect_intent


# =========================================================
# CUSTOMER AUTH APIs
# =========================================================

class CustomerRegisterView(APIView):
    """
    POST /api/customer-auth/register/
    Public — no token needed.
    """

    permission_classes = []

    def post(self, request):

        name = request.data.get('name')
        email = request.data.get('email')
        password = request.data.get('password')
        contact_number = request.data.get('contact_number', '')
        address = request.data.get('address', '')

        if not name or not email or not password:
            return Response(
                {'error': 'name, email, and password are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if Customer.objects.filter(email=email).exists():
            return Response(
                {'error': 'An account with this email already exists'},
                status=status.HTTP_400_BAD_REQUEST
            )

        customer = Customer.objects.create(
            name=name,
            email=email,
            password_hash=make_password(password),
            contact_number=contact_number,
            address=address,
            is_active=True,
        )

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
    """

    permission_classes = []

    def post(self, request):

        email = request.data.get('email')
        password = request.data.get('password')

        if not email or not password:
            return Response(
                {'error': 'email and password are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            customer = Customer.objects.get(
                email=email,
                is_active=True
            )

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
    """

    def get(self, request):

        return Response({
            'message': 'Profile endpoint — token validation coming Week 4'
        })


# =========================================================
# CHATBOT API
# =========================================================

@csrf_exempt
def chatbot(request):

    if request.method == "POST":

        data = json.loads(request.body)

        message = data.get("message", "")

        intent = detect_intent(message)

        return JsonResponse({
            "message": message,
            "intent": intent
        })

    return JsonResponse({
        "error": "POST method required"
    }, status=405)


# =========================================================
# ORDER STATUS UPDATE API
# =========================================================

@csrf_exempt
def update_order_status(request):

    if request.method == "PUT":

        data = json.loads(request.body)

        order_ref = data.get("order_ref")
        order_status = data.get("status")

        if not order_ref:
            return JsonResponse({
                "error": "order_ref required"
            }, status=400)

        if not order_status:
            return JsonResponse({
                "error": "status required"
            }, status=400)

        if order_status == "READY":

            AuditLog.objects.create(
                action=f"Order {order_ref} is ready for pickup"
            )

        elif order_status == "CANCELLED":

            AuditLog.objects.create(
                action=f"Order {order_ref} has been cancelled"
            )

        elif order_status == "PROCESSING":

            AuditLog.objects.create(
                action=f"Order {order_ref} is processing"
            )

        return JsonResponse({
            "message": "Order updated successfully",
            "order_ref": order_ref,
            "status": order_status
        })

    return JsonResponse({
        "error": "PUT method required"
    }, status=405)