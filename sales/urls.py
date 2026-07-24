from django.urls import path
from . import views

urlpatterns = [

    # Upload endpoints
    path(
        'sales/upload/item-ledger/',
        views.ItemLedgerPDFUploadView.as_view(),
        name='upload-item-ledger'
    ),

    path(
        'sales/upload/daily-bills/',
        views.DailyBillsUploadView.as_view(),
        name='upload-daily-bills'
    ),

    path(
        'sales/upload-log/',
        views.UploadLogListView.as_view(),
        name='upload-log-list'
    ),

    path(
        'sales/upload-log/<int:pk>/',
        views.UploadLogDetailView.as_view(),
        name='upload-log-detail'
    ),

    path(
        'sales/item-sales/',
        views.ItemSalesListView.as_view(),
        name='item-sales-list'
    ),

    path(
        'sales/daily-bills/',
        views.DailyBillsListView.as_view(),
        name='daily-bills-list'
    ),

    path(
        'reports/sales-summary/',
        views.sales_summary,
        name='sales-summary'
    ),

    path(
        'reports/expiry-summary/',
        views.expiry_summary,
        name='expiry-summary'
    ),

    path(
        'reports/sales/',
        views.sales_report_export,
        name='sales-report-export'
    ),

    path(
        'reports/profit/',
        views.profit_report_export,
        name='profit-report-export'
    ),

    path(
        'reports/inventory/',
        views.inventory_report_export,
        name='inventory-report-export'
    ),

    path(
        'reports/health-scores/',
        views.health_score_report_export,
        name='health-score-report-export'
    ),

    path(
        'reports/supplier/',
        views.supplier_report_export,
        name='supplier-report-export'
    ),

    path(
        'reports/lifecycle/',
        views.lifecycle_report_export,
        name='lifecycle-report-export'
    ),




]