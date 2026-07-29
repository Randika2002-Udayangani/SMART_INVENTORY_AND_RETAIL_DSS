from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.hashers import check_password
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