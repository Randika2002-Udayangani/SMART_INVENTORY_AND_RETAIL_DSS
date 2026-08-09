from django.shortcuts import render

def dashboard_home(request):
    return render(request, 'dashboard/home.html')

def dashboard_login(request):
    return render(request, 'dashboard/login.html')

def sales_report(request):
    return render(request, 'dashboard/sales_report.html')

def loss_analysis(request):
    return render(request, 'dashboard/loss_analysis.html')

def lifecycle(request):
    return render(request, 'dashboard/lifecycle.html')

def health_score(request):
    return render(request, 'dashboard/health_score.html')

def reorder(request):
    return render(request, 'dashboard/reorder.html')

def analytics(request):
    return render(request, 'dashboard/analytics.html')

def discount_engine(request):
    return render(request, 'dashboard/discount_engine.html')

def inventory(request):
    return render(request, 'dashboard/inventory.html')

def purchases(request):
    return render(request, 'dashboard/purchases.html')

def suppliers(request):
    return render(request, 'dashboard/suppliers.html')

def products(request):
    return render(request, 'dashboard/products.html')

def inventory(request):
    return render(request, 'dashboard/inventory.html')
