from django.urls import path
from . import views
from django.views.generic import TemplateView

urlpatterns = [
    path('', views.customer_home, name='customer_home'),
    path('products/', views.product_list, name='products'),
    path('cart/', views.cart, name='cart'),
    path('chatbot/', views.chatbot, name='chatbot'),
    path('login/',    views.login_page,      name='customer-login'),
    path('register/', views.register_page,   name='customer-register'),
    path('order-form/', TemplateView.as_view(template_name='customer/order_form.html'), name='order_form'),
]

