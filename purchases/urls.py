from django.urls import path
from . import views

urlpatterns = [
    # ── Purchases ────────────────────────────────────────────────
    # GET  /api/purchases/       — list all purchases
    # POST /api/purchases/       — create new purchase (GRN)
    path('purchases/', views.PurchaseListCreateView.as_view(), name='purchase-list'),

    # GET  /api/purchases/<id>/  — get one purchase with batches
    path('purchases/<int:pk>/', views.PurchaseDetailView.as_view(), name='purchase-detail'),
    path('purchases/upload/invoice/', views.PurchaseInvoicePDFUploadView.as_view(), name='upload-purchase-invoice'),

    

    # ── Batches ──────────────────────────────────────────────────
    # NOTE: expiring-soon and confirm-expiry/bulk must come BEFORE
    # <int:pk> patterns to avoid URL conflicts
    path('batches/expiring-soon/', views.BatchExpiringSoonView.as_view(), name='batch-expiring'),
    path('batches/confirm-expiry/bulk/', views.BulkConfirmBatchExpiryView.as_view(), name='batch-confirm-expiry-bulk'),

    path('batches/', views.BatchListView.as_view(), name='batch-list'),

    path('batches/<int:pk>/status/', views.BatchStatusUpdateView.as_view(), name='batch-status'),
    path('batches/<int:pk>/confirm-expiry/', views.ConfirmBatchExpiryView.as_view(), name='batch-confirm-expiry'),
]