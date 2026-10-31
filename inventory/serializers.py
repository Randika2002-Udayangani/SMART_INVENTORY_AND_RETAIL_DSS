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


class PublicDiscountRecommendationSerializer(serializers.ModelSerializer):
    product_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = DiscountRecommendation
        fields = [
            'product_id',
            'recommended_discount_pct',
            'recommended_price',
        ]
        read_only_fields = fields


class DiscountRecommendationSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    reviewed_by_username = serializers.CharField(
        source='reviewed_by.username', read_only=True, allow_null=True, default=None
    )

    class Meta:
        model = DiscountRecommendation
        fields = [
            'id', 'product', 'product_name', 'batch',
            'days_until_expiry', 'current_price',
            'recommended_discount_pct', 'recommended_price',
            'profit_protected', 'recovery_sell', 'recovery_return',
            'recovery_discard', 'best_action', 'status',
            'calculated_date', 'reviewed_by', 'reviewed_by_username', 'reviewed_at',
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


class ActiveDiscountSerializer(serializers.ModelSerializer):
    """Customer-safe projection of an approved discount (Today's deals).

    Built ONLY from get_customer_visible_discounts()'s queryset. Deliberately
    excludes every internal discount-engine field: recovery_sell,
    recovery_return, recovery_discard, profit_protected, best_action,
    current_price snapshot, days_until_expiry, status, reviewed_by and
    reviewed_at all stay out of the customer payload.
    """
    product_id = serializers.IntegerField(source='product.id', read_only=True)
    product_name = serializers.CharField(
        source='product.product_name', read_only=True
    )
    category_name = serializers.CharField(
        source='product.category.category_name',
        read_only=True, allow_null=True, default=None,
    )
    brand = serializers.CharField(
        source='product.brand.brand_name',
        read_only=True, allow_null=True, default=None,
    )
    original_price = serializers.DecimalField(
        source='product.unit_price', max_digits=10, decimal_places=2,
        read_only=True,
    )
    discount_percentage = serializers.DecimalField(
        source='recommended_discount_pct', max_digits=5, decimal_places=2,
        read_only=True,
    )
    discounted_price = serializers.DecimalField(
        source='recommended_price', max_digits=10, decimal_places=2,
        read_only=True,
    )
    is_available = serializers.SerializerMethodField()
    expiry_date = serializers.DateField(
        source='batch.expiry_date', read_only=True
    )

    class Meta:
        model = DiscountRecommendation
        fields = [
            'product_id', 'product_name', 'category_name', 'brand',
            'original_price', 'discount_percentage', 'discounted_price',
            'is_available', 'expiry_date',
        ]

    def get_is_available(self, obj):
        # Same public availability definition as the rest of the customer
        # API (_public_product): boolean only, never an exact stock figure.
        from inventory.services.stock import get_available_stock
        return get_available_stock(obj.product_id) > 0
