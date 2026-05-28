from django.urls import path

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