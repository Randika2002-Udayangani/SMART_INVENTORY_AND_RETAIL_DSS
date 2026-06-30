from django.urls import path
from inventory.views import (
    HealthScoreListView,
    HealthScoreCriticalView,
    CategoryHealthScoreView,
    LifecycleListView,
    LifecycleDecliningView,
    LossSummaryView,
    lifecycle_analytics,
)

from sales.views import ItemSalesListView
from sales.views import profit_summary

urlpatterns = [
    # ── F05 Profit & Analytics ────────────────────────────
    path('analytics/item-sales/', ItemSalesListView.as_view(), name='analytics-item-sales'),

    # ── F06 Lifecycle ─────────────────────────────────────
    path('analytics/lifecycle/', lifecycle_analytics, name='analytics-lifecycle'),
    path('analytics/lifecycle/declining/', LifecycleDecliningView.as_view(), name='analytics-lifecycle-declining'),

    # ── F07 Loss Summary ──────────────────────────────────
    path('analytics/loss-summary/', LossSummaryView.as_view(), name='analytics-loss-summary'),

    # ── F08 Health Scores ─────────────────────────────────
    path('analytics/health-scores/', HealthScoreListView.as_view(), name='analytics-health-scores'),
    path('analytics/health-scores/critical/', HealthScoreCriticalView.as_view(), name='analytics-health-critical'),
    path('analytics/health-scores/categories/', CategoryHealthScoreView.as_view(), name='analytics-health-categories'),

    # ── Profit summary (M1 requested) ─────────────────────
    path('analytics/profit-summary/', profit_summary, name='analytics-profit-summary'),
]