from django.urls import path
from .views import status, secure_status

urlpatterns = [
    # Public endpoint
    path('status/', status),

    # Secure endpoint (JWT required)
    path('secure-status/', secure_status),
]