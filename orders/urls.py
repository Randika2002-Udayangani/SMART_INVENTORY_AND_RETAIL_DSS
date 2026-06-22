# ============================================================
# REPLACE orders/urls.py with this — only adds 3 new paths.
# If your real file already has Orders API / chatbot paths added
# since this snapshot, just add the 3 new lines manually instead
# of overwriting the whole file.
# ============================================================

from django.urls import path
from . import views

urlpatterns = [
    path('customer-auth/register/', views.CustomerRegisterView.as_view(), name='customer-register'),
    path('customer-auth/login/', views.CustomerLoginView.as_view(), name='customer-login'),
    path('customer-auth/logout/', views.CustomerLogoutView.as_view(), name='customer-logout'),
    path('customer-auth/profile/', views.CustomerProfileView.as_view(), name='customer-profile'),
    path('customer-auth/change-password/', views.CustomerChangePasswordView.as_view(), name='customer-change-password'),
]