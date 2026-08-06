from rest_framework import serializers
from .models import UploadLog, DailyBillSummary, ItemSalesRecord


class UploadLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = UploadLog
        fields = '__all__'


class DailyBillSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyBillSummary
        fields = '__all__'


class ItemSalesSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(
        source='product.product_name', read_only=True
    )

    class Meta:
        model = ItemSalesRecord
        fields = '__all__'