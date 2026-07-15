from django.shortcuts import render

def dashboard_home(request):
    return render(request, 'dashboard/home.html')

def dashboard_login(request):
    return render(request, 'dashboard/login.html')

def sales_report(request):
    return render(request, 'dashboard/sales_report.html')