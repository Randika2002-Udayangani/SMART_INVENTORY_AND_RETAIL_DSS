from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from rest_framework_simplejwt.tokens import AccessToken

from .models import Customer


class CustomerJWTAuthentication(BaseAuthentication):

    def authenticate(self, request):

        auth_header = request.headers.get("Authorization")

        if not auth_header:
            return None


        if not auth_header.startswith("Bearer "):
            raise AuthenticationFailed(
                "Invalid authorization header"
            )


        token = auth_header.split(" ")[1]


        try:
            access_token = AccessToken(token)

        except Exception:
            raise AuthenticationFailed(
                "Invalid token"
            )


        if access_token.get("type") != "customer":
            raise AuthenticationFailed(
                "Not a customer token"
            )


        try:

            customer = Customer.objects.get(
                id=access_token["customer_id"],
                is_active=True
            )


        except Customer.DoesNotExist:

            raise AuthenticationFailed(
                "Customer not found"
            )


        return (customer, token)