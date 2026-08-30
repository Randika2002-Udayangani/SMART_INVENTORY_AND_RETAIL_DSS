from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard_home, name='dashboard_home'),
    path('login/', views.dashboard_login, name='dashboard_login'),
    path('sales-report/', views.sales_report, name='sales_report'),
    path('sales-upload/', views.sales_upload, name='sales_upload'),
    path('reports/', views.reports, name='reports'),
    path('notifications/', views.notifications, name='notifications'),
    path('loss-analysis/', views.loss_analysis, name='loss_analysis'),
    path('lifecycle/', views.lifecycle, name='lifecycle'),
    path('health-score/', views.health_score, name='health_score'),
    path('reorder/', views.reorder, name='reorder'),
    path('analytics/', views.analytics, name='analytics'),
    path('discount-engine/', views.discount_engine, name='discount_engine'),
    path('inventory/', views.inventory, name='inventory'),
    path('purchases/', views.purchases, name='purchases'),
    path('suppliers/', views.suppliers, name='suppliers'),
    path('products/', views.products, name='products'),
    path('system-config/', views.system_config, name='system_config'),
    path('audit-log/', views.audit_log, name='audit_log'),
    path('users/', views.user_management, name='user_management'),
]
