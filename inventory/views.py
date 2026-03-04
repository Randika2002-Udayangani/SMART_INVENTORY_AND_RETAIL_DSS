from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Sum
from django.utils import timezone
from products.models import Product
from users.models import SystemConfig
from .models import StockLedger, StockAdjustment
from purchases.models import PurchaseBatch
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


# ─────────────────────────────────────────────────────────────────
# GET /api/inventory/stock/
# Returns all active products with current stock level and status
# ─────────────────────────────────────────────────────────────────
class StockSnapshotView(APIView):
    """
    Returns a stock snapshot for all active products.
    Stock is calculated from sum of remaining_quantity on ACTIVE batches.
    """

    def get(self, request):
        last_sync = get_last_sync_date()
        products = Product.objects.filter(is_active=True)
        result = []

        for product in products:
            current_stock = PurchaseBatch.objects.filter(
                product=product,
                status='ACTIVE'
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
            'note'          : 'Stock is snapshot-based — purchases tracked, sales estimated from ledger.',
            'count'         : len(result),
            'stock'         : result
        })


# ─────────────────────────────────────────────────────────────────
# GET /api/inventory/stock/<product_id>/
# Returns one product with full batch breakdown
# ─────────────────────────────────────────────────────────────────
class ProductStockDetailView(APIView):
    """
    Returns detailed stock info for one product.
    Includes all ACTIVE batches sorted by expiry date (FEFO order).
    """

    def get(self, request, product_id):
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response(
                {'error': 'Product not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        batches = PurchaseBatch.objects.filter(
            product=product,
            status='ACTIVE'
        ).order_by('expiry_date')

        total_stock = batches.aggregate(
            total=Sum('remaining_quantity')
        )['total'] or 0

        batch_data = []
        for b in batches:
            batch_data.append({
                'batch_id'          : b.id,
                'remaining_quantity': b.remaining_quantity,
                'quantity_received' : b.quantity_received,
                'cost_price'        : str(b.cost_price),
                'expiry_date'       : str(b.expiry_date) if b.expiry_date else None,
                'status'            : b.status,
            })

        reorder = product.reorder_threshold or 0
        if total_stock == 0:
            stock_status = 'OUT OF STOCK'
        elif total_stock <= reorder:
            stock_status = 'LOW STOCK'
        else:
            stock_status = 'AVAILABLE'

        return Response({
            'product_id'        : product.id,
            'product_name'      : product.product_name,
            'sku_code'          : product.sku_code,
            'avg_cost_price'    : str(product.avg_cost_price),
            'total_current_stock': total_stock,
            'reorder_threshold' : reorder,
            'stock_status'      : stock_status,
            'last_sync_date'    : get_last_sync_date(),
            'active_batch_count': len(batch_data),
            'batches'           : batch_data
        })


# ─────────────────────────────────────────────────────────────────
# GET /api/inventory/ledger/
# Full stock movement history — filter by ?product=<id> &type=PURCHASE
# ─────────────────────────────────────────────────────────────────
class StockLedgerView(generics.ListAPIView):
    """
    Returns all stock ledger entries (movements).
    Filter: ?product=<id>  ?type=PURCHASE|SALE_SYNC|MANUAL_ADJUSTMENT
    """
    serializer_class = StockLedgerSerializer

    def get_queryset(self):
        queryset = StockLedger.objects.all().order_by('-transaction_date')
        product = self.request.query_params.get('product')
        transaction_type = self.request.query_params.get('type')
        if product:
            queryset = queryset.filter(product__id=product)
        if transaction_type:
            queryset = queryset.filter(transaction_type=transaction_type)
        return queryset


# ─────────────────────────────────────────────────────────────────
# POST /api/inventory/adjust/
# Manual stock correction — applies to oldest active batch (FEFO)
# Body: { "product_id": 1, "quantity_change": -5, "reason": "Damaged" }
# ─────────────────────────────────────────────────────────────────
class StockAdjustmentView(APIView):
    """
    Manually adjust stock for a product.
    quantity_change can be positive (add) or negative (remove).
    Applies to the oldest active batch (First Expiry First Out).
    """

    def post(self, request):
        product_id    = request.data.get('product_id')
        quantity_change = request.data.get('quantity_change')
        reason        = request.data.get('reason', '')

        # Validate required fields
        if not product_id or quantity_change is None:
            return Response(
                {'error': 'product_id and quantity_change are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response(
                {'error': 'Product not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Apply to the oldest active batch (FEFO)
        batch = PurchaseBatch.objects.filter(
            product=product,
            status='ACTIVE'
        ).order_by('expiry_date').first()

        if batch is None:
            return Response(
                {'error': 'No active batch found for this product. '
                          'Create a purchase first.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        new_qty = batch.remaining_quantity + int(quantity_change)
        if new_qty < 0:
            return Response(
                {
                    'error': f'Adjustment would make stock negative. '
                             f'Current batch has {batch.remaining_quantity} units. '
                             f'You tried to remove {abs(int(quantity_change))}.'
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        # Update batch
        batch.remaining_quantity = new_qty
        if new_qty == 0:
            batch.status = 'DEPLETED'
        batch.save()

        # Write to StockLedger
        StockLedger.objects.create(
            product=product,
            batch=batch,
            transaction_type='MANUAL_ADJUSTMENT',
            source='MANUAL_ADJUSTMENT',
            quantity_change=int(quantity_change),
        )

        # Write to StockAdjustment log
        adjustment = StockAdjustment.objects.create(
            product=product,
            batch=batch,
            quantity_change=int(quantity_change),
            reason=reason,
        )

        return Response({
            'message'              : 'Stock adjusted successfully',
            'product'              : product.product_name,
            'quantity_change'      : quantity_change,
            'new_remaining_quantity': new_qty,
            'batch_id'             : batch.id,
            'batch_status'         : batch.status,
            'adjustment_id'        : adjustment.id
        }, status=status.HTTP_201_CREATED)


# ─────────────────────────────────────────────────────────────────
# GET /api/inventory/low-stock/
# Products at or below their reorder threshold
# ─────────────────────────────────────────────────────────────────
class LowStockView(APIView):
    """Returns all active products where current_stock <= reorder_threshold."""

    def get(self, request):
        products = Product.objects.filter(is_active=True)
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
            'count'              : len(low_stock),
            'low_stock_products' : low_stock
        })


# ─────────────────────────────────────────────────────────────────
# GET /api/inventory/out-of-stock/
# Products with zero stock
# ─────────────────────────────────────────────────────────────────
class OutOfStockView(APIView):
    """Returns all active products with zero remaining stock."""

    def get(self, request):
        products = Product.objects.filter(is_active=True)
        out = []

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

        return Response({
            'count'        : len(out),
            'out_of_stock' : out
        })