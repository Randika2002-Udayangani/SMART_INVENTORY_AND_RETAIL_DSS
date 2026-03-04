from django.urls import path
from . import views

urlpatterns = [
    # Purchases — specific before generic
    path('purchases/', views.PurchaseListCreateView.as_view(), name='purchase-list'),
    path('purchases/<int:pk>/', views.PurchaseDetailView.as_view(), name='purchase-detail'),

    # Batches — specific before generic
    path('batches/', views.BatchListView.as_view(), name='batch-list'),
    path('batches/expiring-soon/', views.BatchExpiringSoonView.as_view(), name='batch-expiring'),
    path('batches/<int:pk>/status/', views.BatchStatusUpdateView.as_view(), name='batch-status'),
]