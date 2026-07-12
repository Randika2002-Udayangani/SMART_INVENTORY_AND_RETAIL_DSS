from django.http import JsonResponse
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

def home(request):
    return JsonResponse({
        "message": "Smart Inventory & Retail DSS API is running",
        "status": "OK"
    })

urlpatterns = [
    
    path("", home),
    path('admin/', admin.site.urls),

    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    path('api/core/', include('core.urls')),

    path('api/', include('users.urls')),
    path('api/', include('products.urls')),
    path('api/', include('suppliers.urls')),
    path('api/', include('purchases.urls')),
    path('api/', include('inventory.urls')),
    path('api/', include('orders.urls')),
    path('api/', include('sales.urls')),
    path('api/', include('analytics.urls')),

    path('dashboard/', include('dashboard.urls')),
]


if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT
    )