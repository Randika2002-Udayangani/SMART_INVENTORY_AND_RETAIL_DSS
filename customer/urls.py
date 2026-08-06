from django.urls import path
from . import views

urlpatterns = [
    path('', views.customer_home, name='customer_home'),
    path('products/', views.product_list, name='products'),
    path('cart/', views.cart, name='cart'),
    path('chatbot/', views.chatbot, name='chatbot'),
]
