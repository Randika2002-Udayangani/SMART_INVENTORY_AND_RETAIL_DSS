from decimal import Decimal

from django.db.models import Avg, F
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Supplier
from .serializers import SupplierSerializer
from users.audit import log_action
from core.permissions import ReadPublicWriteAuthenticated
from core.authentication import LenientJWTAuthentication

from purchases.models import Purchase, PurchaseBatch
from inventory.models import SupplierReturn
from orders.models import ProductRating


class SupplierListCreateView(ReadPublicWriteAuthenticated, generics.ListCreateAPIView):
    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer
    authentication_classes = [LenientJWTAuthentication]

    def perform_create(self, serializer):
        supplier = serializer.save()
        log_action(
            user=self.request.user,
            action='CREATE',
            table_name='supplier',
            record_id=supplier.id,
            old_value=None,
            new_value=SupplierSerializer(supplier).data,
            request=self.request,
        )


class SupplierDetailView(ReadPublicWriteAuthenticated, generics.RetrieveUpdateDestroyAPIView):
    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer
    authentication_classes = [LenientJWTAuthentication]

    def perform_update(self, serializer):
        old_data = SupplierSerializer(self.get_object()).data
        supplier = serializer.save()
        log_action(
            user=self.request.user,
            action='UPDATE',
            table_name='supplier',
            record_id=supplier.id,
            old_value=old_data,
            new_value=SupplierSerializer(supplier).data,
            request=self.request,
        )

    def perform_destroy(self, instance):
        old_data = SupplierSerializer(instance).data
        log_action(
            user=self.request.user,
            action='DELETE',
            table_name='supplier',
            record_id=instance.id,
            old_value=old_data,
            new_value=None,
            request=self.request,
        )
        instance.delete()


# ═════════════════════════════════════════════════════════════════
# F11 — Supplier Scorecard
#
# Four components, each scored 0–100, then averaged. A component with
# no data yet for this supplier (e.g. no ratings on any product they've
# supplied, or no returns filed) is left out of the average rather than
# counted as 0 — same adaptive-weighting idea as the Health Score engine,
# so a new supplier isn't penalised for missing history vs poor performance.
# Auth: staff JWT required on all three (unlike base Supplier list/detail,
# which is public-read) — no ReadPublicWriteAuthenticated mixin here,
# so the global default (JWTAuthentication + IsAuthenticated) applies.
# ═════════════════════════════════════════════════════════════════

def _compute_scorecard(supplier):
    components = {}

    # ── Delivery accuracy: % of purchases delivered on/before expected_days
    purchases = Purchase.objects.filter(
        supplier=supplier,
        expected_days__isnull=False,
        actual_days__isnull=False,
    )
    if purchases.exists():
        on_time = purchases.filter(actual_days__lte=F('expected_days')).count()
        components['delivery_accuracy'] = round(on_time / purchases.count() * 100, 1)

    # ── Price stability: % of batches that did NOT increase >5% vs the
    # previous batch of the same product from this supplier
    batches = PurchaseBatch.objects.filter(
        purchase__supplier=supplier
    ).select_related('product').order_by('product_id', 'id')

    comparisons, stable = 0, 0
    last_cost_by_product = {}
    for batch in batches:
        prev = last_cost_by_product.get(batch.product_id)
        if prev is not None and prev > 0:
            comparisons += 1
            increase_pct = (batch.cost_price - prev) / prev * 100
            if increase_pct <= 5:
                stable += 1
        last_cost_by_product[batch.product_id] = batch.cost_price
    if comparisons > 0:
        components['price_stability'] = round(stable / comparisons * 100, 1)

    # ── Return acceptance rate: CONFIRMED / (CONFIRMED + REJECTED)
    # PENDING returns are excluded — they haven't been decided yet
    returns = SupplierReturn.objects.filter(supplier=supplier)
    decided = returns.filter(status__in=['CONFIRMED', 'REJECTED'])
    if decided.exists():
        confirmed = decided.filter(status='CONFIRMED').count()
        components['return_acceptance_rate'] = round(confirmed / decided.count() * 100, 1)

    # ── Avg product quality: avg ProductRating.rating (1–5, active only)
    # across every product this supplier has ever delivered a batch of,
    # scaled to 0–100
    product_ids = PurchaseBatch.objects.filter(
        purchase__supplier=supplier
    ).values_list('product_id', flat=True).distinct()
    ratings = ProductRating.objects.filter(product_id__in=product_ids, is_active=True)
    avg_rating = ratings.aggregate(avg=Avg('rating'))['avg']
    if avg_rating is not None:
        components['avg_product_quality'] = round(avg_rating / 5 * 100, 1)

    overall_score = (
        round(sum(components.values()) / len(components), 1)
        if components else None
    )

    return {
        'supplier_id'  : supplier.id,
        'supplier_name': supplier.supplier_name,
        'overall_score': overall_score,   # None = not enough data yet on any component
        'components'   : components,
    }


class SupplierScorecardSummaryView(APIView):
    """
    GET /api/suppliers/scorecard-summary/
    Ranked list of all suppliers by overall_score, highest first.
    Suppliers with no score yet (no data on any component) sort last.
    ⚠ Registered BEFORE suppliers/<int:pk>/ in urls.py.
    """
    def get(self, request):
        scores = [_compute_scorecard(s) for s in Supplier.objects.all()]
        scores.sort(key=lambda s: (s['overall_score'] is None, -(s['overall_score'] or 0)))
        return Response(scores)


class SupplierScorecardDetailView(APIView):
    """
    GET /api/suppliers/<id>/scorecard/
    Full component breakdown for one supplier.
    """
    def get(self, request, pk):
        try:
            supplier = Supplier.objects.get(pk=pk)
        except Supplier.DoesNotExist:
            return Response({'error': 'Supplier not found'}, status=404)
        return Response(_compute_scorecard(supplier))


class SupplierCostTrendView(APIView):
    """
    GET /api/suppliers/<id>/cost-trend/
    Cost price history per product supplied by this supplier, in
    batch order, flagging any increase over 5% vs the previous batch.
    """
    def get(self, request, pk):
        try:
            supplier = Supplier.objects.get(pk=pk)
        except Supplier.DoesNotExist:
            return Response({'error': 'Supplier not found'}, status=404)

        batches = PurchaseBatch.objects.filter(
            purchase__supplier=supplier
        ).select_related('product', 'purchase').order_by(
            'product_id', 'purchase__purchase_date', 'id'
        )

        by_product = {}
        for batch in batches:
            pid = batch.product_id
            entry = {
                'batch_id'     : batch.id,
                'purchase_date': batch.purchase.purchase_date,
                'cost_price'   : str(batch.cost_price),
                'flagged'      : False,
            }
            product_entry = by_product.setdefault(pid, {
                'product_id'  : pid,
                'product_name': batch.product.product_name,
                'history'     : [],
            })
            history = product_entry['history']
            if history:
                prev_cost = Decimal(history[-1]['cost_price'])
                if prev_cost > 0:
                    increase_pct = (batch.cost_price - prev_cost) / prev_cost * 100
                    if increase_pct > 5:
                        entry['flagged'] = True
                        entry['increase_pct'] = round(float(increase_pct), 1)
            history.append(entry)

        return Response(list(by_product.values()))