from datetime import date, timedelta  
from users.permissions import IsManagerOrAdmin  
from decimal import Decimal
from users.audit import log_action

from django.db.models import Sum
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from products.models import Product
from purchases.models import PurchaseBatch
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
from inventory.services.reorder_logic import get_urgency


from inventory.services.reorder_logic import check_reorder_needs

from orders.models import Notification

from datetime import date
#   from inventory.services.reorder_logic import check_reorder_needs


from django.utils import timezone as dj_timezone

from django.utils import timezone as dj_timezone
from core.utils import get_last_sync_date, get_latest_sync_uploads


# ═════════════════════════════════════════════════════════════════
# F03 — Inventory & Stock
# ═════════════════════════════════════════════════════════════════

class StockSnapshotView(APIView):
    def get(self, request):
        last_sync = get_last_sync_date()
        products  = Product.objects.filter(is_active=True)
        result    = []

        for product in products:
            current_stock = PurchaseBatch.objects.filter(
                product=product, status='ACTIVE'
            ).aggregate(total=Sum('remaining_quantity'))['total'] or 0

            reorder = product.reorder_threshold or 0
            if current_stock == 0:
                stock_status = 'OUT OF STOCK'
            elif current_stock <= reorder:
                stock_status = 'LOW STOCK'
            else:
                stock_status = 'AVAILABLE'

            result.append({
                'product_id'       : product.id,
                'product_name'     : product.product_name,
                'sku_code'         : product.sku_code,
                'current_stock'    : current_stock,
                'reorder_threshold': reorder,
                'stock_status'     : stock_status,
                'avg_cost_price'   : str(product.avg_cost_price),
                'last_sync_date'   : last_sync,
            })

        return Response({
            'last_sync_date': last_sync,
            'note'          : 'Stock is snapshot-based.',
            'count'         : len(result),
            'stock'         : result
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

        return Response({
            'product_id'         : product.id,
            'product_name'       : product.product_name,
            'sku_code'           : product.sku_code,
            'avg_cost_price'     : str(product.avg_cost_price),
            'total_current_stock': total_stock,
            'reorder_threshold'  : reorder,
            'stock_status'       : stock_status,
            'last_sync_date'     : get_last_sync_date(),
            'active_batch_count' : len(batch_data),
            'batches'            : batch_data
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
            product=product, status='ACTIVE'
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
 
    today = date.today()
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
        current_stock        int     — SUM of remaining_quantity from ACTIVE batches
        reorder_threshold    int     — Product.reorder_threshold
        shortage              int     — how many units below reorder_threshold
        urgency               str     — CRITICAL | HIGH | MEDIUM | LOW
        days_of_stock         float|None — None if no sales data (avg_daily == 0)
        avg_daily_sales       float
    """
 
    def get(self, request):
        today = date.today()
        since = today - timedelta(days=SALES_LOOKBACK_DAYS)
 
        products = Product.objects.filter(is_active=True)
 
        # Bulk-fetch all active batch stock in one query (avoid N+1)
        stock_by_product = {
            row['product']: row['total']
            for row in PurchaseBatch.objects.filter(
                status='ACTIVE',
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
    def get(self, request):
        products = Product.objects.filter(is_active=True)
        out      = []

        for product in products:
            current = PurchaseBatch.objects.filter(
                product=product, status='ACTIVE'
            ).aggregate(total=Sum('remaining_quantity'))['total'] or 0

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
    def get(self, request, product_id):
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'},
                            status=status.HTTP_404_NOT_FOUND)

        queryset = ProductLifecycle.objects.filter(
            product=product
        ).order_by('-calculated_date')
        data = queryset.values(
            'id', 'product', 'status', 'recommendation',
            'sales_velocity', 'calculated_date'
        )
        return Response(list(data))


# ═════════════════════════════════════════════════════════════════
# F07 — Loss & Supplier Returns
# ═════════════════════════════════════════════════════════════════

class LossRecordView(APIView):

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

        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'},
                            status=status.HTTP_404_NOT_FOUND)

        loss_value = int(loss_quantity) * (product.avg_cost_price or 0)

        record = LossRecord.objects.create(
            product       = product,
            loss_type     = loss_type,
            loss_quantity = int(loss_quantity),
            loss_value    = loss_value,
            loss_date     = date.today(),
            notes         = notes,
        )

        log_action(
            user=request.user, action='CREATE', table_name='loss_record',
            record_id=record.id, old_value=None,
            new_value={
                'product': product.product_name,
                'loss_type': loss_type,
                'loss_quantity': int(loss_quantity),
                'loss_value': str(loss_value),
            },
            request=request,
        )

        return Response({
            'message'      : 'Loss recorded successfully',
            'loss_id'      : record.id,
            'product'      : product.product_name,
            'loss_type'    : loss_type,
            'loss_quantity': int(loss_quantity),
            'loss_value'   : str(loss_value),
        }, status=status.HTTP_201_CREATED)


class LossSummaryView(APIView):

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

    def post(self, request):
        today   = date.today()
        expired = PurchaseBatch.objects.filter(
            status='ACTIVE',
            expiry_date__lt=today,
            remaining_quantity__gt=0
        )
        created = 0

        for batch in expired:
            already = LossRecord.objects.filter(
                batch=batch, loss_type='EXPIRY'
            ).exists()
            if already:
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

            batch.status = 'EXPIRED'
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

    def get(self, request):
        queryset    = SupplierReturn.objects.all().order_by('-return_date')
        supplier_id = request.query_params.get('supplier')
        ret_status  = request.query_params.get('status')

        if supplier_id:
            queryset = queryset.filter(supplier__id=supplier_id)
        if ret_status:
            queryset = queryset.filter(status=ret_status)

        data = queryset.values(
            'id', 'supplier', 'product', 'batch',
            'return_date', 'quantity_returned', 'return_value',
            'return_reason', 'recovery_type', 'status', 'notes'
        )
        return Response(list(data))

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
            return_date       = date.today(),
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


class SupplierReturnStatusView(APIView):

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
    ?product=<id>. Includes product_name and sku_code so callers don't
    need a separate lookup per row.
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
            id__in=Subquery(latest_ids)
        ).select_related('product', 'product__category').order_by('overall_score')
 
        status_filter = request.query_params.get('status')
        product_filter = request.query_params.get('product')
 
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if product_filter:
            queryset = queryset.filter(product_id=product_filter)
 
        data = queryset.values(
            'id', 'product', 'product__product_name', 'product__sku_code',
            'product__category__category_name',
            'velocity_score', 'margin_score',
            'expiry_risk_score', 'stock_duration_score', 'rating_score',
            'overall_score', 'status', 'recommended_action',
            'rating_sufficient', 'weighting_mode', 'calculated_date', 'calculated_at'
        )
        return Response(list(data))
 

 



class HealthScoreSummaryView(APIView):
 
    def get(self, request):
        from django.db.models import Count, OuterRef, Subquery
 
        latest_ids = (
            InventoryHealthScore.objects
            .filter(product_id=OuterRef('product_id'))
            .order_by('-calculated_date', '-id')
            .values('id')[:1]
        )
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


RECOMMENDATION_MAP = {
    'NEW':          'MONITOR',
    'GROWING':      'RETAIN',
    'STABLE':       'RETAIN',
    'DECLINING':    'DISCOUNT',
    'SLOW_MOVING':  'DISCONTINUE',
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

    Response per product:
        product_id          int
        product_name        str
        sku_code             str
        lifecycle_status     str  — NEW | GROWING | STABLE | DECLINING | SLOW_MOVING
        recommendation        str  — RETAIN | MONITOR | DISCOUNT | DISCONTINUE
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

    if status_filter:
        latest_records = latest_records.filter(status=status_filter)
    if rec_filter:
        latest_records = latest_records.filter(recommendation=rec_filter)

    # ── Serialize ─────────────────────────────────────────────────────────────
    results = []
    for record in latest_records:
        recommendation = RECOMMENDATION_MAP.get(
            record.status, record.recommendation or 'MONITOR'
        )

        results.append({
            'product_id':        record.product.id,
            'product_name':      record.product.product_name,
            'sku_code':          record.product.sku_code or '',
            'lifecycle_status':  record.status,
            'recommendation':    recommendation,
            'sales_velocity':    float(record.sales_velocity) if record.sales_velocity else None,
            'comparison_period': record.comparison_period or '',
            'calculated_date':   str(record.calculated_date),
        })

    results.sort(key=lambda r: STATUS_ORDER.get(r['lifecycle_status'], 9))

    status_counts = Counter(r['lifecycle_status'] for r in results)

    return Response({
        'results': results,
        'total':   len(results),
        'summary': {
            'NEW':         status_counts.get('NEW', 0),
            'GROWING':     status_counts.get('GROWING', 0),
            'STABLE':      status_counts.get('STABLE', 0),
            'DECLINING':   status_counts.get('DECLINING', 0),
            'SLOW_MOVING': status_counts.get('SLOW_MOVING', 0),
        },
        'note': (
            'This endpoint reads the most recent calculation run. '
            'To recalculate, call POST /api/lifecycle/calculate/ first.'
        ),
    })

 
 
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
        # created_by is a legacy AppUser FK, not the real auth_user table —
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
 
        for r in results:
            touched_product_ids.add(r['product_id'])
 
            # Capture previous urgency BEFORE update_or_create overwrites it,
            # so we can detect a genuine escalation vs. a repeat of the same
            # urgency level.
            existing = ReorderRecommendation.objects.filter(
                product_id=r['product_id'], status='PENDING'
            ).first()
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
    """
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
    Unread notifications, staff-facing (customer is null).
    NOTE: Notification.user is still a legacy AppUser FK (same gap
    flagged elsewhere in this project — it's never reliably
    populated), so this currently returns ALL unread staff
    notifications rather than filtering to "my" notifications.
    Revisit once the AppUser → auth_user bridge is resolved.
    """
 
    def get(self, request):
        notifications = Notification.objects.filter(
            is_read=False, customer__isnull=True
        ).order_by('-created_at')
 
        data = [{
            'id': n.id,
            'type': n.type,
            'priority': n.priority,
            'title': n.title,
            'message': n.message,
            'reference_table': n.reference_table,
            'reference_id': n.reference_id,
            'is_read': n.is_read,
            'created_at': n.created_at,
        } for n in notifications]
 
        return Response(data)
 
 
class NotificationDetailView(APIView):
    """
    GET    /api/notifications/{id}/   — full detail with reference_table/id
    PATCH  /api/notifications/{id}/read/  — mark as read (separate route, see urls.py)
    DELETE /api/notifications/{id}/   — dismiss
    """
 
    def get(self, request, pk):
        try:
            n = Notification.objects.get(pk=pk)
        except Notification.DoesNotExist:
            return Response({'error': 'Notification not found'}, status=status.HTTP_404_NOT_FOUND)
 
        return Response({
            'id': n.id,
            'type': n.type,
            'priority': n.priority,
            'title': n.title,
            'message': n.message,
            'reference_table': n.reference_table,
            'reference_id': n.reference_id,
            'is_read': n.is_read,
            'created_at': n.created_at,
            'read_at': n.read_at,
            'expires_at': n.expires_at,
        })
 
    def delete(self, request, pk):
        try:
            n = Notification.objects.get(pk=pk)
        except Notification.DoesNotExist:
            return Response({'error': 'Notification not found'}, status=status.HTTP_404_NOT_FOUND)
        n.delete()
        return Response({'message': 'Notification dismissed'}, status=status.HTTP_204_NO_CONTENT)
 
 
class NotificationMarkReadView(APIView):
    """PATCH /api/notifications/{id}/read/ — sets is_read=True, read_at=now."""
 
    def patch(self, request, pk):
        try:
            n = Notification.objects.get(pk=pk)
        except Notification.DoesNotExist:
            return Response({'error': 'Notification not found'}, status=status.HTTP_404_NOT_FOUND)
 
        n.is_read = True
        n.read_at = dj_timezone.now()
        n.save()
 
        return Response({
            'id': n.id, 'is_read': n.is_read, 'read_at': n.read_at,
        })
from django.http import JsonResponse

def api_status(request):
    return JsonResponse({
        "status": "working"
    })