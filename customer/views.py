from django.shortcuts import render

def product_list(request):
    return render(request, "customer/products.html")

def cart(request):
    return render(request, "customer/cart.html")

def chatbot(request):
    return render(request, "customer/chatbot.html")

def customer_home(request):
    return render(request, 'customer/home.html') 

def login_page(request):
    return render(request, 'customer/login.html')

def register_page(request):
    return render(request, 'customer/register.html')

def order_form(request):
    return render(request, 'customer/order_form.html')