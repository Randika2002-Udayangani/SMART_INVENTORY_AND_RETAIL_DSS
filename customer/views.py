from django.shortcuts import render
from django.http import HttpResponse

def customer_home(request):
    return render(request, 'customer/products.html')

def product_list(request):
    return render(request, 'customer/products.html')

def cart(request):
    return HttpResponse("Cart page coming soon.")

def cart_page(request):
    return render(request, 'customer/cart.html')

def chatbot(request):
    return render(request, 'customer/chatbot.html')

def login_page(request):
    return render(request, 'customer/login.html')

def register_page(request):
    return render(request, 'customer/register.html')

def order_form(request):
    return render(request, 'customer/order_form.html')

def my_orders(request):
    return render(request, 'customer/my_orders.html')

def profile_page(request):
    return render(request, 'customer/profile.html')

def product_detail_page(request):
    return render(request, 'customer/product_detail.html')