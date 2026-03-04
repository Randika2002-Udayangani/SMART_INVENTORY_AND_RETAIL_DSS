from django.urls import path
from . import views

urlpatterns = [
    # ── Stock snapshot ───────────────────────────────────────────
    # GET /api/inventory/stock/              — all products stock
    # GET /api/inventory/stock/<product_id>/ — one product detail
    # NOTE: specific paths must come BEFORE parameterised paths
    path('inventory/stock/',
         views.StockSnapshotView.as_view(),
         name='stock-snapshot'),

    path('inventory/stock/<int:product_id>/',
         views.ProductStockDetailView.as_view(),
         name='product-stock-detail'),

    # ── Alert views ──────────────────────────────────────────────
    path('inventory/low-stock/',
         views.LowStockView.as_view(),
         name='low-stock'),

    path('inventory/out-of-stock/',
         views.OutOfStockView.as_view(),
         name='out-of-stock'),

    # ── Ledger & Adjustments ─────────────────────────────────────
    path('inventory/ledger/',
         views.StockLedgerView.as_view(),
         name='stock-ledger'),

    path('inventory/adjust/',
         views.StockAdjustmentView.as_view(),
         name='stock-adjust'),
]