from django.contrib import admin
from django.urls import path, include

# JWT
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

urlpatterns = [
    # Admin
    path('admin/', admin.site.urls),

    # Core APIs (status, secure-status)
    path('api/core/', include('core.urls')),

    # Orders APIs (chatbot)
    path('api/', include('orders.urls')),

    # JWT Auth
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
]