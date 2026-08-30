from django.contrib.auth import get_user_model
from rest_framework import serializers
from .models import UploadLog, DailyBillSummary, ItemSalesRecord


class UploadLogSerializer(serializers.ModelSerializer):
    # UploadLog.uploaded_by stores a raw user id (IntegerField, not a
    # ForeignKey — see sales/views.py, which sets it via request.user.id),
    # so DRF can't resolve it to a username automatically the way it would
    # for a real FK. This does that lookup manually. uploaded_by itself is
    # kept in the response (not replaced) so nothing already reading the
    # raw id breaks.
    uploaded_by_username = serializers.SerializerMethodField()

    class Meta:
        model = UploadLog
        fields = '__all__'

    def get_uploaded_by_username(self, obj):
        if not obj.uploaded_by:
            return None
        User = get_user_model()
        try:
            return User.objects.get(pk=obj.uploaded_by).username
        except User.DoesNotExist:
            return None


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