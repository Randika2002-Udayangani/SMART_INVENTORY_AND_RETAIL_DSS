from rest_framework import serializers
from .models import (
    StockLedger, StockAdjustment, InventoryHealthScore,
    CategoryHealthScore, DiscountRule, DiscountRecommendation,
    ReorderRecommendation, LossRecord, SupplierReturn, ProductLifecycle
)
from products.models import Product


class StockLedgerSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(
        source='product.product_name', read_only=True
    )

    class Meta:
        model = StockLedger
        fields = [
            'id', 'product', 'product_name', 'batch',
            'transaction_type', 'source', 'quantity_change',
            'transaction_date', 'reference_id'
        ]


class StockAdjustmentSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(
        source='product.product_name', read_only=True
    )

    class Meta:
        model = StockAdjustment
        fields = [
            'id', 'product', 'product_name', 'batch',
            'quantity_change', 'reason',
            'adjustment_date', 'adjusted_by'
        ]


class CurrentStockSerializer(serializers.Serializer):
    """
    Used for stock snapshot responses.
    Not a ModelSerializer — data is built manually in the view.
    """
    product_id         = serializers.IntegerField()
    product_name       = serializers.CharField()
    sku_code           = serializers.CharField(allow_null=True)
    current_stock      = serializers.IntegerField()
    reorder_threshold  = serializers.IntegerField()
    stock_status       = serializers.CharField()
    avg_cost_price     = serializers.DecimalField(
        max_digits=10, decimal_places=2, allow_null=True
    )
    last_sync_date     = serializers.CharField()