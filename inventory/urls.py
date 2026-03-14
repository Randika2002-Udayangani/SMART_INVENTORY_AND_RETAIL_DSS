from django.urls import path
from . import views

urlpatterns = [
    # ── Stock snapshot ───────────────────────────────────────────
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

    # ── F06 Product Lifecycle ────────────────────────────────────
    # ⚠ CRITICAL ORDER: calculate/ and declining/ MUST be registered
    # BEFORE <int:product_id>/ — otherwise Django casts 'declining'
    # and 'calculate' as integers and returns 404/500.
    path('lifecycle/calculate/',
         views.LifecycleCalculateView.as_view(),
         name='lifecycle-calculate'),
    path('lifecycle/declining/',
         views.LifecycleDecliningView.as_view(),
         name='lifecycle-declining'),
    path('lifecycle/',
         views.LifecycleListView.as_view(),
         name='lifecycle-list'),
    path('lifecycle/<int:product_id>/',
         views.LifecycleProductHistoryView.as_view(),
         name='lifecycle-product-history'),



         # ── F07 Loss Records ─────────────────────────────────────────
    # ⚠ summary/ and auto-detect/ MUST be before any parameterised paths
    path('losses/summary/',
         views.LossSummaryView.as_view(),
         name='loss-summary'),
    path('losses/auto-detect/',
         views.LossAutoDetectView.as_view(),
         name='loss-auto-detect'),
    path('losses/',
         views.LossRecordView.as_view(),
         name='loss-list'),

    # ── F07 Supplier Returns ─────────────────────────────────────
    # ⚠ summary/ MUST be before <int:pk>/status/
    path('supplier-returns/summary/',
         views.SupplierReturnSummaryView.as_view(),
         name='supplier-return-summary'),
    path('supplier-returns/<int:pk>/status/',
         views.SupplierReturnStatusView.as_view(),
         name='supplier-return-status'),
    path('supplier-returns/',
         views.SupplierReturnView.as_view(),
         name='supplier-return-list'),
         # ── F07 Loss Records ─────────────────────────────────────────
    # ⚠ summary/ and auto-detect/ MUST be before any parameterised paths
    path('losses/summary/',
         views.LossSummaryView.as_view(),
         name='loss-summary'),
    path('losses/auto-detect/',
         views.LossAutoDetectView.as_view(),
         name='loss-auto-detect'),
    path('losses/',
         views.LossRecordView.as_view(),
         name='loss-list'),

    # ── F07 Supplier Returns ─────────────────────────────────────
    # ⚠ summary/ MUST be before <int:pk>/status/
    path('supplier-returns/summary/',
         views.SupplierReturnSummaryView.as_view(),
         name='supplier-return-summary'),
    path('supplier-returns/<int:pk>/status/',
         views.SupplierReturnStatusView.as_view(),
         name='supplier-return-status'),
    path('supplier-returns/',
         views.SupplierReturnView.as_view(),
         name='supplier-return-list'),

     # ── F08 Inventory Health Score ───────────────────────────────
     # ⚠ CRITICAL ORDER: categories/ and critical/ MUST be before
     # <int:product_id>/ to avoid Django int cast errors
    path('health-scores/calculate/',
         views.HealthScoreCalculateView.as_view(),
         name='health-score-calculate'),
    path('health-scores/categories/',
         views.CategoryHealthScoreView.as_view(),
         name='health-score-categories'),
    path('health-scores/critical/',
         views.HealthScoreCriticalView.as_view(),
         name='health-score-critical'),
    path('health-scores/',
         views.HealthScoreListView.as_view(),
         name='health-score-list'),
    path('health-scores/<int:product_id>/',
         views.HealthScoreDetailView.as_view(),
         name='health-score-detail'),
]


