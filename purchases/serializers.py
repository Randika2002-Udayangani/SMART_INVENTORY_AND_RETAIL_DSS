import logging
from decimal import Decimal
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .models import Purchase, PurchaseBatch
from suppliers.models import Supplier
from products.models import Product

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Batch serializer — used for reading and nested writing
# ─────────────────────────────────────────────────────────────────
class PurchaseBatchSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(
        source='product.product_name', read_only=True
    )
    invoice_number = serializers.CharField(
        source='purchase.invoice_number', read_only=True, allow_null=True, default=None
    )

    class Meta:
        model  = PurchaseBatch
        fields = [
            'id', 'product', 'product_name',
            'quantity_received', 'cost_price',
            'expiry_date', 'remaining_quantity', 'status',
            'purchase', 'invoice_number',
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
        model  = Purchase
        fields = [
            'id', 'supplier', 'supplier_name',
            'order_date', 'purchase_date', 'invoice_number',
            'total_amount', 'expected_days', 'actual_days',
            'batches'
        ]


# ─────────────────────────────────────────────────────────────────
# Purchase CREATE serializer — handles nested batch creation,
# Stock Ledger update, and WAC recalculation
#
# Fixes applied (Nipuni — with M1 permission):
#   Fix 1  — F02-A: Supplier validation (lead_time_days is None + contact)
#   Fix 2  — F02-B: Duplicate invoice check per supplier
#   Fix 3  — F02-C: Empty batches list rejected
#   Fix 4  — F02-C: qty > 0 validation on each batch
#   Fix 5  — F02-C: cost_price > 0 validation using Decimal (no float precision loss)
#   Fix 6  — F02-C: expiry_date required + must be future + 7-day warning
#   Fix 7  — F02-D: WAC uses remaining_quantity not quantity_received
#   Fix 8  — WAC recalculated once per unique product after all batches saved
#   Fix 9  — logger imported once at module level not inside loops
#   Fix 10 — Decimal arithmetic throughout, no float() precision loss
# ─────────────────────────────────────────────────────────────────
class PurchaseCreateSerializer(serializers.ModelSerializer):
    batches = PurchaseBatchSerializer(many=True, write_only=True)

    class Meta:
        model  = Purchase
        fields = [
            'supplier', 'order_date', 'purchase_date',
            'invoice_number', 'total_amount',
            'expected_days', 'actual_days', 'batches'
        ]

    # ── Fix 1: F02-A — Supplier validation ───────────────────────
    # Fix: use `is None` not `not supplier.lead_time_days`
    # Reason: lead_time_days = 0 is falsy in Python but is a valid value
    # `not 0` → True → would incorrectly reject a supplier with 0 lead days
    # `0 is None` → False → correctly accepts it
    def validate_supplier(self, supplier):
        if not supplier:
            raise serializers.ValidationError(
                'Supplier not found. Create supplier record first.'
            )
        if supplier.lead_time_days is None:
            raise serializers.ValidationError(
                f'Supplier "{supplier.supplier_name}" has no lead_time_days set. '
                f'Update supplier record before recording a purchase.'
            )
        if not supplier.email and not supplier.contact_number:
            raise serializers.ValidationError(
                f'Supplier "{supplier.supplier_name}" has no email or contact number. '
                f'At least one contact method is required.'
            )
        return supplier

    # ── Fix 2: F02-B — Duplicate invoice + batch validations ─────
    def validate(self, data):
        supplier       = data.get('supplier')
        invoice_number = data.get('invoice_number')

        # Duplicate invoice check
        if supplier and invoice_number:
            if Purchase.objects.filter(
                supplier=supplier,
                invoice_number=invoice_number
            ).exists():
                raise serializers.ValidationError({
                    'invoice_number': (
                        f'Invoice "{invoice_number}" already exists for supplier '
                        f'"{supplier.supplier_name}". '
                        f'Duplicate invoices are not allowed.'
                    )
                })

        batches = data.get('batches', [])

        # ── Fix 3: Empty batches guard ────────────────────────────
        # A purchase with no batches is logically meaningless —
        # no stock would be added, no WAC updated, no ledger entry
        if not batches:
            raise serializers.ValidationError({
                'batches': 'At least one batch must be provided.'
            })

        today    = timezone.now().date()
        warnings = []

        for i, batch in enumerate(batches):
            product = batch.get('product')
            label   = f'Batch {i + 1} (product: {product})'

            # ── Fix 4: qty > 0 ────────────────────────────────────
            qty = batch.get('quantity_received', 0)
            if not qty or qty <= 0:
                raise serializers.ValidationError({
                    'batches': f'{label}: quantity_received must be greater than 0.'
                })

            # ── Fix 5: cost_price > 0 using Decimal ───────────────
            # float(cost_price) risks precision loss on DecimalField values
            # e.g. float(Decimal('380.00')) can introduce floating point error
            # Use Decimal comparison throughout for financial accuracy
            cost_price = batch.get('cost_price')
            try:
                if Decimal(str(cost_price)) <= Decimal('0'):
                    raise serializers.ValidationError({
                        'batches': f'{label}: cost_price must be greater than 0.'
                    })
            except (TypeError, ValueError):
                raise serializers.ValidationError({
                    'batches': f'{label}: cost_price is not a valid number.'
                })

            # ── Fix 6: expiry_date required + future check ─────────
            # If expiry_date is None and model allows null, batch would be
            # saved without expiry — F07 and F09 cannot process it correctly
            # Explicit required check prevents silent null expiry batches
            expiry_date = batch.get('expiry_date')
            if not expiry_date:
                raise serializers.ValidationError({
                    'batches': f'{label}: expiry_date is required.'
                })
            if expiry_date <= today:
                raise serializers.ValidationError({
                    'batches': (
                        f'{label}: expiry_date {expiry_date} is today or in the past. '
                        f'Batch rejected — expiry must be a future date.'
                    )
                })
            if expiry_date <= today + timedelta(days=7):
                warnings.append(
                    f'{label}: expiry_date {expiry_date} is within 7 days. '
                    f'Batch accepted but nearly expired on arrival.'
                )

        if warnings:
            self.context['warnings'] = warnings

        return data

    def create(self, validated_data):
        batches_data = validated_data.pop('batches')

        with transaction.atomic():
            # Step 1 — Create the Purchase (GRN header)
            purchase     = Purchase.objects.create(**validated_data)
            total_amount = Decimal('0.00')

            # ── Fix 8: track affected products for WAC ────────────
            # Recalculate WAC once per unique product AFTER all batches saved
            # Before fix: WAC recalculated inside loop = 1 query per batch
            # e.g. 5 batches of same product = 5 WAC recalculations
            # After fix: collect unique products, recalculate once each
            # e.g. 5 batches of same product = 1 WAC recalculation
            affected_products = set()

            for batch_data in batches_data:
                # Step 2 — Set remaining_quantity = quantity_received on arrival
                batch_data['remaining_quantity'] = batch_data['quantity_received']
                batch_data.setdefault('status', 'ACTIVE')

                # Step 3 — Create the batch row
                batch = PurchaseBatch.objects.create(
                    purchase=purchase,
                    **batch_data
                )

                # Step 4 — Add to total using Decimal arithmetic
                # Fix 10: Decimal * int — no float precision loss
                total_amount += batch.cost_price * batch.quantity_received

                # Step 5 — Write to Stock Ledger
                try:
                    from inventory.models import StockLedger
                    StockLedger.objects.create(
                        product          = batch.product,
                        batch            = batch,
                        transaction_type = 'PURCHASE',
                        source           = 'PURCHASE',
                        quantity_change  = batch.quantity_received,
                        reference_id     = purchase.id
                    )
                except Exception as e:
                    # Log failure — purchase and batch still saved
                    # inventory team must investigate if ledger is missing entries
                    logger.error(
                        f'StockLedger insert failed for batch {batch.id} '
                        f'(purchase {purchase.id}): {e}'
                    )

                # Collect product for WAC recalculation after loop
                affected_products.add(batch.product)

            # ── Fix 7 + 8: WAC recalculation — once per product ───
            # Fix 7: uses remaining_quantity not quantity_received
            #   quantity_received = original qty on arrival, never changes
            #   remaining_quantity = qty still in stock now, decreases as sold
            #   WAC must reflect current stock reality, not original arrivals
            #   This feeds F05 profit calculation and F09 discount profit floor
            #
            # Fix 8: runs after all batches saved, once per unique product
            #   If two batches for same product → only one WAC recalc needed
            #
            # Edge case (Logic Report): if remaining units = 0 → keep existing WAC
            for product in affected_products:
                try:
                    active_batches = PurchaseBatch.objects.filter(
                        product=product,
                        status='ACTIVE'
                    )
                    # Fix 10: Decimal arithmetic — no float() conversion
                    total_cost  = sum(
                        b.cost_price * b.remaining_quantity   # Fix 7 ← remaining
                        for b in active_batches
                    )
                    total_units = sum(
                        b.remaining_quantity                  # Fix 7 ← remaining
                        for b in active_batches
                    )
                    if total_units > 0:
                        product.avg_cost_price = round(
                            total_cost / total_units, 2
                        )
                        product.save(update_fields=['avg_cost_price'])
                    # else: keep existing avg_cost_price unchanged (Logic Report)
                except Exception as e:
                    logger.error(
                        f'WAC recalculation failed for product {product.id}: {e}'
                    )

            # Step 7 — Save final total amount to purchase
            purchase.total_amount = round(total_amount, 2)
            purchase.save(update_fields=['total_amount'])

        return purchase

# ─────────────────────────────────────────────────────────────────
# Confirm-expiry serializers — used to move a batch out of
# PENDING_EXPIRY once staff enters the real expiry date (R9 gap fix)
# ─────────────────────────────────────────────────────────────────
class ConfirmBatchExpirySerializer(serializers.Serializer):
    expiry_date = serializers.DateField()

    def validate_expiry_date(self, value):
        if value <= timezone.now().date():
            raise serializers.ValidationError(
                'expiry_date must be a future date.'
            )
        return value


class BulkConfirmBatchExpiryItemSerializer(serializers.Serializer):
    batch_id = serializers.IntegerField()
    expiry_date = serializers.DateField()

    def validate_expiry_date(self, value):
        if value <= timezone.now().date():
            raise serializers.ValidationError(
                'expiry_date must be a future date.'
            )
        return value


class BulkConfirmBatchExpirySerializer(serializers.Serializer):
    batches = BulkConfirmBatchExpiryItemSerializer(many=True)

    def validate_batches(self, value):
        if not value:
            raise serializers.ValidationError(
                'At least one batch must be provided.'
            )
        return value