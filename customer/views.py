from django.shortcuts import render

def product_list(request):
    return render(request, "customer/products.html")

def cart(request):
    return render(request, "customer/cart.html")

def chatbot(request):
    return render(request, "customer/chatbot.html")

def customer_home(request):
    return render(request, 'customer/home.html') 