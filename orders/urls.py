from django.urls import path
from . import views

urlpatterns = [
    path('customer-auth/register/', views.CustomerRegisterView.as_view(), name='customer-register'),
    path('customer-auth/login/', views.CustomerLoginView.as_view(), name='customer-login'),
    path('customer-auth/profile/', views.CustomerProfileView.as_view(), name='customer-profile'),
]