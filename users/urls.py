from django.urls import path
from . import views

urlpatterns = [
    # POST /api/users/  — admin/staff creates a new staff account (ADMIN/MANAGER/STAFF)
    # Moved off customer-auth/register/ — that URL belongs to orders.urls (real Customer model)
    path('users/', views.RegisterView.as_view(), name='user-register'),
]