from django.urls import path
from . import views

urlpatterns = [
    # Upload endpoints — specific before generic (Risk R12)
    path('sales/upload/item-ledger/', views.ItemLedgerPDFUploadView.as_view(), name='upload-item-ledger'),
    path('sales/upload/daily-bills/', views.DailyBillsUploadView.as_view(), name='upload-daily-bills'),
    path('sales/upload-log/', views.UploadLogListView.as_view(), name='upload-log-list'),
    path('sales/upload-log/<int:pk>/', views.UploadLogDetailView.as_view(), name='upload-log-detail'),
    path('sales/item-sales/', views.ItemSalesListView.as_view(), name='item-sales-list'),
]