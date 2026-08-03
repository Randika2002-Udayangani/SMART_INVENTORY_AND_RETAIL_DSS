"""
URL configuration for smart_inventory project.
"""

from django.http import JsonResponse
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)
from rest_framework_simplejwt.views import TokenRefreshView
from users.views import LockoutTokenObtainPairView

def home(request):
    return JsonResponse({
        "message": "Smart Inventory & Retail DSS API is running",
        "status": "OK"
    })


urlpatterns = [

    path("", home),

    path("admin/", admin.site.urls),

    # JWT Authentication
    path("api/auth/login/", LockoutTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/auth/refresh/", TokenRefreshView.as_view(), name="token_refresh"),

    # Core
    path("api/core/", include("core.urls")),

    # Orders & Chatbot
    path("api/", include("orders.urls")),

    # Other Apps
    path("api/", include("users.urls")),
    path("api/", include("products.urls")),
    path("api/", include("suppliers.urls")),
    path("api/", include("purchases.urls")),
    path("api/", include("inventory.urls")),
    path("api/", include("sales.urls")),
    path("api/", include("analytics.urls")),

    # Dashboard
    path("dashboard/", include("dashboard.urls")),
]


if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT
    )