from django.shortcuts import render

def product_list(request):
    return render(request, "customer/product_list.html")

