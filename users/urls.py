from django.urls import path
from . import views

urlpatterns = [
    # POST /api/customer-auth/register/  — create new user
    path('customer-auth/register/', views.RegisterView.as_view(), name='auth-register'),

    # POST /api/customer-auth/login/     — login and get tokens
    path('customer-auth/login/', views.LoginView.as_view(), name='auth-login'),
]