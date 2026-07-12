from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard_home, name='dashboard_home'),
    path('login/', views.dashboard_login, name='dashboard_login'),
    path('sales-report/', views.sales_report, name='sales_report'),
    path('loss-analysis/', views.loss_analysis, name='loss_analysis'),
    path('lifecycle/', views.lifecycle, name='lifecycle'),
]