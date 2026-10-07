from django.urls import path
from . import views

urlpatterns = [
    # ⚠ Static paths MUST be registered before suppliers/<int:pk>/ —
    # Django resolves top-to-bottom (see API Design Doc v3.1 §1 routing note)
    path('suppliers/scorecard-summary/', views.SupplierScorecardSummaryView.as_view(), name='supplier-scorecard-summary'),
    path('suppliers/<int:pk>/scorecard/', views.SupplierScorecardDetailView.as_view(), name='supplier-scorecard-detail'),
    path('suppliers/<int:pk>/cost-trend/', views.SupplierCostTrendView.as_view(), name='supplier-cost-trend'),

    path('suppliers/', views.SupplierListCreateView.as_view(), name='supplier-list'),
    path('suppliers/<int:pk>/', views.SupplierDetailView.as_view(), name='supplier-detail'),
]