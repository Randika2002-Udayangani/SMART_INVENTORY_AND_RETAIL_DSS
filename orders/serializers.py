from rest_framework import serializers
from django.contrib.auth.hashers import make_password
from products.models import Product

from .models import (
    Customer,
    OnlineOrder,
    OnlineOrderItem,
    ChatbotLog,
    ProductRating,
    ProductRatingSummary,
    Notification,
)



class CustomerRegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        min_length=6
    )

    class Meta:
        model = Customer
        fields = [
            "id",
            "name",
            "email",
            "contact_number",
            "address",
            "password",
        ]

        read_only_fields = ["id"]

    def create(self, validated_data):

        password = validated_data.pop("password")

        customer = Customer.objects.create(
            password_hash=make_password(password),
            **validated_data
        )

        return customer


class CustomerLoginSerializer(serializers.Serializer):

    email = serializers.EmailField()

    password = serializers.CharField(
        write_only=True
    )


class CustomerSerializer(serializers.ModelSerializer):

    class Meta:
        model = Customer

        fields = [
            "id",
            "name",
            "email",
            "contact_number",
            "address",
            "created_at",
            "last_login",
        ]

        read_only_fields = [
            "id",
            "created_at",
            "last_login",
        ]


class OnlineOrderItemSerializer(serializers.ModelSerializer):

    product_name = serializers.CharField(
        source="product.product_name",
        read_only=True
    )

    class Meta:
        model = OnlineOrderItem

        fields = [
            "id",
            "product",
            "product_name",
            "quantity",
            "unit_price",
            "is_reserved",
            "reserved_at",
        ]

class OnlineOrderSerializer(serializers.ModelSerializer):

    customer_name = serializers.CharField(
        source="customer.name",
        read_only=True
    )

    items = OnlineOrderItemSerializer(
        source="onlineorderitem_set",
        many=True,
        read_only=True,
    )

    class Meta:
        model = OnlineOrder

        fields = [
            "id",
            "customer",
            "customer_name",
            "order_reference",
            "order_date",
            "pickup_date",
            "pickup_time_slot",
            "collection_deadline",
            "status",
            "cancel_reason",
            "cancelled_by",
            "total_amount",
            "payment_status",
            "notes",
            "confirmed_by",
            "created_at",
            "items",
        ]

        read_only_fields = [
            "id",
            "order_reference",
            "order_date",
            "created_at",
        ]


class OrderStatusSerializer(serializers.Serializer):

    status = serializers.ChoiceField(
        choices=[
            "PENDING",
            "CONFIRMED",
            "READY",
            "COMPLETED",
            "CANCELLED",
            "EXPIRED",
        ]
    )



class ChatbotLogSerializer(serializers.ModelSerializer):

    class Meta:
        model = ChatbotLog
        fields = "__all__"


class ProductRatingSerializer(serializers.ModelSerializer):

    class Meta:
        model = ProductRating
        fields = "__all__"


class ProductRatingSummarySerializer(serializers.ModelSerializer):

    class Meta:
        model = ProductRatingSummary
        fields = "__all__"



class NotificationSerializer(serializers.ModelSerializer):

    class Meta:
        model = Notification
        fields = "__all__"




class RatingCreateSerializer(serializers.Serializer):
    """
    Validates the POST /api/ratings/ body. A plain Serializer
    (not ModelSerializer) on purpose — customer and is_verified
    are set server-side in the view, never taken from the request
    body, so they're deliberately excluded here.
    """
    product = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.filter(is_active=True)
    )
    rating = serializers.IntegerField(min_value=1, max_value=5)
    feedback_text = serializers.CharField(
        max_length=500, required=False, allow_blank=True
    )


class ProductRatingPublicSerializer(serializers.ModelSerializer):
    """
    Public-facing read serializer for GET /api/ratings/product/{id}/.
    Deliberately omits `customer` — see the note in
    ProductRatingListView about the "internal only" vs "public
    endpoint" contradiction in the API doc.
    """
    class Meta:
        model = ProductRating
        fields = ["id", "rating", "feedback_text", "is_verified", "created_at"]
