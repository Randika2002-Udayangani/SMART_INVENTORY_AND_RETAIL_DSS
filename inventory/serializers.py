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
    class Meta:
        model = StockAdjustment
        fields = '__all__'


class CurrentStockSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    sku_code = serializers.CharField()
    current_stock = serializers.IntegerField()
    reorder_threshold = serializers.IntegerField()
    stock_status = serializers.CharField()
    last_sync_date = serializers.DateField()