from django.urls import path
from . import views

urlpatterns = [
    # Stock — specific before generic
    path('inventory/stock/', views.StockSnapshotView.as_view(), name='stock-snapshot'),
    path('inventory/low-stock/', views.LowStockView.as_view(), name='low-stock'),
    path('inventory/out-of-stock/', views.OutOfStockView.as_view(), name='out-of-stock'),
    path('inventory/ledger/', views.StockLedgerView.as_view(), name='stock-ledger'),
    path('inventory/adjust/', views.StockAdjustmentView.as_view(), name='stock-adjust'),
    path('inventory/stock/<int:product_id>/', views.ProductStockDetailView.as_view(), name='product-stock'),
]