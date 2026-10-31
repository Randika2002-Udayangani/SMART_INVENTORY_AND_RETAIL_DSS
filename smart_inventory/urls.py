"""
URL configuration for smart_inventory project.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path, include
from django.http import HttpResponse
from customer import views
from customer import views as customer_views

from rest_framework_simplejwt.views import TokenRefreshView
from users.views import LockoutTokenObtainPairView
from dashboard.views import dashboard_login

urlpatterns = [
    # Home
    path('', customer_views.landing_page, name='landing'),

    # JWT Authentication
    path('api/auth/login/', LockoutTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/auth/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    # Customer frontend pages
    path('customer/', include('customer.urls')),
    path('cart/', views.cart, name='cart'),

    # App API endpoints
    path('api/', include('core.urls')),
    path('api/', include('users.urls')),
    path('api/', include('products.urls')),
    path('api/', include('suppliers.urls')),
    path('api/', include('purchases.urls')),
    path('api/', include('inventory.urls')),
    path('api/', include('orders.urls')),
    path('api/', include('sales.urls')),
    path('api/', include('analytics.urls')),

    # Private staff login
    path(
        f'{settings.STAFF_LOGIN_PATH}/',
        dashboard_login,
        name='dashboard_login',
    ),

    # Dashboard
    path('dashboard/', include('dashboard.urls')),

] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
