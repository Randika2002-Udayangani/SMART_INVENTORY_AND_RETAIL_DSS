from django.urls import path

from .views import (
    CustomerRegisterView,
    CustomerLoginView,
    CustomerProfileView,
    OrderCreateView,
    OrderListView,
    OrderStatusUpdateView,
    chatbot,
)


urlpatterns = [

    path(
        "customer-auth/register/",
        CustomerRegisterView.as_view(),
        name="customer-register"
    ),

    path(
        "customer-auth/login/",
        CustomerLoginView.as_view(),
        name="customer-login"
    ),

    path(
        "customer-auth/profile/",
        CustomerProfileView.as_view(),
        name="customer-profile"
    ),


    path(
        "orders/",
        OrderCreateView.as_view(),
        name="order-create"
    ),

    path(
        "orders/list/",
        OrderListView.as_view(),
        name="order-list"
    ),

    path(
        "orders/<int:pk>/status/",
        OrderStatusUpdateView.as_view(),
        name="order-status"
    ),


    path(
        "chatbot/query/",
        chatbot,
        name="chatbot"
    ),

]