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