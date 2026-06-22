from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.hashers import check_password
from .models import Customer


 
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import TokenError
from .models import Customer

class CustomerAuthBackend(BaseBackend):
    """
    Custom authentication backend for Customer portal.
    Checks email + password against Customer table.
    Completely separate from staff App_User auth.
    """

    def authenticate(self, request, email=None, password=None):
        try:
            customer = Customer.objects.get(email=email, is_active=True)
            if check_password(password, customer.password_hash):
                return customer
        except Customer.DoesNotExist:
            return None

    def get_user(self, user_id):
        try:
            return Customer.objects.get(pk=user_id)
        except Customer.DoesNotExist:
            return None
        
# ============================================================
# APPEND to orders/authentication.py — keep CustomerAuthBackend above
# ============================================================

 
 
class CustomerJWTAuthentication(BaseAuthentication):
    """
    DRF authentication class for customer-facing endpoints.
    Validates the JWT issued by tokens.get_tokens_for_customer() and
    resolves it to a real Customer row (NOT a Django auth User —
    that's why the default JWTAuthentication can't be reused here).
 
    Use explicitly on customer views:
        authentication_classes = [CustomerJWTAuthentication]
 
    Do NOT add this to DEFAULT_AUTHENTICATION_CLASSES in settings.py —
    staff endpoints must keep using the standard JWTAuthentication
    against auth_user. This class is opt-in per view only.
    """
 
    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header.startswith('Bearer '):
            return None
 
        raw_token = auth_header.split(' ', 1)[1]
 
        try:
            token = AccessToken(raw_token)
        except TokenError:
            raise AuthenticationFailed('Invalid or expired token')
 
        if token.get('type') != 'customer':
            # Not a customer token (probably a staff token) — return None
            # so this stays a no-op instead of blocking staff requests.
            return None
 
        customer_id = token.get('customer_id')
        try:
            customer = Customer.objects.get(pk=customer_id, is_active=True)
        except Customer.DoesNotExist:
            raise AuthenticationFailed('Customer not found or inactive')
 
        return (customer, token)
 
