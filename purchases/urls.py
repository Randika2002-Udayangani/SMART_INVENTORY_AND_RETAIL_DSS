from django.urls import path
from . import views

urlpatterns = [
    # ── Purchases ────────────────────────────────────────────────
    # GET  /api/purchases/       — list all purchases
    # POST /api/purchases/       — create new purchase (GRN)
    path('purchases/', views.PurchaseListCreateView.as_view(), name='purchase-list'),

    # GET  /api/purchases/<id>/  — get one purchase with batches
    path('purchases/<int:pk>/', views.PurchaseDetailView.as_view(), name='purchase-detail'),

    # ── Batches ──────────────────────────────────────────────────
    # NOTE: expiring-soon must come BEFORE <int:pk> to avoid URL conflict
    # GET  /api/batches/expiring-soon/?days=30
    path('batches/expiring-soon/', views.BatchExpiringSoonView.as_view(), name='batch-expiring'),

    # GET  /api/batches/          — list all batches (?status=ACTIVE &product=<id>)
    path('batches/', views.BatchListView.as_view(), name='batch-list'),

    # PATCH /api/batches/<id>/status/  — update batch status
    path('batches/<int:pk>/status/', views.BatchStatusUpdateView.as_view(), name='batch-status'),
]