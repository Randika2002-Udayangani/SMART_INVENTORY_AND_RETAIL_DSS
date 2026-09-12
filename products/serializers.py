# products/serializers.py
from rest_framework import serializers
from .models import Brand, Category, StoreZone, Product


# ─────────────────────────────────────────────
# StoreZone
# ─────────────────────────────────────────────
class StoreZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model  = StoreZone
        fields = '__all__'


# ─────────────────────────────────────────────
# Brand
# ─────────────────────────────────────────────
class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Brand
        fields = '__all__'


# ─────────────────────────────────────────────
# Category
# ─────────────────────────────────────────────
class CategorySerializer(serializers.ModelSerializer):
    default_zone = StoreZoneSerializer(read_only=True)
    default_zone_id = serializers.PrimaryKeyRelatedField(
        queryset=StoreZone.objects.all(),
        source='default_zone',
        write_only=True,
        required=False,
        allow_null=True
    )

    class Meta:
        model  = Category
        fields = ['id', 'category_name', 'default_zone', 'default_zone_id']


# ─────────────────────────────────────────────
# Product — PUBLIC serializer
# Used by: GET /api/products/  (unauthenticated)
#          GET /api/products/{id}/  (unauthenticated)
#          M3 Chalani customer portal
#
# Intentionally excludes cost_price and avg_cost_price.
# Reason: these are internal business fields.
#   cost_price    — what the store paid the supplier
#   avg_cost_price — WAC used for profit calculations
# Exposing these to customers would reveal store margins.
# API Design Document v3.0: "omits avg_cost_price for
# unauthenticated requests".
#
# Fix: allow_null=True on category_name and brand_name
# Reason: Pipeline 1 (Book1.xlsx) inserts new products
# with category=None and brand=None (R8 rule).
# Without allow_null, the serializer crashes on
# GET /api/products/ for any newly imported product
# until staff assigns a category.
# ─────────────────────────────────────────────
class ProductPublicSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(
        source='category.category_name',
        read_only=True,
        allow_null=True,
        default=None    # returns null instead of crashing for R8 products
    )
    brand_name = serializers.CharField(
        source='brand.brand_name',
        read_only=True,
        allow_null=True,
        default=None    # returns null instead of crashing for unbranked products
    )

    class Meta:
        model  = Product
        fields = [
            'id', 'product_name', 'sku_code',
            'unit_price',               # selling price — safe to show publicly
            'reorder_threshold', 'introduced_date', 'is_active',
            'category', 'category_name',
            'brand', 'brand_name',
            # cost_price    — excluded: internal supplier cost
            # avg_cost_price — excluded: internal WAC used for profit calc
        ]


# ─────────────────────────────────────────────
# Product — STAFF serializer
# Used by: GET /api/products/  (authenticated staff/admin)
#          POST /api/products/  (create)
#          PUT  /api/products/{id}/  (update)
#          Pipeline 1 upload response
#          F05 profit analytics (reads avg_cost_price)
#          F08 health score (reads avg_cost_price)
#          F09 discount engine (reads avg_cost_price)
#
# Includes cost_price and avg_cost_price — staff need
# these for purchase management, profit review, and WAC
# verification after each batch delivery.
# ─────────────────────────────────────────────
class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(
        source='category.category_name',
        read_only=True,
        allow_null=True,
        default=None
    )
    brand_name = serializers.CharField(
        source='brand.brand_name',
        read_only=True,
        allow_null=True,
        default=None
    )

    class Meta:
        model  = Product
        fields = [
            'id', 'product_name', 'sku_code',
            'unit_price', 'cost_price', 'avg_cost_price',  # staff sees all
            'reorder_threshold', 'introduced_date', 'is_active',
            'category', 'category_name',
            'brand', 'brand_name',
        ]

    def to_internal_value(self, data):
        # sku_code is unique=True + null=True + blank=True on the model.
        # Most products have no SKU. If a client sends "" (a blank text
        # input, not an explicit null), the auto-generated UniqueValidator
        # still checks "" for uniqueness — and every other blank-SKU
        # product also has "", so the second save collides and raises an
        # IntegrityError (500) instead of a clean validation error.
        # Normalizing "" -> None here runs before that validator, and
        # multiple NULLs are allowed by the DB unique constraint.
        if hasattr(data, 'copy'):
            data = data.copy()
        if data.get('sku_code', None) == '':
            data['sku_code'] = None
        return super().to_internal_value(data)

# ============================================================
# APPEND to the bottom of products/serializers.py
# ============================================================

from .models import ZoneRecommendation, ProductZoneOverride, ZoneCalculationRun


class ZoneRecommendationSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    # allow_null/default — same reasoning as ProductSerializer.category_name:
    # a product can have no category assigned, and this must not 500.
    category_name = serializers.CharField(
        source='product.category.category_name',
        read_only=True, allow_null=True, default=None
    )
    current_zone_name = serializers.CharField(source='current_zone.zone_name', read_only=True)
    suggested_zone_name = serializers.CharField(source='suggested_zone.zone_name', read_only=True)
    health_scores = serializers.SerializerMethodField()
    updated_by_username = serializers.CharField(
        source='updated_by.username', read_only=True, allow_null=True, default=None
    )

    class Meta:
        model = ZoneRecommendation
        fields = [
            'id', 'product', 'product_name', 'category_name',
            'current_zone', 'current_zone_name',
            'suggested_zone', 'suggested_zone_name',
            'reason', 'performance_score', 'status', 'recommendation_date',
            'health_scores', 'updated_by_username',
        ]

    def get_health_scores(self, obj):
        # Local import — same reasoning as the ZoneRecommendationCalculateView
        # and RecalculateWACView above: avoids a circular import between the
        # products and inventory apps.
        from inventory.models import InventoryHealthScore

        score = (
            InventoryHealthScore.objects
            .filter(product_id=obj.product_id)
            .order_by('-calculated_date', '-calculated_at')
            .first()
        )
        if score is None:
            return None
        return {
            'velocity_score': score.velocity_score,
            'margin_score': score.margin_score,
            'expiry_risk_score': score.expiry_risk_score,
            'overall_score': score.overall_score,
        }


class ZoneRecommendationStatusSerializer(serializers.ModelSerializer):
    """Used only by the accept/reject/apply action endpoint — status is the
    single field a manager is allowed to change on a recommendation."""

    class Meta:
        model = ZoneRecommendation
        fields = ['id', 'status']

    def validate_status(self, value):
        valid = dict(ZoneRecommendation.STATUS_CHOICES)
        if value not in valid:
            raise serializers.ValidationError(f"status must be one of {list(valid)}")
        return value


class ProductZoneOverrideSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    zone_name = serializers.CharField(source='zone.zone_name', read_only=True)

    class Meta:
        model = ProductZoneOverride
        fields = [
            'id', 'product', 'product_name',
            'zone', 'zone_name',
            'start_date', 'end_date', 'reason',
        ]

    def validate(self, data):
        start = data.get('start_date', getattr(self.instance, 'start_date', None))
        end = data.get('end_date', getattr(self.instance, 'end_date', None))
        if end and start and end < start:
            raise serializers.ValidationError("end_date cannot be before start_date")
        return data


class ZoneCalculationRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = ZoneCalculationRun
        fields = [
            'id', 'run_at', 'products_evaluated', 'recommendations_created',
            'skipped_no_health_score', 'skipped_no_current_zone',
            'skipped_duplicate', 'categories_unmapped_count',
        ]