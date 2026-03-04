from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Sum
from django.utils import timezone
from products.models import Product
from users.models import SystemConfig
from .models import (
    StockLedger, StockAdjustment, PurchaseBatch
)
from .serializers import (
    StockLedgerSerializer, StockAdjustmentSerializer, CurrentStockSerializer
)


def get_last_sync_date():
    try:
        config = SystemConfig.objects.get(key='last_sync_date')
        return config.value
    except SystemConfig.DoesNotExist:
        return 'Not synced yet'


class StockSnapshotView(APIView):
    """GET /api/inventory/stock/ — all products with current stock level"""

    def get(self, request):
        last_sync = get_last_sync_date()
        products = Product.objects.filter(is_active=True)
        result = []

        for product in products:
            current_stock = PurchaseBatch.objects.filter(
                product=product,
                status='ACTIVE'
            ).aggregate(total=Sum('remaining_quantity'))['total'] or 0

            if current_stock == 0:
                stock_status = 'OUT OF STOCK'
            elif current_stock <= product.reorder_threshold:
                stock_status = 'LOW STOCK'
            else:
                stock_status = 'AVAILABLE'

            result.append({
                'product_id': product.id,
                'product_name': product.product_name,
                'sku_code': product.sku_code,
                'current_stock': current_stock,
                'reorder_threshold': product.reorder_threshold,
                'stock_status': stock_status,
                'last_sync_date': last_sync,
            })

        return Response({
            'last_sync_date': last_sync,
            'note': 'Stock is snapshot-based — not real-time.',
            'count': len(result),
            'stock': result
        })


class ProductStockDetailView(APIView):
    """GET /api/inventory/stock/{product_id}/ — one product stock breakdown"""

    def get(self, request, product_id):
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return Response(
                {'error': 'Product not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        batches = PurchaseBatch.objects.filter(
            product=product, status='ACTIVE'
        ).order_by('expiry_date')

        total_stock = batches.aggregate(
            total=Sum('remaining_quantity')
        )['total'] or 0

        batch_data = []
        for b in batches:
            batch_data.append({
                'batch_id': b.id,
                'remaining_quantity': b.remaining_quantity,
                'cost_price': str(b.cost_price),
                'expiry_date': str(b.expiry_date) if b.expiry_date else None,
                'status': b.status,
            })

        return Response({
            'product_id': product.id,
            'product_name': product.product_name,
            'sku_code': product.sku_code,
            'total_current_stock': total_stock,
            'last_sync_date': get_last_sync_date(),
            'batches': batch_data
        })


class StockLedgerView(generics.ListAPIView):
    """GET /api/inventory/ledger/ — full stock movement history"""
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


class StockAdjustmentView(APIView):
    """POST /api/inventory/adjust/ — manual stock correction"""

    def post(self, request):
        product_id = request.data.get('product_id')
        quantity_change = request.data.get('quantity_change')
        reason = request.data.get('reason', '')

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

        # Apply to oldest active batch
        batch = PurchaseBatch.objects.filter(
            product=product, status='ACTIVE'
        ).order_by('expiry_date').first()

        if batch is None:
            return Response(
                {'error': 'No active batch found for this product'},
                status=status.HTTP_400_BAD_REQUEST
            )

        new_qty = batch.remaining_quantity + int(quantity_change)
        if new_qty < 0:
            return Response(
                {'error': 'Adjustment would make stock negative. Not allowed.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        batch.remaining_quantity = new_qty
        if new_qty == 0:
            batch.status = 'DEPLETED'
        batch.save()

        # Create StockLedger entry
        StockLedger.objects.create(
            product=product,
            batch=batch,
            transaction_type='MANUAL_ADJUSTMENT',
            source='MANUAL_ADJUSTMENT',
            quantity_change=int(quantity_change),
        )

        # Create StockAdjustment record
        adjustment = StockAdjustment.objects.create(
            product=product,
            batch=batch,
            quantity_change=int(quantity_change),
            reason=reason,
        )

        return Response({
            'message': 'Stock adjusted successfully',
            'product': product.product_name,
            'quantity_change': quantity_change,
            'new_remaining_quantity': new_qty,
            'batch_id': batch.id,
            'adjustment_id': adjustment.id
        }, status=status.HTTP_201_CREATED)


class LowStockView(APIView):
    """GET /api/inventory/low-stock/"""

    def get(self, request):
        products = Product.objects.filter(is_active=True)
        low_stock = []
        for product in products:
            current = PurchaseBatch.objects.filter(
                product=product, status='ACTIVE'
            ).aggregate(total=Sum('remaining_quantity'))['total'] or 0
            if current <= product.reorder_threshold:
                low_stock.append({
                    'product_id': product.id,
                    'product_name': product.product_name,
                    'current_stock': current,
                    'reorder_threshold': product.reorder_threshold,
                })
        return Response({'count': len(low_stock), 'low_stock_products': low_stock})


class OutOfStockView(APIView):
    """GET /api/inventory/out-of-stock/"""

    def get(self, request):
        products = Product.objects.filter(is_active=True)
        out = []
        for product in products:
            current = PurchaseBatch.objects.filter(
                product=product, status='ACTIVE'
            ).aggregate(total=Sum('remaining_quantity'))['total'] or 0
            if current == 0:
                out.append({
                    'product_id': product.id,
                    'product_name': product.product_name,
                    'sku_code': product.sku_code,
                })
        return Response({'count': len(out), 'out_of_stock': out})