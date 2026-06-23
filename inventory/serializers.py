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

# ============================================================
# APPEND to inventory/serializers.py
# (DiscountRule and DiscountRecommendation are already imported
# at the top of this file — no new model imports needed.)
# ============================================================

class DiscountRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiscountRule
        fields = [
            'id', 'days_from_expiry_min', 'days_from_expiry_max',
            'discount_percentage', 'minimum_margin_pct',
            'is_active', 'created_by', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class DiscountRecommendationSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.product_name', read_only=True)

    class Meta:
        model = DiscountRecommendation
        fields = [
            'id', 'product', 'product_name', 'batch',
            'days_until_expiry', 'current_price',
            'recommended_discount_pct', 'recommended_price',
            'profit_protected', 'recovery_sell', 'recovery_return',
            'recovery_discard', 'best_action', 'status',
            'calculated_date', 'reviewed_by', 'reviewed_at',
        ]
        read_only_fields = [
            'id', 'product', 'product_name', 'batch',
            'days_until_expiry', 'current_price',
            'recommended_discount_pct', 'recommended_price',
            'profit_protected', 'recovery_sell', 'recovery_return',
            'recovery_discard', 'best_action', 'calculated_date',
        ]

class ReorderRecommendationSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    supplier_name = serializers.CharField(source='supplier.supplier_name', read_only=True, allow_null=True, default=None)
 
    class Meta:
        model = ReorderRecommendation
        fields = [
            'id', 'product', 'product_name', 'supplier', 'supplier_name',
            'current_stock', 'avg_daily_sales', 'days_of_stock',
            'safety_stock', 'suggested_quantity', 'estimated_cost',
            'urgency', 'calculation_date', 'status', 'actioned_by',
        ]
        read_only_fields = [
            'id', 'product', 'product_name', 'supplier', 'supplier_name',
            'current_stock', 'avg_daily_sales', 'days_of_stock',
            'safety_stock', 'suggested_quantity', 'estimated_cost',
            'urgency', 'calculation_date',
        ]
 
