# ============================================================
# REPLACE inventory/urls.py with this — adds the Discount Rules
# and Discount Recommendations sections.
# ============================================================

from django.urls import path
from . import views

urlpatterns = [
    # ── Stock snapshot ───────────────────────────────────
    path('inventory/stock/', views.StockSnapshotView.as_view(), name='stock-snapshot'),
    path('inventory/stock/<int:product_id>/', views.ProductStockDetailView.as_view(), name='product-stock-detail'),
    path('inventory/sync-date/', views.SyncDateView.as_view(), name='inventory-sync-date'),

    # ── Alert views ──────────────────────────────────────
    path('inventory/low-stock/', views.LowStockView.as_view(), name='low-stock'),
    path('inventory/out-of-stock/', views.OutOfStockView.as_view(), name='out-of-stock'),

    # ── Ledger & Adjustments ─────────────────────────────
    path('inventory/ledger/', views.StockLedgerView.as_view(), name='stock-ledger'),
    path('inventory/adjust/', views.StockAdjustmentView.as_view(), name='stock-adjust'),

    # ── F06 Product Lifecycle ─────────────────────────────
    path('lifecycle/calculate/', views.LifecycleCalculateView.as_view(), name='lifecycle-calculate'),
    path('lifecycle/declining/', views.LifecycleDecliningView.as_view(), name='lifecycle-declining'),
    path('lifecycle/', views.LifecycleListView.as_view(), name='lifecycle-list'),
    path('lifecycle/<int:product_id>/', views.LifecycleProductHistoryView.as_view(), name='lifecycle-product-history'),

    # ── F07 Loss Records ──────────────────────────────────
    path('losses/summary/', views.LossSummaryView.as_view(), name='loss-summary'),
    path('losses/auto-detect/', views.LossAutoDetectView.as_view(), name='loss-auto-detect'),
    path('losses/', views.LossRecordView.as_view(), name='loss-list'),

    # ── F07 Supplier Returns ──────────────────────────────
    path('supplier-returns/summary/', views.SupplierReturnSummaryView.as_view(), name='supplier-return-summary'),
    path('supplier-returns/<int:pk>/status/', views.SupplierReturnStatusView.as_view(), name='supplier-return-status'),
    path('supplier-returns/', views.SupplierReturnView.as_view(), name='supplier-return-list'),

    # ── F08 Inventory Health Score ────────────────────────
    path('health-scores/calculate/', views.HealthScoreCalculateView.as_view(), name='health-score-calculate'),
    path('health-scores/categories/', views.CategoryHealthScoreView.as_view(), name='health-score-categories'),
    path('health-scores/critical/', views.HealthScoreCriticalView.as_view(), name='health-score-critical'),
    path('health-scores/', views.HealthScoreListView.as_view(), name='health-score-list'),
    path('health-scores/<int:product_id>/', views.HealthScoreDetailView.as_view(), name='health-score-detail'),

    # ── F09 Discount Rules (config CRUD — NOT the calculation engine) ──
    path('discount-rules/', views.DiscountRuleListCreateView.as_view(), name='discount-rule-list'),
    path('discount-rules/<int:pk>/', views.DiscountRuleDetailView.as_view(), name='discount-rule-detail'),

    # ── F09 Discount Recommendations (read + review only) ──
    path('discounts/recommendations/', views.DiscountRecommendationListView.as_view(), name='discount-recommendation-list'),
    path('discounts/recommendations/<int:pk>/', views.DiscountRecommendationDetailView.as_view(), name='discount-recommendation-detail'),

    # ── F10 Reorder Recommendations ───────────────────────── 
    path('reorder/calculate/', views.ReorderCalculateView.as_view(), name='reorder-calculate'),
    path('reorder/recommendations/', views.ReorderRecommendationListView.as_view(), name='reorder-recommendation-list'),
    path('reorder/recommendations/<int:pk>/', views.ReorderRecommendationDetailView.as_view(), name='reorder-recommendation-detail'),

    # ============================================================
# ADD these 3 lines to inventory/urls.py — place anywhere, e.g.
# right after the Reorder section. Note: 'notifications/<int:pk>/read/'
# is registered BEFORE 'notifications/<int:pk>/' is NOT required here
# since both use <int:pk> with different trailing static segments —
# no routing collision, same reasoning as recalculate-wac/ earlier.
# ============================================================

    path('notifications/', views.NotificationListView.as_view(), name='notification-list'),
    path('notifications/<int:pk>/read/', views.NotificationMarkReadView.as_view(), name='notification-mark-read'),
    path('notifications/<int:pk>/', views.NotificationDetailView.as_view(), name='notification-detail'),
 

]