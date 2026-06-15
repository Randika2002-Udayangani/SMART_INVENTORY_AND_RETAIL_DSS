from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from django.http import HttpResponse
from customer import views

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

def home(request):
    return HttpResponse("Frontend Working ✅")

urlpatterns = [
    # Home
    path('', home),

    # Admin
    path('admin/', admin.site.urls),

    # JWT Authentication
    path('api/auth/login/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/auth/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    # Customer frontend pages  ← THIS was missing before
    path('customer/', include('customer.urls')),
    path('cart/', views.cart, name='cart'),

    # App API endpoints
    path('api/', include('users.urls')),
    path('api/', include('products.urls')),
    path('api/', include('suppliers.urls')),
    path('api/', include('purchases.urls')),
    path('api/', include('inventory.urls')),
    path('api/', include('orders.urls')),
    path('api/', include('sales.urls')),
    path('api/', include('analytics.urls')),

    # Dashboard
    path('dashboard/', include('dashboard.urls')),

] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)