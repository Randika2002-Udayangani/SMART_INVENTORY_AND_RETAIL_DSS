from django.urls import path
from inventory.views import (
    HealthScoreListView,
    HealthScoreCriticalView,
    CategoryHealthScoreView,
    LifecycleListView,
    LifecycleDecliningView,
    LossSummaryView,
    lifecycle_analytics,
    HealthScoreSummaryView,
)

from sales.views import ItemSalesListView
from sales.views import profit_summary
from analytics.views import slow_moving, sales_trend, category_performance, store_revenue

urlpatterns = [
    # ── F05 Profit & Analytics ────────────────────────────
    path('analytics/item-sales/', ItemSalesListView.as_view(), name='analytics-item-sales'),

    # ── F06 Lifecycle ─────────────────────────────────────
    path('analytics/lifecycle/', lifecycle_analytics, name='analytics-lifecycle'),
    path('analytics/lifecycle/declining/', LifecycleDecliningView.as_view(), name='analytics-lifecycle-declining'),

    # ── F07 Loss Summary ──────────────────────────────────
    path('analytics/loss-summary/', LossSummaryView.as_view(), name='analytics-loss-summary'),

    # ── F08 Health Scores ─────────────────────────────────
    path('analytics/health-scores/', HealthScoreSummaryView.as_view(), name='analytics-health-scores'),
    path('analytics/health-scores/critical/', HealthScoreCriticalView.as_view(), name='analytics-health-critical'),
    path('analytics/health-scores/categories/', CategoryHealthScoreView.as_view(), name='analytics-health-categories'),

    # ── Profit summary (M1 requested) ─────────────────────
    path('analytics/profit-summary/', profit_summary, name='analytics-profit-summary'),

    # ── F05-D..G: remaining analytics endpoints (M2 — Nipuni) ─────
    path('analytics/slow-moving/', slow_moving, name='analytics-slow-moving'),
    path('analytics/sales-trend/', sales_trend, name='analytics-sales-trend'),
    path('analytics/category-performance/', category_performance, name='analytics-category-performance'),
    path('analytics/store-revenue/', store_revenue, name='analytics-store-revenue'),
]