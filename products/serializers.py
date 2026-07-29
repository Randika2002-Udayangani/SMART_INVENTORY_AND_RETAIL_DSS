from rest_framework import serializers
from .models import Brand, Category, StoreZone, Product


class StoreZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = StoreZone
        fields = '__all__'


class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = '__all__'


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
        model = Category
        fields = ['id', 'category_name', 'default_zone', 'default_zone_id']


class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(
        source='category.category_name', read_only=True
    )
    brand_name = serializers.CharField(
        source='brand.brand_name', read_only=True
    )

    class Meta:
        model = Product
        fields = [
            'id', 'product_name', 'sku_code',
            'unit_price', 'cost_price', 'avg_cost_price',
            'reorder_threshold', 'introduced_date', 'is_active',
            'category', 'category_name',
            'brand', 'brand_name',
        ]