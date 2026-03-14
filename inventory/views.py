from datetime import date, timedelta

from django.db.models import Sum
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from products.models import Product
from purchases.models import PurchaseBatch
from suppliers.models import Supplier
from users.models import SystemConfig
from .models import (
    StockLedger, StockAdjustment, ProductLifecycle,
    LossRecord, SupplierReturn
)
from .serializers import (
    StockLedgerSerializer, StockAdjustmentSerializer, CurrentStockSerializer
)


def get_last_sync_date():
    """Helper — reads last_item_ledger_sync from SystemConfig."""
    try:
        config = SystemConfig.objects.get(key='last_item_ledger_sync')
        return config.value
    except SystemConfig.DoesNotExist:
        return 'Not synced yet'


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

        new_qty = batch.remaining_quantity + int(quantity_change)
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

        return Response({
            'message'               : 'Stock adjusted successfully',
            'product'               : product.product_name,
            'quantity_change'       : quantity_change,
            'new_remaining_quantity': new_qty,
            'batch_id'              : batch.id,
            'batch_status'          : batch.status,
            'adjustment_id'         : adjustment.id
        }, status=status.HTTP_201_CREATED)


class LowStockView(APIView):
    def get(self, request):
        products  = Product.objects.filter(is_active=True)
        low_stock = []

        for product in products:
            current = PurchaseBatch.objects.filter(
                product=product, status='ACTIVE'
            ).aggregate(total=Sum('remaining_quantity'))['total'] or 0

            reorder = product.reorder_threshold or 0
            if current <= reorder:
                low_stock.append({
                    'product_id'       : product.id,
                    'product_name'     : product.product_name,
                    'sku_code'         : product.sku_code,
                    'current_stock'    : current,
                    'reorder_threshold': reorder,
                    'units_short'      : reorder - current,
                })

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
    def post(self, request):
        from sales.models import ItemSalesRecord

        today     = date.today()
        products  = Product.objects.filter(is_active=True)
        processed = 0

        for product in products:
            if product.introduced_date and \
               product.introduced_date > today - timedelta(days=30):
                status_val     = 'NEW'
                recommendation = 'MONITOR'
                current_vel    = 0.0

            else:
                current_qty = ItemSalesRecord.objects.filter(
                    product=product,
                    sale_date__gte=today - timedelta(days=30)
                ).aggregate(total=Sum('quantity_sold'))['total'] or 0

                hist_qty = ItemSalesRecord.objects.filter(
                    product=product,
                    sale_date__gte=today - timedelta(days=120),
                    sale_date__lt=today - timedelta(days=30)
                ).aggregate(total=Sum('quantity_sold'))['total'] or 0

                current_vel = current_qty / 30
                hist_vel    = hist_qty / 90 if hist_qty else None

                units_60 = ItemSalesRecord.objects.filter(
                    product=product,
                    sale_date__gte=today - timedelta(days=60)
                ).aggregate(total=Sum('quantity_sold'))['total'] or 0

                if units_60 < 5:
                    status_val     = 'SLOW_MOVING'
                    recommendation = 'DISCONTINUE'
                elif hist_vel is None:
                    status_val     = 'STABLE'
                    recommendation = 'RETAIN'
                elif current_vel > hist_vel * 1.15:
                    status_val     = 'GROWING'
                    recommendation = 'RETAIN'
                elif current_vel < hist_vel * 0.85:
                    status_val     = 'DECLINING'
                    recommendation = 'DISCOUNT'
                else:
                    status_val     = 'STABLE'
                    recommendation = 'RETAIN'

            ProductLifecycle.objects.update_or_create(
                product           = product,
                comparison_period = today.strftime('%Y-%m-%d'),
                defaults={
                    'status'        : status_val,
                    'recommendation': recommendation,
                    'sales_velocity': round(current_vel, 4),
                }
            )
            processed += 1

        return Response({
            'message'           : 'Lifecycle calculation complete',
            'products_processed': processed,
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
            'id', 'product', 'batch', 'loss_type',
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

        ret.status = new_status
        ret.save()

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
    

    # ═════════════════════════════════════════════════════════════════
# F08 — Inventory Health Score
# Paste this block at the BOTTOM of inventory/views.py
# ═════════════════════════════════════════════════════════════════

from .models import InventoryHealthScore, CategoryHealthScore
from products.models import Category


# ─────────────────────────────────────────────────────────────────
# POST /api/health-scores/calculate/
# ─────────────────────────────────────────────────────────────────
class HealthScoreCalculateView(APIView):
    """
    Triggers full health score recalculation for all active products.
    Delegates to services.calculate_health_scores() for logic.
    Also calculates Category_Health_Score aggregates.
    """

    def post(self, request):
        from .services import calculate_health_scores
        result = calculate_health_scores()
        return Response({
            'message'           : 'Health score calculation complete',
            'products_processed': result['products_processed'],
            'summary'           : result['summary'],
        }, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────
# GET /api/health-scores/
# Filter by ?status=CRITICAL|AT RISK|WATCH|HEALTHY
# ─────────────────────────────────────────────────────────────────
class HealthScoreListView(APIView):

    def get(self, request):
        queryset      = InventoryHealthScore.objects.all().order_by(
            '-calculated_date', 'overall_score'
        )
        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        data = queryset.values(
            'id', 'product', 'velocity_score', 'margin_score',
            'expiry_risk_score', 'stock_duration_score', 'rating_score',
            'overall_score', 'status', 'recommended_action',
            'rating_sufficient', 'weighting_mode', 'calculated_date'
        )
        return Response(list(data))


# ─────────────────────────────────────────────────────────────────
# GET /api/health-scores/categories/
# ⚠ Must be registered BEFORE health-scores/<int:product_id>/
# ─────────────────────────────────────────────────────────────────
class CategoryHealthScoreView(APIView):

    def get(self, request):
        queryset = CategoryHealthScore.objects.all().order_by(
            '-calculated_date', 'avg_health_score'
        )
        data = queryset.values(
            'id', 'category', 'avg_health_score',
            'healthy_count', 'watch_count', 'at_risk_count',
            'critical_count', 'status', 'calculated_date'
        )
        return Response(list(data))


# ─────────────────────────────────────────────────────────────────
# GET /api/health-scores/critical/
# ⚠ Must be registered BEFORE health-scores/<int:product_id>/
# ─────────────────────────────────────────────────────────────────
class HealthScoreCriticalView(APIView):

    def get(self, request):
        queryset = InventoryHealthScore.objects.filter(
            status='CRITICAL'
        ).order_by('-calculated_date', 'overall_score')
        data = queryset.values(
            'id', 'product', 'overall_score', 'status',
            'recommended_action', 'calculated_date'
        )
        return Response(list(data))


# ─────────────────────────────────────────────────────────────────
# GET /api/health-scores/<product_id>/
# Full health score history for one product across all runs
# ─────────────────────────────────────────────────────────────────
class HealthScoreDetailView(APIView):

    def get(self, request, product_id):
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'},
                            status=status.HTTP_404_NOT_FOUND)

        queryset = InventoryHealthScore.objects.filter(
            product=product
        ).order_by('-calculated_date')

        data = queryset.values(
            'id', 'product', 'velocity_score', 'margin_score',
            'expiry_risk_score', 'stock_duration_score', 'rating_score',
            'overall_score', 'status', 'recommended_action',
            'rating_sufficient', 'weighting_mode', 'calculated_date'
        )
        return Response(list(data))