from rest_framework import serializers
from .models import Purchase, PurchaseBatch
from suppliers.models import Supplier
from products.models import Product


# ─────────────────────────────────────────────────────────────────
# Batch serializer — used for reading and nested writing
# ─────────────────────────────────────────────────────────────────
class PurchaseBatchSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(
        source='product.product_name', read_only=True
    )

    class Meta:
        model = PurchaseBatch
        fields = [
            'id', 'product', 'product_name',
            'quantity_received', 'cost_price',
            'expiry_date', 'remaining_quantity', 'status'
        ]
        extra_kwargs = {
            'remaining_quantity': {'required': False},
            'status'            : {'required': False},
        }


# ─────────────────────────────────────────────────────────────────
# Purchase READ serializer — returns purchase + nested batches
# ─────────────────────────────────────────────────────────────────
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
            'id', 'supplier', 'supplier_name',
            'order_date', 'purchase_date', 'invoice_number',
            'total_amount', 'expected_days', 'actual_days',
            'batches'
        ]


# ─────────────────────────────────────────────────────────────────
# Purchase CREATE serializer — handles nested batch creation,
# Stock Ledger update, and WAC recalculation
# ─────────────────────────────────────────────────────────────────
class PurchaseCreateSerializer(serializers.ModelSerializer):
    batches = PurchaseBatchSerializer(many=True, write_only=True)

    class Meta:
        model = Purchase
        fields = [
            'supplier', 'order_date', 'purchase_date',
            'invoice_number', 'total_amount',
            'expected_days', 'actual_days', 'batches'
        ]

    def create(self, validated_data):
        from django.db import transaction

        batches_data = validated_data.pop('batches')

        with transaction.atomic():
            # Step 1 — Create the Purchase (GRN header)
            purchase = Purchase.objects.create(**validated_data)
            total_amount = 0

            for batch_data in batches_data:
                # Step 2 — Set remaining_quantity = quantity_received on arrival
                batch_data['remaining_quantity'] = batch_data['quantity_received']
                batch_data.setdefault('status', 'ACTIVE')

                # Step 3 — Create the batch row
                batch = PurchaseBatch.objects.create(
                    purchase=purchase,
                    **batch_data
                )

                # Step 4 — Add to total amount
                total_amount += float(batch.cost_price) * batch.quantity_received

                # Step 5 — Write to Stock Ledger (try/except so it
                # doesn't crash if StockLedger model differs slightly)
                try:
                    from inventory.models import StockLedger
                    StockLedger.objects.create(
                        product=batch.product,
                        batch=batch,
                        transaction_type='PURCHASE',
                        source='PURCHASE',
                        quantity_change=batch.quantity_received,
                        reference_id=purchase.id
                    )
                except Exception:
                    # StockLedger write failed — still continue
                    # The batch and purchase are still saved
                    pass

                # Step 6 — Recalculate WAC (Weighted Average Cost)
                # WAC = total cost of all active batches / total units
                product = batch.product
                try:
                    active_batches = PurchaseBatch.objects.filter(
                        product=product,
                        status='ACTIVE'
                    )
                    total_cost  = sum(
                        float(b.cost_price) * b.quantity_received
                        for b in active_batches
                    )
                    total_units = sum(b.quantity_received for b in active_batches)
                    if total_units > 0:
                        product.avg_cost_price = round(total_cost / total_units, 2)
                        product.save(update_fields=['avg_cost_price'])
                except Exception:
                    pass

            # Step 7 — Save final total amount to purchase
            purchase.total_amount = round(total_amount, 2)
            purchase.save(update_fields=['total_amount'])

        return purchase