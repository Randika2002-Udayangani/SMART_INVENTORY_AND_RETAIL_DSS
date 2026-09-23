from datetime import date, timedelta  
import csv
import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from users.permissions import IsManagerOrAdmin  
from decimal import Decimal
from users.audit import log_action

from django.db import transaction
from django.db.models import Sum
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser

from products.models import Product
from purchases.models import Purchase, PurchaseBatch
from suppliers.models import Supplier
from users.models import SystemConfig
from .models import (
    StockLedger, StockAdjustment, ProductLifecycle,
    LossRecord, SupplierReturn,
    InventoryHealthScore, CategoryHealthScore,
    DiscountRule, DiscountRecommendation, 
    ReorderRecommendation,
)
from .serializers import (
    StockLedgerSerializer, StockAdjustmentSerializer, CurrentStockSerializer, 
    DiscountRuleSerializer, DiscountRecommendationSerializer, 
    ReorderRecommendationSerializer,
)
from sales.models import ItemSalesRecord
from sales.models import UploadLog
from inventory.services.reorder_logic import get_urgency
from inventory.services.fefo import deduct_stock_fefo


from inventory.services.reorder_logic import check_reorder_needs

from orders.models import Notification

from datetime import date
#   from inventory.services.reorder_logic import check_reorder_needs


from django.utils import timezone as dj_timezone

from django.utils import timezone as dj_timezone
from core.utils import get_last_sync_date, get_latest_sync_uploads
from core.pagination import StandardResultsPagination


# Same fix pattern as notify_expiring_batches.py, notify_missing_uploads.py,
# lifecycle.py, health_score.py and analytics/views.py: date.today() reads
# the SERVER's own OS clock, not Django's TIME_ZONE setting. Works fine on
# local machines already set to Sri Lanka time, but silently wrong the
# moment this runs on a UTC-clocked host (Render included).
LOCAL_TZ = ZoneInfo("Asia/Colombo")


# ═════════════════════════════════════════════════════════════════
# F03 — Inventory & Stock
# ═════════════════════════════════════════════════════════════════

