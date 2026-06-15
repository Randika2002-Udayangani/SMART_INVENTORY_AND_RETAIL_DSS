from django.urls import path
from . import views

urlpatterns = [
    path('customer-auth/register/', views.CustomerRegisterView.as_view(), name='customer-register'),
    path('customer-auth/login/', views.CustomerLoginView.as_view(), name='customer-login'),
    path('customer-auth/profile/', views.CustomerProfileView.as_view(), name='customer-profile'),
]


from .views import (
    chatbot,
    update_order_status
)

urlpatterns = [

    # Chatbot API
    path(
        'chatbot/',
        chatbot
    ),

    # Order Status Update API
    path(
        'orders/update-status/',
        update_order_status
    ),
]