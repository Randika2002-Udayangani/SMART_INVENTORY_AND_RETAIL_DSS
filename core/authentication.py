from rest_framework_simplejwt.authentication import JWTAuthentication


class LenientJWTAuthentication(JWTAuthentication):
    """
    Behaves exactly like JWTAuthentication, except it never raises
    on a malformed/foreign token — e.g. a customer JWT hitting a
    staff-scoped view. Instead it treats it as no-credentials-given,
    so AllowAny views stay genuinely public even when a customer
    happens to be logged in and their token gets attached anyway.
    Permission classes (IsAuthenticated on writes) still enforce
    security exactly as before — this only changes how a *bad*
    token is interpreted, not whether access is granted.
    """
    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except Exception:
            return None