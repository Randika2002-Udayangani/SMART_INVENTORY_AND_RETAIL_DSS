from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import AppUser


class StaffJWTAuthentication(JWTAuthentication):

    def get_user(self, validated_token):

        user_id = validated_token.get("user_id")


        if not user_id:
            raise AuthenticationFailed(
                "Invalid token"
            )


        try:
            user = AppUser.objects.get(
                id=user_id
            )

        except AppUser.DoesNotExist:
            raise AuthenticationFailed(
                "User not found"
            )


        if not user.is_active:
            raise AuthenticationFailed(
                "Inactive user"
            )


        return user