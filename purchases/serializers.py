from rest_framework import serializers
from .models import Purchase, PurchaseBatch
from suppliers.models import Supplier
from products.models import Product


class PurchaseBatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseBatch
        fields = [
            'id', 'product', 'quantity_received', 'cost_price',
            'expiry_date', 'remaining_quantity', 'status'
        ]
        extra_kwargs = {
            'remaining_quantity': {'required': False, 'read_only': False},
            'status': {'required': False},
        }

class PurchaseSerializer(serializers.ModelSerializer):
    batches = PurchaseBatchSerializer(
        many=True,
        source='purchasebatch_set',
        read_only=True
    )
    supplier_name = serializers.CharField(
        source='supplier.supplier_name', read_only=True
    )

    class Meta:
        model = Purchase
        fields = [
            'id', 'supplier', 'supplier_name', 'order_date',
            'purchase_date', 'invoice_number', 'total_amount',
            'expected_days', 'actual_days', 'batches'
        ]


class PurchaseCreateSerializer(serializers.ModelSerializer):
    batches = PurchaseBatchSerializer(many=True, write_only=True)

    class Meta:
        model = Purchase
        fields = [
            'supplier', 'order_date', 'purchase_date',
            'invoice_number', 'total_amount', 'expected_days',
            'actual_days', 'batches'
        ]

    def create(self, validated_data):
        from inventory.models import StockLedger
        from django.db import transaction

        batches_data = validated_data.pop('batches')

        with transaction.atomic():
            purchase = Purchase.objects.create(**validated_data)
            total = 0

            for batch_data in batches_data:
                batch_data['remaining_quantity'] = batch_data['quantity_received']
                batch = PurchaseBatch.objects.create(
                    purchase=purchase, **batch_data
                )
                total += batch.cost_price * batch.quantity_received

                # Auto update Stock Ledger
                StockLedger.objects.create(
                    product=batch.product,
                    batch=batch,
                    transaction_type='PURCHASE',
                    source='PURCHASE',
                    quantity_change=batch.quantity_received,
                    reference_id=purchase.id
                )

                # Recalculate WAC for this product
                product = batch.product
                all_batches = PurchaseBatch.objects.filter(
                    product=product,
                    status='ACTIVE'
                )
                total_cost = sum(
                    b.cost_price * b.quantity_received for b in all_batches
                )
                total_units = sum(b.quantity_received for b in all_batches)
                if total_units > 0:
                    product.avg_cost_price = total_cost / total_units
                    product.save()

            purchase.total_amount = total
            purchase.save()

        return purchase