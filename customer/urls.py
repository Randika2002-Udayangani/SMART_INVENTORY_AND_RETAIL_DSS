from django.urls import path
from . import views

urlpatterns = [
    path('', views.product_list, name='customer_home'),
    path('products/', views.product_list, name='products'),
    path('cart/', views.cart_page, name='cart-page'),
    path('chatbot/', views.chatbot, name='chatbot'),
    path('login/', views.login_page, name='customer-login'),
    path('register/', views.register_page, name='customer-register'),
    path('order/', views.order_form, name='order-form'),
    path('my-orders/', views.my_orders, name='my-orders'),
    path('profile/', views.profile_page, name='customer-profile'),
    path('product/', views.product_detail_page, name='product-detail'),
]