class StockSnapshotView(APIView):
    """
    GET /api/inventory/stock/?search=&status=AVAILABLE|LOW|OUT&page=&page_size=

    FIX (performance, N+1): previously ran one PurchaseBatch aggregate query
    PER product inside the loop below (N+1 — e.g. 500 active products
    meant 500 extra queries on this one page load). Same bug already fixed
    in LowStockView further down this file (see its "Bulk-fetch all active
    batch stock in one query" comment) — StockSnapshotView just never got
    the same fix applied. Now uses the identical bulk-fetch pattern: one
    query for every product's stock, then a dict lookup inside the loop
    instead of a query.

    FIX (pagination): previously returned every active product in one
    response with no pagination at all — fine for a small catalogue, but
    for 500-2000+ products this meant inventory.html's loadStock() pulled
    the entire table on every page load just to render 25 rows, and
    inventory.html computed its Total/Low/Out KPI cards by counting that
    same full client-side array. Search/status filtering also happened
    entirely in the browser over the fully-loaded list.

    The expensive part (DB query count) was ALREADY correct after the N+1
    fix above — this only changes what gets serialized into the HTTP
    response. Stock is still computed for every active product in one bulk
    query + one Python loop (cheap, no per-product queries); search/status
    filtering and pagination now happen on that same in-memory list before
    it's returned, instead of after it reaches the browser.

    KPI totals (Total/Low/Out) must NOT be computed from this endpoint's
    paginated response — see StockSummaryView below, same fix pattern as
    HealthScoreSummaryView.

    Note: this is a plain APIView, not generics.ListAPIView, so pagination
    is hand-rolled below (matching HealthScoreListView's envelope) rather
    than via `pagination_class` — that attribute only does something on
    DRF's generic list views, so it's deliberately not set here.
    """

    def get(self, request):
        last_sync = get_last_sync_date()
        products  = list(Product.objects.filter(is_active=True).select_related('category', 'brand'))

        stock_by_product = {
            row['product']: row['total'] or 0
            for row in PurchaseBatch.objects.filter(
                product_id__in=[p.id for p in products],
                status='ACTIVE',
            ).values('product').annotate(total=Sum('remaining_quantity'))
        }

        search = request.query_params.get('search', '').strip().lower()
        status_filter = request.query_params.get('status', '').strip().upper()
        if status_filter and status_filter not in ('AVAILABLE', 'LOW', 'OUT'):
            return Response(
                {'error': "status must be one of: AVAILABLE, LOW, OUT."},
                status=status.HTTP_400_BAD_REQUEST
            )

        result = []

        for product in products:
            current_stock = stock_by_product.get(product.id, 0)

            reorder = product.reorder_threshold or 0
            if current_stock == 0:
                stock_status = 'OUT OF STOCK'
            elif current_stock <= reorder:
                stock_status = 'LOW STOCK'
            else:
                stock_status = 'AVAILABLE'

            if search and search not in product.product_name.lower():
                continue
            if status_filter == 'AVAILABLE' and stock_status != 'AVAILABLE':
                continue
            if status_filter == 'LOW' and stock_status != 'LOW STOCK':
                continue
            if status_filter == 'OUT' and stock_status != 'OUT OF STOCK':
                continue

            result.append({
                'product_id'       : product.id,
                'product_name'     : product.product_name,
                'category_name'    : product.category.category_name if product.category else '—',
                'brand_name'       : product.brand.brand_name if product.brand else 'UNBRANDED',
                'sku_code'         : product.sku_code,
                'current_stock'    : current_stock,
                'reorder_threshold': reorder,
                'stock_status'     : stock_status,
                'avg_cost_price'   : str(product.avg_cost_price),
                'last_sync_date'   : last_sync,
            })

        try:
            page = max(1, int(request.query_params.get('page', 1)))
            page_size = min(100, max(1, int(request.query_params.get('page_size', 25))))
        except (TypeError, ValueError):
            return Response({'error': 'page and page_size must be integers.'}, status=status.HTTP_400_BAD_REQUEST)

        count = len(result)
        total_pages = max(1, -(-count // page_size))  # ceiling division
        start = (page - 1) * page_size
        page_rows = result[start:start + page_size]

        return Response({
            'last_sync_date': last_sync,
            'note'          : 'Stock is snapshot-based.',
            'results'       : page_rows,
            'count'         : count,
            'page'          : page,
            'page_size'     : page_size,
            'total_pages'   : total_pages,
        })


class StockSummaryView(APIView):
    """
    GET /api/inventory/stock/summary/

    Total/Low/Out/Available counts for the inventory.html KPI cards.
    Paired with the pagination fix on StockSnapshotView above — those KPI
    cards must read from here, not from counting a paginated results list,
    or they'll silently show only the current page's counts instead of the
    real totals. Same fix pattern as HealthScoreSummaryView.

    Reuses the identical bulk-fetch (no N+1) that StockSnapshotView uses,
    just returns counts instead of per-product rows.
    """
    def get(self, request):
        products = list(Product.objects.filter(is_active=True))

        stock_by_product = {
            row['product']: row['total'] or 0
            for row in PurchaseBatch.objects.filter(
                product_id__in=[p.id for p in products],
                status='ACTIVE',
            ).values('product').annotate(total=Sum('remaining_quantity'))
        }

        total = low = out = available = 0
        for product in products:
            current_stock = stock_by_product.get(product.id, 0)
            reorder = product.reorder_threshold or 0
            total += 1
            if current_stock == 0:
                out += 1
            elif current_stock <= reorder:
                low += 1
            else:
                available += 1

        return Response({
            'total_products': total,
            'low_stock'     : low,
            'out_of_stock'  : out,
            'available'     : available,
        })


class InventoryProductOptionsView(APIView):
    """
    GET /api/inventory/products/picker/

    Lightweight {id, name} pairs for every active product — nothing else.
    Feeds inventory.html's <datalist id="inventoryProductOptions">, shared
    across the Stock Ledger and Manual Adjustment tabs' product-ID inputs.

    Deliberately separate from StockSnapshotView: the datalist doesn't need
    stock levels, categories, brands, or WAC — just enough to let someone
    type a product name and get its ID. No PurchaseBatch query at all here,
    so this stays cheap even at 2000+ products, and doesn't inherit
    StockSnapshotView's pagination (a <datalist> needs the full option set
    to be useful — paginating it would just move the problem, not solve it;
    this endpoint solves it by making the per-row payload small instead).
    """
    def get(self, request):
        products = Product.objects.filter(is_active=True).values('id', 'product_name').order_by('product_name')
        return Response({'products': list(products)})


STOCK_LEDGER_HISTORY_LIMIT = 100

TRANSACTION_TYPE_LABELS = {
    'PURCHASE': 'Purchase',
    'SALE_SYNC': 'Sale Sync',
    'MANUAL_ADJUSTMENT': 'Manual Adjustment',
    'INITIAL_IMPORT': 'Initial Import',
}

SOURCE_REASON_LABELS = {
    'DAMAGE_LOSS': 'Damage recorded',
    'EXPIRY_AUTO_DETECT': 'Expiry loss recorded',
    'MANUAL_ADJUSTMENT': 'Manual stock adjustment',
}


def _compute_stock_ledger_chronological(product):
    """
    Returns the FULL chronological (oldest -> newest) ledger history for a
    product, with running balance computed correctly in chronological order.
    This must always run in full before any pagination/reversal happens --
    computing running balance on a sliced/paginated subset would silently
    produce wrong balances for every page after the first.
    """
    entries = list(
        StockLedger.objects
        .filter(product=product)
        .order_by('transaction_date', 'id')
        .values('transaction_type', 'source', 'quantity_change',
                 'transaction_date', 'reference_id')
    )

    running_balance = 0
    chronological = []
    for e in entries:
        running_balance += e['quantity_change']
        chronological.append({
            'transaction_date': e['transaction_date'].isoformat(),
            'movement_type': TRANSACTION_TYPE_LABELS.get(
                e['transaction_type'], e['transaction_type']
            ),
            'source': e['source'] or '—',
            'reference_id': e['reference_id'],
            'quantity_change': e['quantity_change'],
            'balance_after': running_balance,
            'reason': SOURCE_REASON_LABELS.get(e['source'], '—'),
        })

    return chronological


def _build_stock_history(product, limit=STOCK_LEDGER_HISTORY_LIMIT):
    """
    Kept for ProductStockDetailView's inline preview (first page only,
    newest-first) so that view's existing response shape doesn't change.
    Full pagination lives in ProductStockHistoryView below.
    """
    chronological = _compute_stock_ledger_chronological(product)
    latest_balance = chronological[-1]['balance_after'] if chronological else 0
    display_order = list(reversed(chronological))[:limit]
    return display_order, latest_balance


class ProductStockHistoryView(APIView):
    """
    GET /api/inventory/stock/<product_id>/history/?page=1&page_size=10

    Paginated stock movement history, newest-first. Each call to "Next"
    from the frontend increments `page` to reveal older entries.

    Response:
        product_id     int
        page           int   — 1-indexed
        page_size      int
        total_count    int   — total ledger entries for this product
        total_pages    int
        has_next       bool
        has_previous   bool
        results        list  — same shape as ProductStockDetailView.stock_history rows
    """

    def get(self, request, product_id):
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            page = max(1, int(request.query_params.get('page', 1)))
        except (TypeError, ValueError):
            return Response({'error': 'page must be an integer'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            page_size = int(request.query_params.get('page_size', 10))
        except (TypeError, ValueError):
            return Response({'error': 'page_size must be an integer'},
                            status=status.HTTP_400_BAD_REQUEST)
        page_size = max(1, min(page_size, 100))  # sane ceiling

        chronological = _compute_stock_ledger_chronological(product)
        newest_first = list(reversed(chronological))

        total_count = len(newest_first)
        total_pages = max(1, -(-total_count // page_size))  # ceil div
        page = min(page, total_pages)

        start = (page - 1) * page_size
        end = start + page_size
        page_rows = newest_first[start:end]

        return Response({
            'product_id': product.id,
            'page': page,
            'page_size': page_size,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_next': end < total_count,
            'has_previous': page > 1,
            'results': page_rows,
        })

class ProductStockDetailView(APIView):
    def get(self, request, product_id):
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'},
                            status=status.HTTP_404_NOT_FOUND)

        batches     = PurchaseBatch.objects.filter(
            product=product, status='ACTIVE'
        ).order_by('expiry_date')
        total_stock = batches.aggregate(
            total=Sum('remaining_quantity'))['total'] or 0

        batch_data = [{
            'batch_id'          : b.id,
            'remaining_quantity': b.remaining_quantity,
            'quantity_received' : b.quantity_received,
            'cost_price'        : str(b.cost_price),
            'expiry_date'       : str(b.expiry_date) if b.expiry_date else None,
            'status'            : b.status,
        } for b in batches]

        reorder = product.reorder_threshold or 0
        if total_stock == 0:
            stock_status = 'OUT OF STOCK'
        elif total_stock <= reorder:
            stock_status = 'LOW STOCK'
        else:
            stock_status = 'AVAILABLE'

        stock_history, latest_ledger_balance = _build_stock_history(product)

        return Response({
            'product_id'         : product.id,
            'product_name'       : product.product_name,
            'category_name'      : product.category.category_name if product.category else '—',
            'brand_name'         : product.brand.brand_name if product.brand else 'UNBRANDED',
            'sku_code'           : product.sku_code,
            'avg_cost_price'     : str(product.avg_cost_price),
            'total_current_stock': total_stock,
            'reorder_threshold'  : reorder,
            'stock_status'       : stock_status,
            'last_sync_date'     : get_last_sync_date(),
            'active_batch_count' : len(batch_data),
            'batches'            : batch_data,
            'stock_history'      : stock_history,
            'latest_ledger_balance': latest_ledger_balance,
        })


class StockLedgerView(generics.ListAPIView):
    serializer_class = StockLedgerSerializer

    def get_queryset(self):
        queryset         = StockLedger.objects.all().order_by('-transaction_date')
        product          = self.request.query_params.get('product')
        transaction_type = self.request.query_params.get('type')
        if product:
            queryset = queryset.filter(product__id=product)
        if transaction_type:
            queryset = queryset.filter(transaction_type=transaction_type)
        return queryset


class StockAdjustmentView(APIView):
    def post(self, request):
        product_id      = request.data.get('product_id')
        quantity_change = request.data.get('quantity_change')
        reason          = request.data.get('reason', '')

        if not product_id or quantity_change is None:
            return Response(
                {'error': 'product_id and quantity_change are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'},
                            status=status.HTTP_404_NOT_FOUND)

        batch = PurchaseBatch.objects.filter(
            product=product, status__in=['ACTIVE', 'PENDING_EXPIRY']
        ).order_by('expiry_date').first()

        if batch is None:
            return Response(
                {'error': 'No active batch found. Create a purchase first.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        old_qty = batch.remaining_quantity
        new_qty = old_qty + int(quantity_change)
        if new_qty < 0:
            return Response(
                {'error': f'Adjustment would make stock negative. '
                          f'Current: {batch.remaining_quantity}, '
                          f'tried to remove: {abs(int(quantity_change))}.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        batch.remaining_quantity = new_qty
        if new_qty == 0:
            batch.status = 'DEPLETED'
        batch.save()

        StockLedger.objects.create(
            product=product, batch=batch,
            transaction_type='MANUAL_ADJUSTMENT',
            source='MANUAL_ADJUSTMENT',
            quantity_change=int(quantity_change),
        )

        adjustment = StockAdjustment.objects.create(
            product=product, batch=batch,
            quantity_change=int(quantity_change),
            reason=reason,
        )

        log_action(
            user=request.user,
            action='STOCK_ADJUSTMENT',
            table_name='purchase_batch',
            record_id=batch.id,
            old_value={'remaining_quantity': old_qty},
            new_value={'remaining_quantity': new_qty, 'reason': reason},
            request=request,
        )

        return Response({
            'message'               : 'Stock adjusted successfully',
            'product'               : product.product_name,
            'quantity_change'       : quantity_change,
            'new_remaining_quantity': new_qty,
            'batch_id'              : batch.id,
            'batch_status'          : batch.status,
            'adjustment_id'         : adjustment.id
        }, status=status.HTTP_201_CREATED)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def expiry_summary(request):
    """
    GET /api/reports/expiry-summary/
 
    Counts of ACTIVE batches (with remaining stock) expiring in
    each urgency window, plus a detailed list per window.
 
    Response:
        as_of                       str  — today's date
        expiring_in_7_days          int  — within 0-7 days
        expiring_in_7_to_14_days    int  — between 8-14 days
        expiring_in_14_to_30_days   int  — between 15-30 days
        total_expiring_in_30_days   int  — sum of all three
        batches_7_days              list — detailed batch list (7-day window)
        batches_7_to_14_days        list — detailed batch list (7-14 day window)
        batches_14_to_30_days       list — detailed batch list (14-30 day window)
 
    Auth: Staff JWT required
    """
 
    today = datetime.now(LOCAL_TZ).date()
    d7    = today + timedelta(days=7)
    d14   = today + timedelta(days=14)
    d30   = today + timedelta(days=30)
 
    # Base queryset: only ACTIVE batches with stock and an expiry date
    active_with_expiry = PurchaseBatch.objects.filter(
        status='ACTIVE',
        remaining_quantity__gt=0,
        expiry_date__isnull=False,
    ).select_related('product')
 
    # ── Counts ─────────────────────────────────────────────────────────────────
    batches_7    = active_with_expiry.filter(expiry_date__lte=d7)
    batches_7_14 = active_with_expiry.filter(expiry_date__gt=d7,  expiry_date__lte=d14)
    batches_14_30 = active_with_expiry.filter(expiry_date__gt=d14, expiry_date__lte=d30)
 
    count_7     = batches_7.count()
    count_7_14  = batches_7_14.count()
    count_14_30 = batches_14_30.count()
 
    def _serialize_batch(batch):
        """Return the detail dict for one batch."""
        days_left = (batch.expiry_date - today).days
        est_loss  = round(
            float(batch.remaining_quantity) * float(batch.cost_price or 0), 2
        )
        return {
            'batch_id':          batch.id,
            'product_id':        batch.product.id,
            'product_name':      batch.product.product_name,
            'sku_code':          batch.product.sku_code or '',
            'expiry_date':       str(batch.expiry_date),
            'days_until_expiry': days_left,
            'remaining_quantity': batch.remaining_quantity,
            'cost_price':        float(batch.cost_price or 0),
            'estimated_loss':    est_loss,  # remaining_qty x cost_price
        }
 
    return Response({
        'as_of':                    str(today),
        'expiring_in_7_days':       count_7,
        'expiring_in_7_to_14_days': count_7_14,
        'expiring_in_14_to_30_days': count_14_30,
        'total_expiring_in_30_days': count_7 + count_7_14 + count_14_30,
        'batches_7_days':       [_serialize_batch(b) for b in batches_7.order_by('expiry_date')],
        'batches_7_to_14_days': [_serialize_batch(b) for b in batches_7_14.order_by('expiry_date')],
        'batches_14_to_30_days': [_serialize_batch(b) for b in batches_14_30.order_by('expiry_date')],
    })

SALES_LOOKBACK_DAYS = 30
 
 
class LowStockView(APIView):
    """
    GET /api/inventory/low-stock/
 
    Returns products where current_stock < reorder_threshold,
    sorted by urgency (CRITICAL first).
 
    Response per product:
        product_id          int
        product_name        str
        sku_code             str
        current_stock        int     — SUM of remaining_quantity from ACTIVE + PENDING_EXPIRY batches
        reorder_threshold    int     — Product.reorder_threshold
        shortage              int     — how many units below reorder_threshold
        urgency               str     — CRITICAL | HIGH | MEDIUM | LOW
        days_of_stock         float|None — None if no sales data (avg_daily == 0)
        avg_daily_sales       float
    """
 
    def get(self, request):
        today = datetime.now(LOCAL_TZ).date()
        since = today - timedelta(days=SALES_LOOKBACK_DAYS)
 
        products = Product.objects.filter(is_active=True)
 
        # Bulk-fetch all active batch stock in one query (avoid N+1)
        stock_by_product = {
            row['product']: row['total']
            for row in PurchaseBatch.objects.filter(
                status__in=['ACTIVE', 'PENDING_EXPIRY'],
                remaining_quantity__gt=0,
            ).values('product').annotate(total=Sum('remaining_quantity'))
        }
 
        # Bulk-fetch 30-day sales per product (avoid N+1)
        sales_by_product = {
            row['product']: row['total']
            for row in ItemSalesRecord.objects.filter(
                sale_date__gte=since,
                sale_date__lte=today,
            ).values('product').annotate(total=Sum('quantity_sold'))
        }
 
        low_stock = []
 
        for product in products:
            reorder_threshold = product.reorder_threshold or 0
            if reorder_threshold == 0:
                continue  # skip products with no reorder point set

            current = stock_by_product.get(product.id, 0)
            if current >= reorder_threshold:
                continue  # only include products strictly below threshold
 
            shortage = reorder_threshold - current
 
            # ── Urgency calculation ──────────────────────────────────────
            total_sold = sales_by_product.get(product.id, 0)
            avg_daily  = Decimal(str(total_sold)) / Decimal(str(SALES_LOOKBACK_DAYS))
 
            if avg_daily > 0:
                days_of_stock = round(float(current) / float(avg_daily), 1)
                urgency       = get_urgency(days_of_stock)
            else:
                # No recent sales — can't compute days_of_stock meaningfully,
                # but product is still below threshold, so flag as LOW
                days_of_stock = None
                urgency       = 'LOW'
 
            low_stock.append({
                'product_id'       : product.id,
                'product_name'     : product.product_name,
                'sku_code'         : product.sku_code,
                'current_stock'    : current,
                'reorder_threshold': reorder_threshold,
                'shortage'         : shortage,
                'urgency'          : urgency,
                'days_of_stock'    : days_of_stock,
                'avg_daily_sales'  : round(float(avg_daily), 2),
            })
 
        # Sort: CRITICAL first, then HIGH, MEDIUM, LOW
        _PRIORITY = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}
        low_stock.sort(key=lambda r: _PRIORITY.get(r['urgency'], 4))
 
        return Response({
            'count'             : len(low_stock),
            'low_stock_products': low_stock
        })
 


class OutOfStockView(APIView):
    """
    GET /api/inventory/out-of-stock/

    FIX (performance): same N+1 bug as StockSnapshotView above — one
    PurchaseBatch aggregate query per product inside the loop. Same fix
    applied: bulk-fetch stock for every product in one query first.
    """
    def get(self, request):
        products = list(Product.objects.filter(is_active=True))

        stock_by_product = {
            row['product']: row['total'] or 0
            for row in PurchaseBatch.objects.filter(
                product_id__in=[p.id for p in products],
                status='ACTIVE',
            ).values('product').annotate(total=Sum('remaining_quantity'))
        }

        out = []
        for product in products:
            current = stock_by_product.get(product.id, 0)
            if current == 0:
                out.append({
                    'product_id'  : product.id,
                    'product_name': product.product_name,
                    'sku_code'    : product.sku_code,
                })

        return Response({'count': len(out), 'out_of_stock': out})


# ═════════════════════════════════════════════════════════════════
# F06 — Product Lifecycle Monitoring
# ═════════════════════════════════════════════════════════════════

class LifecycleCalculateView(APIView):
    permission_classes = [IsManagerOrAdmin]

    def post(self, request):
        from inventory.services.lifecycle import run_lifecycle_calculation
        result = run_lifecycle_calculation()

        log_action(
            user=request.user, action='CALCULATE', table_name='product_lifecycle',
            record_id=None, old_value=None,
            new_value={
                'products_processed': len(result['products']),
                'summary': result['summary'],
            },
            request=request,
        )

        return Response({
            'message'           : 'Lifecycle calculation complete',
            'products_processed': len(result['products']),
            'summary'           : result['summary'],
        }, status=status.HTTP_200_OK)

class LifecycleListView(APIView):
    def get(self, request):
        queryset      = ProductLifecycle.objects.all().order_by('-calculated_date')
        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        data = queryset.values(
            'id', 'product', 'status', 'recommendation',
            'sales_velocity', 'calculated_date'
        )
        return Response(list(data))


class LifecycleDecliningView(APIView):
    def get(self, request):
        queryset = ProductLifecycle.objects.filter(
            status='DECLINING'
        ).order_by('-calculated_date')
        data = queryset.values(
            'id', 'product', 'status', 'recommendation',
            'sales_velocity', 'calculated_date'
        )
        return Response(list(data))


class LifecycleProductHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, product_id):
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'},
                            status=status.HTTP_404_NOT_FOUND)

        date_from = request.query_params.get('date_from') or '2026-01-01'
        date_to = request.query_params.get('date_to') or str(datetime.now(LOCAL_TZ).date())
        try:
            period_start = date.fromisoformat(date_from)
            period_end = date.fromisoformat(date_to)
        except ValueError:
            return Response({'error': 'date_from and date_to must use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)
        if period_start > period_end:
            return Response({'error': 'date_from must not be after date_to.'}, status=status.HTTP_400_BAD_REQUEST)

        queryset = ProductLifecycle.objects.filter(
            product=product
        ).order_by('calculated_date', 'id')
        queryset = queryset.filter(
            calculated_date__gte=period_start,
            calculated_date__lte=period_end,
        )
        data = queryset.values(
            'id', 'product', 'status', 'recommendation',
            'sales_velocity', 'comparison_period', 'calculated_date'
        )
        history = list(data)

        # Lifecycle calculations are usually monthly, so plotting only those
        # rows leaves a product with current sales as a single point (or no
        # graph before its first calculation).  The graph uses the actual
        # day-by-day item-sales records for the requested fixed date range.
        daily_sales = {
            row['sale_date']: float(row['total'] or 0)
            for row in (
                ItemSalesRecord.objects.filter(
                    product=product,
                    sale_date__range=(period_start, period_end),
                )
                .values('sale_date')
                .annotate(total=Sum('quantity_sold'))
            )
        }
        sales_series = []
        cursor = period_start
        while cursor <= period_end:
            sales_series.append({
                'date': str(cursor),
                'sales_velocity': daily_sales.get(cursor, 0),
            })
            cursor += timedelta(days=1)

        return Response({
            'history': history,
            'sales_series': sales_series,
            'date_from': str(period_start),
            'date_to': str(period_end),
        })


# ═════════════════════════════════════════════════════════════════
# F07 — Loss & Supplier Returns
# ═════════════════════════════════════════════════════════════════

class LossRecordView(APIView):

    def get_permissions(self):
        if self.request.method == 'POST':
            return [IsAuthenticated()]
        return [IsManagerOrAdmin()]

    def get(self, request):
        queryset  = LossRecord.objects.all().order_by('-loss_date')
        loss_type = request.query_params.get('loss_type')
        product   = request.query_params.get('product')
        date_from = request.query_params.get('date_from')
        date_to   = request.query_params.get('date_to')

        if loss_type:
            queryset = queryset.filter(loss_type=loss_type)
        if product:
            queryset = queryset.filter(product__id=product)
        if date_from:
            queryset = queryset.filter(loss_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(loss_date__lte=date_to)

        data = queryset.values(
            'id', 'product', 'product__product_name', 'batch', 'loss_type',
            'loss_quantity', 'loss_value', 'loss_date', 'notes'
        )
        data = [
            {**row, 'product_name': row.pop('product__product_name')}
            for row in data
        ]
        return Response(list(data))

    def post(self, request):
        product_id    = request.data.get('product_id')
        loss_type     = request.data.get('loss_type')
        loss_quantity = request.data.get('loss_quantity')
        notes         = request.data.get('notes', '')

        if not product_id or not loss_type or loss_quantity is None:
            return Response(
                {'error': 'product_id, loss_type and loss_quantity are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if loss_type not in ['EXPIRY', 'SLOW_MOVING', 'DAMAGE', 'OTHER']:
            return Response(
                {'error': 'loss_type must be EXPIRY / SLOW_MOVING / DAMAGE / OTHER'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if (
            loss_type not in ['DAMAGE', 'EXPIRY']
            and not (
                request.user.is_superuser
                or request.user.groups.filter(name__in=['ADMIN', 'MANAGER']).exists()
            )
        ):
            return Response(
                {'error': 'Staff may record damage or verified expiry only'},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            quantity = int(loss_quantity)
        except (TypeError, ValueError):
            return Response(
                {'error': 'loss_quantity must be a whole number'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if quantity <= 0:
            return Response(
                {'error': 'loss_quantity must be greater than zero'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(str(notes)) > 255:
            return Response(
                {'error': 'notes must be 255 characters or fewer'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'},
                            status=status.HTTP_404_NOT_FOUND)

        loss_value = quantity * (product.avg_cost_price or 0)

        record = LossRecord.objects.create(
            product       = product,
            loss_type     = loss_type,
            loss_quantity = quantity,
            loss_value    = loss_value,
            loss_date     = datetime.now(LOCAL_TZ).date(),
            recorded_by   = request.user,
            notes         = notes,
        )

        # FIX (stock audit, 2026-09): damage losses were previously
        # recorded in LossRecord but never actually reduced stock --
        # PurchaseBatch.remaining_quantity stayed untouched, so a
        # product could show plenty of "available" stock while a chunk
        # of it had already been recorded as damaged and thrown away.
        # Follows the SAME convention EXPIRY_AUTO_DETECT already uses
        # (MANUAL_ADJUSTMENT transaction_type, distinguished by source)
        # rather than inventing a new transaction_type. Manually-
        # recorded EXPIRY through this same endpoint has an identical
        # gap -- intentionally NOT changed here since it wasn't asked
        # for; flagging it as a known follow-up alongside this fix.
        stock_deduction_shortfall = None
        if loss_type == 'DAMAGE':
            fefo_result = deduct_stock_fefo(
                product_id=product.id,
                quantity=quantity,
                source='DAMAGE_LOSS',
                reference_id=record.id,
                transaction_type='MANUAL_ADJUSTMENT',
            )
            if fefo_result['shortfall'] > 0:
                stock_deduction_shortfall = fefo_result['shortfall']

        log_action(
            user=request.user, action='CREATE', table_name='loss_record',
            record_id=record.id, old_value=None,
            new_value={
                'product': product.product_name,
                'loss_type': loss_type,
                'loss_quantity': quantity,
                'loss_value': str(loss_value),
            },
            request=request,
        )

        response_data = {
            'message'      : 'Loss recorded successfully',
            'loss_id'      : record.id,
            'product'      : product.product_name,
            'loss_type'    : loss_type,
            'loss_quantity': quantity,
            'loss_value'   : str(loss_value),
        }
        if stock_deduction_shortfall is not None:
            response_data['warning'] = (
                f'Loss recorded, but only {quantity - stock_deduction_shortfall} '
                f'of {quantity} units could be deducted from sellable stock '
                f'(shortfall {stock_deduction_shortfall}). This product\'s '
                f'stock may already be understated -- worth a manual check.'
            )

        return Response(response_data, status=status.HTTP_201_CREATED)


class LossSummaryView(APIView):

    permission_classes = [IsManagerOrAdmin]

    def get(self, request):
        from sales.models import DailyBillSummary

        date_from = request.query_params.get('date_from')
        date_to   = request.query_params.get('date_to')

        loss_qs = LossRecord.objects.all()
        bill_qs = DailyBillSummary.objects.all()
        ret_qs  = SupplierReturn.objects.filter(status='CONFIRMED')

        if date_from:
            loss_qs = loss_qs.filter(loss_date__gte=date_from)
            bill_qs = bill_qs.filter(sale_date__gte=date_from)
            ret_qs  = ret_qs.filter(return_date__gte=date_from)
        if date_to:
            loss_qs = loss_qs.filter(loss_date__lte=date_to)
            bill_qs = bill_qs.filter(sale_date__lte=date_to)
            ret_qs  = ret_qs.filter(return_date__lte=date_to)

        gross_expiry  = loss_qs.filter(loss_type='EXPIRY').aggregate(
            t=Sum('loss_value'))['t'] or 0
        damage_loss   = loss_qs.filter(loss_type='DAMAGE').aggregate(
            t=Sum('loss_value'))['t'] or 0
        recovered     = ret_qs.aggregate(t=Sum('return_value'))['t'] or 0
        discount_loss = bill_qs.aggregate(t=Sum('discount'))['t'] or 0

        net_expiry = gross_expiry - recovered
        total_loss = float(net_expiry) + float(damage_loss) + float(discount_loss)

        return Response({
            'gross_expiry_loss': str(gross_expiry),
            'recovered_amount' : str(recovered),
            'net_expiry_loss'  : str(net_expiry),
            'discount_loss'    : str(discount_loss),
            'damage_loss'      : str(damage_loss),
            'total_net_loss'   : str(total_loss),
        })


class LossAutoDetectView(APIView):

    permission_classes = [IsManagerOrAdmin]

    def post(self, request):
        today   = datetime.now(LOCAL_TZ).date()
        expired = list(PurchaseBatch.objects.filter(
            status='ACTIVE',
            expiry_date__lt=today,
            remaining_quantity__gt=0
        ).select_related('product'))
        existing_batch_ids = set(
            LossRecord.objects.filter(
                batch_id__in=[batch.id for batch in expired],
                loss_type='EXPIRY',
            ).values_list('batch_id', flat=True)
        )
        created = 0

        for batch in expired:
            if batch.id in existing_batch_ids:
                continue

            LossRecord.objects.create(
                product       = batch.product,
                batch         = batch,
                loss_type     = 'EXPIRY',
                loss_quantity = batch.remaining_quantity,
                loss_value    = batch.remaining_quantity * batch.cost_price,
                loss_date     = today,
                notes         = f'Auto-detected expiry: batch {batch.id}',
            )

            StockLedger.objects.create(
                product          = batch.product,
                batch            = batch,
                transaction_type = 'MANUAL_ADJUSTMENT',
                source           = 'EXPIRY_AUTO_DETECT',
                quantity_change  = -batch.remaining_quantity,
            )

            # FIX (stock audit, 2026-09): this previously only flipped
            # status to EXPIRED without zeroing remaining_quantity. The
            # ledger entry above correctly nets the batch's contribution
            # to zero in any ledger-based total, and status filtering
            # correctly excludes it from batch-based totals -- so the
            # aggregate "current stock" number was coincidentally still
            # right either way. But the batch record itself was left
            # showing a stale, wrong remaining_quantity, which is a real
            # data-integrity problem for anything that inspects batches
            # directly (e.g. an audit report, or code written later that
            # assumes remaining_quantity=0 means "actually depleted").
            batch.status = 'EXPIRED'
            batch.remaining_quantity = 0
            batch.save()
            created += 1

        log_action(
            user=request.user, action='CALCULATE', table_name='loss_record',
            record_id=None, old_value=None,
            new_value={'batches_expired': created},
            request=request,
        )

        return Response({
            'message'        : 'Expiry auto-detection complete',
            'batches_expired': created,
        }, status=status.HTTP_200_OK)


class SupplierReturnView(APIView):

    permission_classes = [IsManagerOrAdmin]

    def get(self, request):
        queryset    = SupplierReturn.objects.all().order_by('-return_date')
        supplier_id = request.query_params.get('supplier')
        ret_status  = request.query_params.get('status')

        if supplier_id:
            queryset = queryset.filter(supplier__id=supplier_id)
        if ret_status:
            queryset = queryset.filter(status=ret_status)

        data = queryset.values(
            'id', 'supplier', 'product', 'product__product_name', 'batch',
            'return_bill_no', 'return_date', 'quantity_returned', 'return_value',
            'return_reason', 'recovery_type', 'status', 'notes'
        )
        return Response([
            {**row, 'product_name': row.pop('product__product_name')}
            for row in data
        ])

    def post(self, request):
        supplier_id       = request.data.get('supplier_id')
        batch_id          = request.data.get('batch_id')
        product_id        = request.data.get('product_id')
        quantity_returned = request.data.get('quantity_returned')
        return_reason     = request.data.get('return_reason', '')
        recovery_type     = request.data.get('recovery_type', '')
        notes             = request.data.get('notes', '')

        if not all([supplier_id, batch_id, product_id, quantity_returned]):
            return Response(
                {'error': 'supplier_id, batch_id, product_id and '
                          'quantity_returned are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            supplier = Supplier.objects.get(pk=supplier_id)
        except Supplier.DoesNotExist:
            return Response({'error': 'Supplier not found'},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            batch = PurchaseBatch.objects.get(pk=batch_id)
        except PurchaseBatch.DoesNotExist:
            return Response({'error': 'Batch not found'},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'},
                            status=status.HTTP_404_NOT_FOUND)

        return_value = int(quantity_returned) * batch.cost_price

        ret = SupplierReturn.objects.create(
            supplier          = supplier,
            batch             = batch,
            product           = product,
            return_date       = datetime.now(LOCAL_TZ).date(),
            quantity_returned = int(quantity_returned),
            return_value      = return_value,
            return_reason     = return_reason,
            recovery_type     = recovery_type,
            status            = 'PENDING',
            notes             = notes,
        )

        log_action(
            user=request.user, action='CREATE', table_name='supplier_return',
            record_id=ret.id, old_value=None,
            new_value={
                'supplier': supplier.supplier_name if hasattr(supplier, 'supplier_name') else supplier.id,
                'product': product.product_name,
                'quantity_returned': int(quantity_returned),
                'return_value': str(return_value),
                'status': 'PENDING',
            },
            request=request,
        )

        return Response({
            'message'          : 'Supplier return recorded',
            'return_id'        : ret.id,
            'supplier'         : supplier.id,
            'product'          : product.product_name,
            'quantity_returned': int(quantity_returned),
            'return_value'     : str(return_value),
            'status'           : 'PENDING',
        }, status=status.HTTP_201_CREATED)


_RETURN_ANCHOR_STORE = 'Samanala Super Mart'
_RETURN_ANCHOR_DOC_TYPE = 'SUPPLY RETURN'
_RETURN_DATE_FORMATS = (
    '%d-%b-%Y', '%d-%B-%Y', '%d/%m/%Y', '%d-%m-%Y',
    '%Y-%m-%d', '%d.%m.%Y',
)
_RETURN_HEADER_LABELS = {
    'bill_no': re.compile(r'^(?:return\s+)?bill\s*(?:no\.?|number)?\s*[:#\-]?\s*$', re.I),
    'return_date': re.compile(r'^(?:return\s+)?date\s*[:#\-]?\s*$', re.I),
    # easyAcc uses "Customer" for the supplier on a supply-return document.
    'supplier_name': re.compile(r'^(?:customer|supplier|vendor)\s*[:#\-]?\s*$', re.I),
    'invoice_number': re.compile(r'^(?:inv(?:oice)?\s*(?:no\.?|number)?|original\s+invoice)\s*[:#\-]?\s*$', re.I),
}
_RETURN_HEADER_INLINE = {
    key: re.compile(pattern, re.I)
    for key, pattern in {
        # A hyphen is intentionally not an inline separator: invoice IDs
        # such as "INV-452" must not be parsed as the label "INV".
        'bill_no': r'(?:return\s+)?bill\s*(?:no\.?|number)?\s*[:#]\s*(.+)',
        'return_date': r'(?:return\s+)?date\s*[:#]\s*(.+)',
        'supplier_name': r'(?:customer|supplier|vendor)\s*[:#]\s*(.+)',
        'invoice_number': r'(?:inv(?:oice)?\s*(?:no\.?|number)?|original\s+invoice)\s*[:#]\s*(.+)',
    }.items()
}
_RETURN_ITEM_LINE_PATTERN = re.compile(
    r'^(\d{3,6})\s+(.+?)\s+'
    r'([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+'
    r'([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})$'
)
_RETURN_SKIP_KEYWORDS = ('Net Total', 'Item Description', 'No Qty', 'Page')


def _parse_return_header(lines):
    """Extract return metadata from text generated by different PDF layouts.

    pdfplumber preserves visual columns differently depending on the printer
    driver.  Therefore this deliberately uses the field labels instead of a
    fixed line position relative to the shop title.
    """
    normalized = [re.sub(r'\s+', ' ', line).strip() for line in lines if line and line.strip()]
    fields = {}

    def value_after_label(index):
        """Get a value from a label-only line, allowing one intervening label."""
        for candidate in normalized[index + 1:index + 3]:
            if any(pattern.match(candidate) for pattern in _RETURN_HEADER_LABELS.values()):
                continue
            if candidate and not re.search(r'^(?:page\s+\d+|samanala super mart|supply return)$', candidate, re.I):
                return candidate
        return None

    for index, line in enumerate(normalized):
        for key, label_pattern in _RETURN_HEADER_LABELS.items():
            if key in fields:
                continue
            if label_pattern.match(line):
                fields[key] = value_after_label(index)
                continue
            inline = _RETURN_HEADER_INLINE[key].search(line)
            if inline:
                # A line can contain several column labels.  Do not mistake
                # the following label (for example "Date:") for a value.
                value = inline.group(1).strip()
                value = re.split(r'\s+(?=(?:return\s+)?(?:bill|date)|(?:customer|supplier|vendor)|(?:inv(?:oice)?|original\s+invoice)\b)', value, maxsplit=1, flags=re.I)[0].strip(' :')
                value_is_another_label = re.match(
                    r'^(?:(?:return\s+)?(?:bill|date)\s*(?:no\.?|number)?|(?:customer|supplier|vendor)|(?:inv(?:oice)?\s+(?:no\.?|number)|original\s+invoice))\s*[:#]',
                    value,
                    re.I,
                )
                if value and not value_is_another_label and not any(pattern.match(value) for pattern in _RETURN_HEADER_LABELS.values()):
                    fields[key] = value

    # Older layouts extract the four header values as separate lines directly
    # before the shop/document title.  Retain that format as a last fallback.
    anchor_index = next((i for i, line in enumerate(normalized)
                         if line.casefold() == _RETURN_ANCHOR_STORE.casefold()), None)
    if anchor_index is not None:
        candidates = [line for line in normalized[max(0, anchor_index - 8):anchor_index]
                      if not line.lower().startswith('page ') and
                      not any(pattern.match(line) for pattern in _RETURN_HEADER_LABELS.values())]
        if len(candidates) >= 4:
            for key, value in zip(('bill_no', 'return_date', 'supplier_name', 'invoice_number'), candidates[-4:]):
                if not fields.get(key):
                    fields[key] = value

    return_date = None
    date_value = fields.get('return_date')
    if date_value:
        # Keep only the date portion when an export appends a time.
        date_value = date_value.split()[0].strip(' ,')
        for date_format in _RETURN_DATE_FORMATS:
            try:
                return_date = datetime.strptime(date_value, date_format).date()
                break
            except ValueError:
                continue

    return (
        fields.get('bill_no'),
        return_date,
        fields.get('supplier_name'),
        fields.get('invoice_number'),
    )


def _parse_return_items(lines, start_index):
    items = []
    index = start_index
    while index < len(lines):
        line = lines[index].strip()
        if not line or any(keyword in line for keyword in _RETURN_SKIP_KEYWORDS):
            index += 1
            continue
        match = _RETURN_ITEM_LINE_PATTERN.match(line)
        if not match and re.match(r'^\d{3,6}\s', line) and index + 1 < len(lines):
            match = _RETURN_ITEM_LINE_PATTERN.match(line + ' ' + lines[index + 1].strip())
            if match:
                index += 1
        if match:
            item_code, description, qty, cost_unit, _cost_total, _sell_qty, sell_unit, _sell_total = match.groups()
            items.append({
                'item_code': item_code,
                'description': description.strip(),
                'qty': Decimal(qty.replace(',', '')),
                'cost_unit': Decimal(cost_unit.replace(',', '')),
                'sell_unit': Decimal(sell_unit.replace(',', '')),
            })
        index += 1
    return items


def _get_or_create_return_product(item, return_date):
    """Find a product from a return line or register a new catalogue item."""
    item_code = item['item_code'].strip()
    description = item['description'].strip()

    # The item code is the stable identifier in the PDF.  A name match is a
    # fallback for older catalogue entries that pre-date SKU imports.
    sku_matches = Product.objects.filter(sku_code__iexact=item_code)
    if sku_matches.count() == 1:
        return sku_matches.first(), False, None
    if sku_matches.count() > 1:
        return None, False, 'More than one product has this item code; product was not changed.'

    name_matches = Product.objects.filter(product_name__iexact=description)
    if name_matches.count() == 1:
        return name_matches.first(), False, None
    if name_matches.count() > 1:
        return None, False, 'More than one product has this description; product was not changed.'

    product = Product.objects.create(
        product_name=description[:150],
        sku_code=item_code[:50],
        cost_price=item['cost_unit'],
        avg_cost_price=item['cost_unit'],
        unit_price=item['sell_unit'],
        introduced_date=return_date,
        is_active=True,
    )
    return product, True, None


def _get_or_create_return_supplier(supplier_name):
    """Find a supplier case-insensitively or register the PDF supplier."""
    supplier = Supplier.objects.filter(supplier_name__iexact=supplier_name).first()
    if supplier:
        return supplier, False
    return Supplier.objects.create(supplier_name=supplier_name[:150]), True


def _create_return_reconciliation_batch(purchase, product, quantity, cost_price):
    """Create stock solely to reconcile a documented return back to zero."""
    return PurchaseBatch.objects.create(
        purchase=purchase,
        product=product,
        quantity_received=quantity,
        remaining_quantity=quantity,
        cost_price=cost_price,
        status='ACTIVE',
    )


class SupplierReturnUploadView(APIView):
    """Upload the Samanala Super Mart SUPPLY RETURN PDF format."""

    permission_classes = [IsManagerOrAdmin]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response({'error': 'No return PDF uploaded.'}, status=400)
        if not uploaded_file.name.lower().endswith('.pdf'):
            return Response({'error': 'Supplier return file must be a PDF.'}, status=400)

        upload_log = UploadLog.objects.create(
            file_name=uploaded_file.name,
            upload_type='SUPPLIER_RETURN',
            status='PARTIAL',
            error_message='',
            uploaded_by=request.user.id,
        )
        try:
            import pdfplumber
            lines = []
            with pdfplumber.open(io.BytesIO(uploaded_file.read())) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ''
                    lines.extend(line.strip() for line in text.split('\n') if line.strip())

            bill_no, return_date, supplier_name, invoice_number = _parse_return_header(lines)
            if not bill_no or not supplier_name or not return_date:
                raise ValueError('Could not extract a valid return bill number, date, and supplier from the PDF header.')
            if return_date > datetime.now(LOCAL_TZ).date():
                raise ValueError('Return date cannot be in the future.')

            start_index = next((i + 1 for i, line in enumerate(lines) if line.startswith('Item Description') or line.startswith('No Unit')), 0)
            items = _parse_return_items(lines, start_index)
            if not items:
                raise ValueError('No return item lines could be parsed from the PDF.')

            processed = []
            flagged = []
            with transaction.atomic():
                supplier, supplier_created = _get_or_create_return_supplier(supplier_name)

                if SupplierReturn.objects.filter(supplier=supplier, return_bill_no=bill_no).exists():
                    return Response({'error': f'Return bill {bill_no} has already been processed.'}, status=409)

                created_products = []
                reconciliation_purchase = None
                reconciliation_batches = []
                for item in items:
                    product, product_created, product_error = _get_or_create_return_product(item, return_date)
                    if product_error:
                        flagged.append({'description': item['description'], 'reason': product_error})
                        continue
                    if product_created:
                        created_products.append({
                            'product_id': product.id,
                            'sku_code': product.sku_code,
                            'product': product.product_name,
                        })
                    batch = PurchaseBatch.objects.filter(
                        product=product, status='ACTIVE', remaining_quantity__gt=0
                    ).order_by('-id').first()
                    quantity = int(item['qty'])
                    if quantity <= 0:
                        flagged.append({'description': item['description'], 'reason': 'Return quantity must be greater than zero.'})
                        continue

                    # A return document can be the first record received for
                    # an item.  Create a same-quantity reconciliation batch,
                    # then deduct it immediately below: this preserves the
                    # return/audit history without adding stock on hand.
                    if not batch:
                        if reconciliation_purchase is None:
                            reconciliation_purchase = Purchase.objects.create(
                                supplier=supplier,
                                purchase_date=return_date,
                                invoice_number=(invoice_number or f'RETURN-{bill_no}')[:50],
                                total_amount=Decimal('0'),
                            )
                        batch = _create_return_reconciliation_batch(
                            reconciliation_purchase, product, quantity, item['cost_unit']
                        )
                        reconciliation_batches.append(batch.id)
                    elif quantity > batch.remaining_quantity:
                        flagged.append({'description': item['description'], 'reason': 'Return quantity exceeds available batch stock.'})
                        continue

                    deduction = deduct_stock_fefo(
                        product_id=product.id,
                        quantity=quantity,
                        source='SUPPLIER_RETURN_PDF',
                        transaction_type='SUPPLIER_RETURN',
                        batch_id=batch.id,
                    )
                    if deduction['shortfall']:
                        flagged.append({'description': item['description'], 'reason': 'Stock deduction shortfall; stock was not accepted.'})
                        continue

                    touched_batch = PurchaseBatch.objects.get(pk=batch.id)
                    ret = SupplierReturn.objects.create(
                        supplier=supplier,
                        batch=touched_batch,
                        product=product,
                        return_bill_no=bill_no,
                        return_date=return_date,
                        quantity_returned=quantity,
                        return_value=item['cost_unit'] * quantity,
                        status='CONFIRMED',
                        notes=f'Imported from {uploaded_file.name}' + (f' / invoice {invoice_number}' if invoice_number else ''),
                        recorded_by=request.user,
                    )
                    processed.append({'return_id': ret.id, 'product': product.product_name, 'batch_id': batch.id, 'quantity_returned': quantity})

            upload_log.status = 'SUCCESS' if not flagged else 'PARTIAL'
            upload_log.error_message = '\n'.join(f"{item['description']}: {item['reason']}" for item in flagged)[:2000]
            upload_log.save(update_fields=['status', 'error_message'])
            return Response({
                'message': 'Supplier return PDF upload complete',
                'upload_log_id': upload_log.id,
                'return_bill_no': bill_no,
                'supplier': supplier.supplier_name,
                'supplier_created': supplier_created,
                'return_date': str(return_date),
                'processed_count': len(processed),
                'flagged_count': len(flagged),
                'created_products': created_products,
                'reconciliation_batch_count': len(reconciliation_batches),
                'processed': processed,
                'flagged': flagged,
            }, status=201)
        except Exception as exc:
            upload_log.status = 'FAILED'
            upload_log.error_message = str(exc)[:2000]
            upload_log.save(update_fields=['status', 'error_message'])
            return Response({'error': str(exc)}, status=400)


class SupplierReturnStatusView(APIView):

    permission_classes = [IsManagerOrAdmin]

    def patch(self, request, pk):
        new_status = request.data.get('status')

        if new_status not in ['CONFIRMED', 'REJECTED']:
            return Response(
                {'error': 'status must be CONFIRMED or REJECTED'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            ret = SupplierReturn.objects.get(pk=pk)
        except SupplierReturn.DoesNotExist:
            return Response({'error': 'Supplier return not found'},
                            status=status.HTTP_404_NOT_FOUND)

        old_value = {'status': ret.status}
        ret.status = new_status
        ret.save()

        log_action(
            user=request.user, action='UPDATE', table_name='supplier_return',
            record_id=ret.id, old_value=old_value,
            new_value={'status': ret.status},
            request=request,
        )

        return Response({
            'message'  : f'Return status updated to {new_status}',
            'return_id': ret.id,
            'status'   : ret.status,
        })


class SupplierReturnSummaryView(APIView):

    def get(self, request):
        suppliers = Supplier.objects.all()
        result    = []

        for supplier in suppliers:
            all_returns       = SupplierReturn.objects.filter(supplier=supplier)
            confirmed_returns = all_returns.filter(status='CONFIRMED')
            rejected_returns  = all_returns.filter(status='REJECTED')

            total_returned  = all_returns.aggregate(
                t=Sum('quantity_returned'))['t'] or 0
            recovery_value  = confirmed_returns.aggregate(
                t=Sum('return_value'))['t'] or 0
            total_confirmed = confirmed_returns.count()
            total_rejected  = rejected_returns.count()

            if all_returns.count() > 0:
                result.append({
                    'supplier_id'       : supplier.id,
                    'total_returns'     : all_returns.count(),
                    'total_confirmed'   : total_confirmed,
                    'total_rejected'    : total_rejected,
                    'total_qty_returned': total_returned,
                    'recovery_value'    : str(recovery_value),
                })

        return Response(result)
    

# ─────────────────────────────────────────────────────────────────
# POST /api/health-scores/calculate/
# ─────────────────────────────────────────────────────────────────
class HealthScoreCalculateView(APIView):
    """
    Triggers full health score recalculation for all active products.
    Delegates to services.calculate_health_scores() for logic.
    Also calculates Category_Health_Score aggregates.
    """
    permission_classes = [IsManagerOrAdmin]

    def post(self, request):
        from inventory.services.health_score import calculate_health_scores
        result = calculate_health_scores()

        log_action(
            user=request.user, action='CALCULATE', table_name='inventory_health_score',
            record_id=None, old_value=None,
            new_value={
                'products_processed': result['products_processed'],
                'summary': result['summary'],
            },
            request=request,
        )

        return Response({
            'message'           : 'Health score calculation complete',
            'products_processed': result['products_processed'],
            'summary'           : result['summary'],
            'last_calculated_at': result['calculated_at'],
        }, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────
# GET /api/health-scores/
# Filter by ?status=CRITICAL|AT RISK|WATCH|HEALTHY
# ─────────────────────────────────────────────────────────────────
class HealthScoreListView(APIView):
    """
    GET /api/health-scores/
    Returns the LATEST health score per product (one row per product,
    not one row per calculation run). Filter by ?status= and/or
    ?product=<id>, and now ?search= (matches product name or SKU).
    Includes product_name and sku_code so callers don't need a
    separate lookup per row.

    Paginated — was previously unpaginated, returning every product's
    health score on every page load. Manual page/page_size handling
    (matching AuditLogListView's envelope) since this is a plain
    APIView, not generics.ListAPIView. See core.pagination for the
    shared version used elsewhere; kept manual here only because this
    view already builds a plain dict list via .values() rather than a
    serializer, so the DRF pagination_class hook doesn't apply directly.

    NOTE: the KPI summary counts on the health_score.html dashboard
    (HEALTHY/WATCH/AT RISK/CRITICAL totals) previously came from
    counting this endpoint's full result set client-side — that would
    have broken silently once this endpoint stopped returning everything
    at once. Fixed by wiring up the already-existing (but previously
    unrouted) HealthScoreSummaryView at GET /api/health-scores/summary/
    instead — see urls.py — which computes those counts with a proper
    aggregate query, independent of pagination. The frontend was updated
    to call that endpoint for the KPI cards instead of counting the list.
    """
 
    def get(self, request):
        from django.db.models import OuterRef, Subquery, Q
 
        latest_ids = (
            InventoryHealthScore.objects
            .filter(product_id=OuterRef('product_id'))
            .order_by('-calculated_date', '-id')
            .values('id')[:1]
        )
        queryset = InventoryHealthScore.objects.filter(
            id__in=Subquery(latest_ids)
        ).select_related('product', 'product__category').order_by('overall_score')
 
        status_filter = request.query_params.get('status')
        product_filter = request.query_params.get('product')
        search = request.query_params.get('search')
 
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if product_filter:
            queryset = queryset.filter(product_id=product_filter)
        if search:
            queryset = queryset.filter(
                Q(product__product_name__icontains=search) |
                Q(product__sku_code__icontains=search)
            )
 
        queryset = queryset.values(
            'id', 'product', 'product__product_name', 'product__sku_code',
            'product__category__category_name',
            'velocity_score', 'margin_score',
            'expiry_risk_score', 'stock_duration_score', 'rating_score',
            'overall_score', 'status', 'recommended_action',
            'rating_sufficient', 'weighting_mode', 'calculated_date', 'calculated_at'
        )

        try:
            page = max(1, int(request.query_params.get('page', 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(request.query_params.get('page_size', 25))
        except (TypeError, ValueError):
            page_size = 25
        page_size = max(1, min(page_size, 100))

        total_count = queryset.count()
        total_pages = max(1, -(-total_count // page_size))
        page = min(page, total_pages)
        start = (page - 1) * page_size
        page_rows = list(queryset[start:start + page_size])

        return Response({
            'results': page_rows,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
        })
 

 



class HealthScoreSummaryView(APIView):
 
    def get(self, request):
        from django.db.models import Count, OuterRef, Subquery
        from products.models import Product

        active_product_count = Product.objects.filter(is_active=True).count()
 
        latest_ids = (
            InventoryHealthScore.objects
            .filter(product_id=OuterRef('product_id'), product__is_active=True)
            .order_by('-calculated_date', '-id')
            .values('id')[:1]
        )
        latest_qs = InventoryHealthScore.objects.filter(
            id__in=Subquery(latest_ids)
        )

        # A partial calculation must not make the KPI cards look like they
        # describe the whole catalogue. Generate the missing active-product
        # scores before counting statuses, then rebuild the latest queryset.
        if latest_qs.count() < active_product_count:
            from inventory.services.health_score import calculate_health_scores
            calculate_health_scores()
            latest_qs = InventoryHealthScore.objects.filter(
                id__in=Subquery(latest_ids)
            )
 
        counts = latest_qs.values('status').annotate(count=Count('id'))
        latest_record = latest_qs.order_by('-calculated_at', '-calculated_date', '-id').first()
 
        summary = {
            'HEALTHY':  0,
            'WATCH':    0,
            'AT RISK':  0,
            'CRITICAL': 0,
        }
        for row in counts:
            if row['status'] in summary:
                summary[row['status']] = row['count']

        last_calculated_at = None
        if latest_record is not None:
            last_calculated_at = latest_record.calculated_at.isoformat() if latest_record.calculated_at else latest_record.calculated_date.isoformat()
 
        return Response({
            'summary': summary,
            'total':   sum(summary.values()),
            'active_product_count': active_product_count,
            'last_calculated_at': last_calculated_at,
            'note': (
                'Call POST /api/health-scores/calculate/ first if all counts '
                'are 0. For the full product list use GET /api/health-scores/.'
            )
        })


# ─────────────────────────────────────────────────────────────────
# GET /api/health-scores/categories/
# ⚠ Must be registered BEFORE health-scores/<int:product_id>/
# ─────────────────────────────────────────────────────────────────
class CategoryHealthScoreView(APIView):
    """
    GET /api/health-scores/categories/
    Returns the LATEST CategoryHealthScore per category, including
    category_name (not just the raw category id).
    """
 
    def get(self, request):
        from django.db.models import OuterRef, Subquery
 
        latest_ids = (
            CategoryHealthScore.objects
            .filter(category_id=OuterRef('category_id'))
            .order_by('-calculated_date', '-id')
            .values('id')[:1]
        )
        queryset = CategoryHealthScore.objects.filter(
            id__in=Subquery(latest_ids)
        ).select_related('category').order_by('avg_health_score')
 
        data = queryset.values(
            'id', 'category', 'category__category_name', 'avg_health_score',
            'healthy_count', 'watch_count', 'at_risk_count',
            'critical_count', 'status', 'calculated_date', 'calculated_at'
        )
        return Response(list(data))



# ─────────────────────────────────────────────────────────────────
# GET /api/health-scores/critical/
# ⚠ Must be registered BEFORE health-scores/<int:product_id>/
# ─────────────────────────────────────────────────────────────────
class HealthScoreCriticalView(APIView):
    """
    GET /api/health-scores/critical/
    Returns the LATEST health score record for every product currently
    at CRITICAL status, including product_name and sku_code.
    """
 
    def get(self, request):
        from django.db.models import OuterRef, Subquery
 
        latest_ids = (
            InventoryHealthScore.objects
            .filter(product_id=OuterRef('product_id'))
            .order_by('-calculated_date', '-id')
            .values('id')[:1]
        )
        queryset = InventoryHealthScore.objects.filter(
            id__in=Subquery(latest_ids),
            status='CRITICAL'
        ).select_related('product').order_by('overall_score')
 
        data = queryset.values(
            'id', 'product', 'product__product_name', 'product__sku_code',
            'overall_score', 'status',
            'recommended_action', 'calculated_date', 'calculated_at'
        )
        return Response(list(data))


# ─────────────────────────────────────────────────────────────────
# GET /api/health-scores/<product_id>/
# Full health score history for one product across all runs
# ─────────────────────────────────────────────────────────────────
class HealthScoreDetailView(APIView):
    """
    GET /api/health-scores/<product_id>/
    Returns the SINGLE latest health score record for one product
    (today's breakdown), including product_name and sku_code.
 
    For full multi-run history (trend view), use
    GET /api/health-scores/history/<product_id>/ instead.
    """
 
    def get(self, request, product_id):
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'},
                            status=status.HTTP_404_NOT_FOUND)
 
        record = InventoryHealthScore.objects.filter(
            product=product
        ).order_by('-calculated_date', '-id').first()
 
        if record is None:
            return Response(
                {'error': 'No health score calculated yet for this product. '
                          'Call POST /api/health-scores/calculate/ first.'},
                status=status.HTTP_404_NOT_FOUND
            )
 
        data = {
            'id': record.id,
            'product': record.product_id,
            'product_name': product.product_name,
            'sku_code': product.sku_code,
            'velocity_score': record.velocity_score,
            'margin_score': record.margin_score,
            'expiry_risk_score': record.expiry_risk_score,
            'stock_duration_score': record.stock_duration_score,
            'rating_score': record.rating_score,
            'overall_score': record.overall_score,
            'status': record.status,
            'recommended_action': record.recommended_action,
            'rating_sufficient': record.rating_sufficient,
            'weighting_mode': record.weighting_mode,
            'calculated_date': record.calculated_date,
            'calculated_at': record.calculated_at.isoformat() if record.calculated_at else None,
        }
        return Response(data)

class HealthScoreHistoryView(APIView):
    """
    GET /api/health-scores/history/<product_id>/
    Full health score history for one product across ALL calculation
    runs (trend view) -- per API Design Doc Section 13. Includes
    product_name and sku_code for consistency with the other views.
    """
 
    def get(self, request, product_id):
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'},
                            status=status.HTTP_404_NOT_FOUND)
 
        queryset = InventoryHealthScore.objects.filter(
            product=product
        ).select_related('product', 'product__category').order_by('-calculated_date')
 
        data = queryset.values(
            'id', 'product', 'product__product_name', 'product__sku_code',
            'product__category__category_name',
            'velocity_score', 'margin_score',
            'expiry_risk_score', 'stock_duration_score', 'rating_score',
            'overall_score', 'status', 'recommended_action',
            'rating_sufficient', 'weighting_mode', 'calculated_date', 'calculated_at'
        )
        return Response(list(data))
    
"""
Plan spec:
    Calls run_lifecycle_calculation() for each active product.
    Returns product_name, lifecycle_status, recommendation.
    Classification order (STRICT — from plan and pseudocode):
        NEW → GROWING → DECLINING → SLOW_MOVING → STABLE
    Staff JWT required.

IMPORTANT NOTE on GET vs POST:
    POST /api/lifecycle/calculate/ (already exists — LifecycleCalculateView)
    saves to DB. GET /api/lifecycle/ (already exists — LifecycleListView)
    reads latest from DB but does NOT include 'recommendation' grouping/
    summary counts the Week 5 plan asks for.
    This new endpoint is a separate REPORTING view — it reads the same
    ProductLifecycle table but adds: status-grouped summary counts,
    a guaranteed recommendation mapping (self-healing if DB has stale
    values), and a friendly empty-state message. It does NOT recalculate —
    GET should never trigger DB writes.
"""


DEFAULT_RECOMMENDATION_MAP = {
    'NEW':          'MONITOR',
    'GROWING':      'RETAIN',
    'STABLE':       'RETAIN',
    'DECLINING':    'DISCOUNT',
    'SLOW_MOVING':  'CLEARANCE',  # fallback only — actual saved value on
                                   # record.recommendation may be PHASE_OUT
                                   # after 3 consecutive SLOW_MOVING runs.
                                   # See inventory/services/lifecycle.py.
}

STATUS_ORDER = {
    'DECLINING':   0,   # most urgent — show first
    'SLOW_MOVING': 1,
    'NEW':         2,
    'GROWING':     3,
    'STABLE':      4,
}


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def lifecycle_analytics(request):
    """
    GET /api/analytics/lifecycle/

    Query params (optional):
        ?status=GROWING            filter by one status
        ?recommendation=DISCOUNT   filter by recommendation
        ?search=chip                filter by product name

    Response per product:
        product_id          int
        product_name        str
        sku_code             str
        lifecycle_status     str  — NEW | GROWING | STABLE | DECLINING | SLOW_MOVING
        recommendation        str  — RETAIN | MONITOR | DISCOUNT | CLEARANCE | PHASE_OUT
        sales_velocity         float — avg units/day in the calculation period
        comparison_period      str  — YYYY-MM format
        calculated_date        str  — when this record was generated

    If no lifecycle calculation has been run yet:
        Returns 200 with empty results list and a note.

    Auth: Staff JWT required
    """
    from django.db.models import OuterRef, Subquery
    from collections import Counter

    # ── Get latest lifecycle record per product ──────────────────────────────
    latest_dates = (
        ProductLifecycle.objects
        .filter(product_id=OuterRef('product_id'))
        .order_by('-calculated_date', '-id')
        .values('id')[:1]
    )

    latest_records = ProductLifecycle.objects.filter(
        id__in=Subquery(latest_dates)
    ).select_related('product')

    if not latest_records.exists():
        return Response({
            'results': [],
            'total':   0,
            'note':    (
                'No lifecycle calculation has been run yet. '
                'Trigger POST /api/lifecycle/calculate/ first, '
                'then this endpoint will return the results.'
            ),
        })

    # ── Optional filters ──────────────────────────────────────────────────────
    status_filter = request.query_params.get('status', '').upper()
    rec_filter    = request.query_params.get('recommendation', '').upper()
    search_term   = request.query_params.get('search', '').strip()

    if status_filter:
        latest_records = latest_records.filter(status=status_filter)
    if rec_filter:
        latest_records = latest_records.filter(recommendation=rec_filter)
    if search_term:
        latest_records = latest_records.filter(
            product__product_name__icontains=search_term
        )

    # ── Serialize ─────────────────────────────────────────────────────────────
    results = []
    for record in latest_records:
        # PHASE_OUT vs CLEARANCE for SLOW_MOVING depends on multi-period
        # streak history (see lifecycle.py) — cannot be derived from status
        # alone. Trust the saved value; only fall back to the default map
        # when the DB value is genuinely blank/null (a stale/legacy row).
        recommendation = record.recommendation or DEFAULT_RECOMMENDATION_MAP.get(
            record.status, 'MONITOR'
        )

        results.append({
            'product_id':        record.product.id,
            'product_name':      record.product.product_name,
            'sku_code':          record.product.sku_code or '',
            'reorder_threshold': record.product.reorder_threshold,
            'lifecycle_status':  record.status,
            'recommendation':    recommendation,
            'sales_velocity':    float(record.sales_velocity) if record.sales_velocity else None,
            'comparison_period': record.comparison_period or '',
            'calculated_date':   str(record.calculated_date),
        })

    results.sort(key=lambda r: STATUS_ORDER.get(r['lifecycle_status'], 9))

    status_counts = Counter(r['lifecycle_status'] for r in results)

    response_data = {
        'results': results,
        'total':   len(results),
        'summary': {
            'NEW':         status_counts.get('NEW', 0),
            'GROWING':     status_counts.get('GROWING', 0),
            'STABLE':      status_counts.get('STABLE', 0),
            'DECLINING':   status_counts.get('DECLINING', 0),
            'SLOW_MOVING': status_counts.get('SLOW_MOVING', 0),
        },
    }
    if not (status_filter or rec_filter or search_term):
        response_data['note'] = (
            'This endpoint reads the most recent calculation run. '
            'To recalculate, call POST /api/lifecycle/calculate/ first.'
        )

    return Response(response_data)

 
 
# ═════════════════════════════════════════════════════════════════
# F09 — Discount Rules (config CRUD only — NOT the calculation
# engine. discount_engine.py / POST /api/discounts/calculate/ stay
# blocked until the project lead confirms tier values.)
# ═════════════════════════════════════════════════════════════════
 
class DiscountRuleListCreateView(APIView):
    """
    GET  /api/discount-rules/   — all tiered discount rules
    POST /api/discount-rules/   — create a new rule tier. Admin/Manager.
    """

    def get_permissions(self):
        if self.request.method == 'POST':
            return [IsManagerOrAdmin()]
        return [IsAuthenticated()]

    def get(self, request):
        rules = DiscountRule.objects.all().order_by('days_from_expiry_min')
        return Response(DiscountRuleSerializer(rules, many=True).data)
 
    def post(self, request):
        serializer = DiscountRuleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # left null here, same gap as everywhere else in this codebase that
        # still references AppUser instead of settings.AUTH_USER_MODEL.
        rule = serializer.save(created_by=request.user)
 
        log_action(
            user=request.user, action='CREATE', table_name='discount_rule',
            record_id=rule.id, old_value=None,
            new_value=DiscountRuleSerializer(rule).data, request=request,
        )
        return Response(DiscountRuleSerializer(rule).data, status=status.HTTP_201_CREATED)
 
 
class DiscountRuleDetailView(APIView):
    """
    PUT   /api/discount-rules/{id}/  — full update
    PATCH /api/discount-rules/{id}/  — partial update. Used for soft
          deactivation: body {"is_active": false}. Per API doc v3.1:
          hard DELETE would break FK integrity on historical
          DiscountRecommendation rows, so deactivation is PATCH-only,
          there is no DELETE.
    """
    permission_classes = [IsManagerOrAdmin]
 
    def get_object(self, pk):
        try:
            return DiscountRule.objects.get(pk=pk)
        except DiscountRule.DoesNotExist:
            return None
 
    def put(self, request, pk):
        rule = self.get_object(pk)
        if rule is None:
            return Response({'error': 'Discount rule not found'}, status=status.HTTP_404_NOT_FOUND)
 
        old_value = DiscountRuleSerializer(rule).data
        serializer = DiscountRuleSerializer(rule, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
 
        log_action(
            user=request.user, action='UPDATE', table_name='discount_rule',
            record_id=rule.id, old_value=old_value,
            new_value=serializer.data, request=request,
        )
        return Response(serializer.data)
 
    def patch(self, request, pk):
        rule = self.get_object(pk)
        if rule is None:
            return Response({'error': 'Discount rule not found'}, status=status.HTTP_404_NOT_FOUND)
 
        old_value = DiscountRuleSerializer(rule).data
        serializer = DiscountRuleSerializer(rule, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
 
        log_action(
            user=request.user, action='UPDATE', table_name='discount_rule',
            record_id=rule.id, old_value=old_value,
            new_value=serializer.data, request=request,
        )
        return Response(serializer.data)
 
 
# ═════════════════════════════════════════════════════════════════
# F09 — Discount Recommendations (read + review only).
# POST /api/discounts/calculate/ is NOT built here — that's the
# blocked calculation engine. This just serves whatever rows exist
# (empty list until calculate/ is built) and lets a manager mark
# a recommendation APPLIED/IGNORED.
# ═════════════════════════════════════════════════════════════════
 
class DiscountRecommendationListView(generics.ListAPIView):
    """
    GET /api/discounts/recommendations/
    Filter by ?status=PENDING/APPLIED/IGNORED/EXPIRED or ?urgency=
    ('urgency' here maps to days_until_expiry ranges, kept simple
    as a direct status filter per the API doc wording.)
    """
    serializer_class = DiscountRecommendationSerializer
 
    def get_queryset(self):
        queryset = DiscountRecommendation.objects.select_related('product').order_by('days_until_expiry')
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return queryset
 
 
class DiscountRecommendationDetailView(APIView):
    """
    PATCH /api/discounts/recommendations/{id}/
    Manager marks a recommendation APPLIED or IGNORED.
    Body: {"status": "APPLIED"}  or  {"status": "IGNORED"}
    """
    permission_classes = [IsManagerOrAdmin]

    def patch(self, request, pk):
        try:
            rec = DiscountRecommendation.objects.get(pk=pk)
        except DiscountRecommendation.DoesNotExist:
            return Response({'error': 'Discount recommendation not found'}, status=status.HTTP_404_NOT_FOUND)
 
        new_status = request.data.get('status')
        if new_status not in ['APPLIED', 'IGNORED']:
            return Response(
                {'error': 'status must be APPLIED or IGNORED'},
                status=status.HTTP_400_BAD_REQUEST
            )
 
        old_value = {'status': rec.status}
        rec.status = new_status
        rec.reviewed_by = request.user
        rec.reviewed_at = dj_timezone.now()
        rec.save()
 
        log_action(
            user=request.user, action='UPDATE', table_name='discount_recommendation',
            record_id=rec.id, old_value=old_value,
            new_value={'status': rec.status}, request=request,
        )
 
        return Response(DiscountRecommendationSerializer(rec).data)
    


class DiscountCalculateView(APIView):
    permission_classes = [IsManagerOrAdmin]

    def post(self, request):
        from inventory.services.discount_engine import calculate_discounts
        result = calculate_discounts()

        log_action(
            user=request.user, action='CALCULATE', table_name='discount_recommendation',
            record_id=None, old_value=None,
            new_value=result, request=request,
        )

        return Response({
            'message': (
                f"Discount calculation complete — "
                f"{result['recommendations_created']} created, "
                f"{result['recommendations_updated']} updated."
            ),
            **result,
        }, status=status.HTTP_200_OK)
    
    
    
class SyncDateView(APIView):
    """
    GET /api/inventory/sync-date/
    Returns the last_item_ledger_sync value from SystemConfig.
    """

    def get(self, request):
        return Response({'last_sync_date': get_last_sync_date()})


class LastUploadsByTypeView(APIView):
    """
    GET /api/inventory/last-uploads-by-type/
    Return the latest eligible upload for each type contributing to Dashboard sync.
    """

    def get(self, request):
        uploads, latest_overall = get_latest_sync_uploads()
        results = []
        for upload_type, row in uploads:
            if row:
                results.append({
                    'upload_type': upload_type,
                    'found': True,
                    'file_name': row.file_name,
                    'status': row.status,
                    'upload_date': row.upload_date,
                    'error_message': row.error_message,
                })
            else:
                results.append({'upload_type': upload_type, 'found': False})

        return Response({
            'current_sync_type': latest_overall.upload_type if latest_overall else None,
            'current_sync_date': latest_overall.upload_date if latest_overall else None,
            'uploads': results,
        })
    

class ReorderCalculateView(APIView):
    """
    POST /api/reorder/calculate/
 
    Triggers reorder calculation for all products via check_reorder_needs().
 
    Uses update_or_create keyed on (product, status='PENDING') so repeated
    calculation runs refresh an existing PENDING recommendation instead of
    creating duplicates. Recommendations already ORDERED/IGNORED are left
    untouched -- actioned history is preserved. If a product still needs
    reordering after being actioned, a fresh new PENDING row is created
    (the old ORDERED/IGNORED row stays as-is).
 
    Notifications only fire when a recommendation is newly created, or when
    an existing PENDING recommendation's urgency escalates to CRITICAL from
    a lower urgency on this run -- prevents notification spam from repeated
    recalculation of an already-known critical item.
 
    Any PENDING recommendation for a product that no longer appears in this
    run's results (no longer needs reordering) is marked AUTO_RESOLVED
    rather than deleted -- preserves the fact it was once flagged and has
    since resolved, matching the report export's expectation of showing
    "flagged, now resolved" instead of a misleadingly-stale PENDING.
 
    Optional body: {"as_of": "2026-02-14"} — for testing against
    frozen sample data only. Production calls should omit this and
    let it default to today.
    """
    permission_classes = [IsManagerOrAdmin]
 
    def post(self, request):
        as_of_str = request.data.get('as_of')
        as_of = None
        if as_of_str:
            try:
                as_of = date.fromisoformat(as_of_str)
            except ValueError:
                return Response({'error': 'as_of must be YYYY-MM-DD'}, status=status.HTTP_400_BAD_REQUEST)
 
        results = check_reorder_needs(as_of=as_of)
 
        touched_product_ids = set()
        created_or_updated = []
        notifications_created = 0
 
        # Bulk-fetch every existing PENDING recommendation for the products
        # in this run's results, up front — was previously one
        # ReorderRecommendation.objects.filter(...).first() query PER
        # result inside the loop below, on top of the N+1 already fixed
        # in check_reorder_needs() itself. For a run with, say, 80
        # products needing reorder, that's 80 fewer queries here.
        result_product_ids = [r['product_id'] for r in results]
        existing_by_product = {
            rec.product_id: rec
            for rec in ReorderRecommendation.objects.filter(
                product_id__in=result_product_ids, status='PENDING'
            )
        }
 
        for r in results:
            touched_product_ids.add(r['product_id'])
 
            # Capture previous urgency BEFORE update_or_create overwrites it,
            # so we can detect a genuine escalation vs. a repeat of the same
            # urgency level.
            existing = existing_by_product.get(r['product_id'])
            previous_urgency = existing.urgency if existing else None
 
            rec, was_created = ReorderRecommendation.objects.update_or_create(
                product_id=r['product_id'],
                status='PENDING',
                defaults={
                    'supplier_id': r['supplier_id'],
                    'current_stock': r['current_stock'],
                    'avg_daily_sales': r['avg_daily_sales'],
                    'days_of_stock': r['days_of_stock'],
                    'safety_stock': r['safety_stock'],
                    'suggested_quantity': r['suggested_quantity'],
                    'estimated_cost': r['estimated_cost'],
                    'urgency': r['urgency'],
                }
            )
            created_or_updated.append(rec)
 
            escalated_to_critical = (
                previous_urgency is not None
                and previous_urgency != 'CRITICAL'
                and r['urgency'] == 'CRITICAL'
            )
 
            if r['urgency'] == 'CRITICAL' and (was_created or escalated_to_critical):
                # Local import to avoid any cross-app circular import risk.
                from orders.models import Notification
                Notification.objects.create(
                    user=None,  # AppUser FK gap — same issue flagged elsewhere
                    customer=None,
                    type='REORDER',
                    priority='CRITICAL',
                    title='Critical reorder needed',
                    message=f"{r['product_name']} is at {r['days_of_stock']} days of stock — reorder now.",
                    reference_table='reorder_recommendation',
                    reference_id=rec.id,
                )
                notifications_created += 1
 
        # ── Auto-resolve stale PENDING recs for products no longer needing reorder ──
        stale_resolved = ReorderRecommendation.objects.filter(
            status='PENDING'
        ).exclude(product_id__in=touched_product_ids).update(status='AUTO_RESOLVED')
 
        log_action(
            user=request.user, action='CALCULATE', table_name='reorder_recommendation',
            record_id=None, old_value=None,
            new_value={
                'recommendations_created_or_updated': len(created_or_updated),
                'notifications_created': notifications_created,
                'auto_resolved': stale_resolved,
            }, request=request,
        )
 
        return Response({
            'message': (
                f'Reorder calculation complete — {len(created_or_updated)} recommendation(s) '
                f'created/updated, {notifications_created} notification(s) sent, '
                f'{stale_resolved} previously-pending recommendation(s) auto-resolved.'
            ),
            'recommendations': ReorderRecommendationSerializer(created_or_updated, many=True).data,
        }, status=status.HTTP_201_CREATED)

 
class ReorderRecommendationListView(generics.ListAPIView):
    """
    GET /api/reorder/recommendations/
    Filter by ?urgency=CRITICAL/HIGH/MEDIUM/LOW and ?status=PENDING/ORDERED/IGNORED
    Paginated — was previously unpaginated. See core.pagination.
    """
    pagination_class = StandardResultsPagination
    serializer_class = ReorderRecommendationSerializer
 
    def get_queryset(self):
        queryset = ReorderRecommendation.objects.select_related(
            'product', 'supplier'
        ).order_by('-calculation_date')
        urgency = self.request.query_params.get('urgency')
        status_filter = self.request.query_params.get('status')
        if urgency:
            queryset = queryset.filter(urgency=urgency)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return queryset
 
 
class ReorderRecommendationDetailView(APIView):
    """
    PATCH /api/reorder/recommendations/{id}/
    Staff/Manager marks recommendation ORDERED or IGNORED.
    Body: {"status": "ORDERED"} or {"status": "IGNORED"}
    """
    permission_classes = [IsManagerOrAdmin]
 
    def patch(self, request, pk):
        try:
            rec = ReorderRecommendation.objects.get(pk=pk)
        except ReorderRecommendation.DoesNotExist:
            return Response({'error': 'Reorder recommendation not found'}, status=status.HTTP_404_NOT_FOUND)
 
        new_status = request.data.get('status')
        if new_status not in ['ORDERED', 'IGNORED']:
            return Response({'error': 'status must be ORDERED or IGNORED'}, status=status.HTTP_400_BAD_REQUEST)
 
        old_value = {'status': rec.status}
        rec.status = new_status
        rec.actioned_by = request.user
        rec.save()
 
        log_action(
            user=request.user, action='UPDATE', table_name='reorder_recommendation',
            record_id=rec.id, old_value=old_value,
            new_value={'status': rec.status}, request=request,
        )
 
        return Response(ReorderRecommendationSerializer(rec).data)
 


class NotificationListView(APIView):
    """
    GET /api/notifications/
    GET /api/notifications/?status=unread|read|all   (default: unread)
    Staff-facing (customer is null).

    Read/dismiss state is now tracked PER USER via orders.models.NotificationRead
    — the Notification row itself stays shared (one alert, created once), but
    each staff member's read/dismiss status is independent. Marking read or
    dismissing on one manager's login no longer affects another manager's view
    of the same alert.

    NOTE: Notification.user is still a legacy AppUser FK (same gap flagged
    elsewhere in this project — it's never reliably populated), so this
    returns ALL staff notifications matching the status filter, not
    filtered to "my" notifications at the Notification level. Per-user
    read/dismiss state above is unrelated to that gap and works correctly
    regardless of it.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        status_param = request.query_params.get('status', 'unread').lower()
        if status_param not in ('unread', 'read', 'all'):
            return Response(
                {'error': 'status must be unread, read, or all'},
                status=status.HTTP_400_BAD_REQUEST
            )

        from orders.models import NotificationRead

        notifications = Notification.objects.filter(
            customer__isnull=True
        ).order_by('-created_at')

        read_states = {
            rs.notification_id: rs
            for rs in NotificationRead.objects.filter(
                user=request.user, notification__in=notifications
            )
        }

        data = []
        for n in notifications:
            rs = read_states.get(n.id)
            is_read = rs.is_read if rs else False

            if rs and rs.is_dismissed:
                continue
            if status_param == 'unread' and is_read:
                continue
            if status_param == 'read' and not is_read:
                continue

            data.append({
                'id': n.id,
                'type': n.type,
                'priority': n.priority,
                'title': n.title,
                'message': n.message,
                'reference_table': n.reference_table,
                'reference_id': n.reference_id,
                'is_read': is_read,
                'read_at': rs.read_at if rs else None,
                'created_at': n.created_at,
            })

        return Response(data)


class NotificationDetailView(APIView):
    """
    GET    /api/notifications/{id}/   — full detail with reference_table/id
    PATCH  /api/notifications/{id}/read/  — mark as read (separate route, see urls.py)
    DELETE /api/notifications/{id}/   — dismiss FOR THE REQUESTING USER ONLY.
           This is a per-user soft dismiss via NotificationRead.is_dismissed,
           NOT a database delete — the shared Notification row is never
           removed, so other staff members still see it until they dismiss
           it themselves.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            n = Notification.objects.get(pk=pk)
        except Notification.DoesNotExist:
            return Response({'error': 'Notification not found'}, status=status.HTTP_404_NOT_FOUND)

        from orders.models import NotificationRead
        rs = NotificationRead.objects.filter(notification=n, user=request.user).first()

        return Response({
            'id': n.id,
            'type': n.type,
            'priority': n.priority,
            'title': n.title,
            'message': n.message,
            'reference_table': n.reference_table,
            'reference_id': n.reference_id,
            'is_read': rs.is_read if rs else False,
            'created_at': n.created_at,
            'read_at': rs.read_at if rs else None,
            'expires_at': n.expires_at,
        })

    def delete(self, request, pk):
        try:
            n = Notification.objects.get(pk=pk)
        except Notification.DoesNotExist:
            return Response({'error': 'Notification not found'}, status=status.HTTP_404_NOT_FOUND)

        from orders.models import NotificationRead
        rs, _ = NotificationRead.objects.get_or_create(notification=n, user=request.user)
        rs.is_dismissed = True
        rs.dismissed_at = dj_timezone.now()
        rs.save()

        return Response({'message': 'Notification dismissed for you'}, status=status.HTTP_204_NO_CONTENT)


class NotificationMarkReadView(APIView):
    """
    PATCH /api/notifications/{id}/read/
    Sets is_read=True, read_at=now — FOR THE REQUESTING USER ONLY, via
    orders.models.NotificationRead. The shared Notification row is never
    modified, so this has no effect on any other staff member's view.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            n = Notification.objects.get(pk=pk)
        except Notification.DoesNotExist:
            return Response({'error': 'Notification not found'}, status=status.HTTP_404_NOT_FOUND)

        from orders.models import NotificationRead
        rs, _ = NotificationRead.objects.get_or_create(notification=n, user=request.user)
        rs.is_read = True
        rs.read_at = dj_timezone.now()
        rs.save()

        return Response({
            'id': n.id, 'is_read': rs.is_read, 'read_at': rs.read_at,
        })




class NotificationUnreadCountView(APIView):
    def get(self, request):
        from orders.models import Notification, NotificationRead
        read_ids = NotificationRead.objects.filter(
            user=request.user, is_read=True
        ).values_list('notification_id', flat=True)
        count = Notification.objects.exclude(id__in=read_ids).count()
        return Response({'unread_count': count})