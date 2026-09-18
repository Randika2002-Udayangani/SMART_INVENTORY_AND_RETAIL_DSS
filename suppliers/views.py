from decimal import Decimal

from django.db import transaction
from django.db.models import Avg, F, Count, Q
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Supplier
from .serializers import SupplierSerializer
from users.audit import log_action
from core.permissions import ReadPublicWriteAuthenticated
from core.authentication import LenientJWTAuthentication
from users.permissions import IsManagerOrAdmin
from core.pagination import StandardResultsPagination

from purchases.models import Purchase, PurchaseBatch
from inventory.models import DiscountRecommendation, SupplierReturn
from orders.models import ProductRating


class SupplierListCreateView(ReadPublicWriteAuthenticated, generics.ListCreateAPIView):
    """
    GET  /api/suppliers/?search=<text>&page=<n>&page_size=<n>
    POST /api/suppliers/

    Paginated — was previously unpaginated, returning every supplier on
    every page load. See core.pagination. Also gained ?search=, since
    the frontend (suppliers.html) previously did instant substring
    search across a fully-loaded in-memory list — real server-side
    pagination breaks that unless search moves server-side too, so
    both changed together. Matches on supplier_name, case-insensitive.
    """
    queryset = Supplier.objects.all().order_by('supplier_name')
    serializer_class = SupplierSerializer
    authentication_classes = [LenientJWTAuthentication]
    pagination_class = StandardResultsPagination

    def get_queryset(self):
        queryset = Supplier.objects.all().order_by('supplier_name')
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(supplier_name__icontains=search)
        return queryset

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

    def get_permissions(self):
        if self.request.method in ('PUT', 'PATCH', 'DELETE'):
            return [permissions.IsAuthenticated(), IsManagerOrAdmin()]
        return super().get_permissions()

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
        with transaction.atomic():
            # These records protect the supplier from deletion. Remove the
            # dependent history first, then delete the supplier atomically.
            SupplierReturn.objects.filter(supplier=instance).delete()
            DiscountRecommendation.objects.filter(
                batch__purchase__supplier=instance
            ).delete()
            Purchase.objects.filter(supplier=instance).delete()
            instance.delete()
        log_action(
            user=self.request.user,
            action='DELETE',
            table_name='supplier',
            record_id=instance.id,
            old_value=old_data,
            new_value=None,
            request=self.request,
        )


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

def _format_scorecard(supplier, components):
    """Shared formatter — builds the response dict from a components mapping.
    Used by both the single-supplier detail path and the bulk summary path
    below, so the two stay in sync."""
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


def _compute_scorecard(supplier):
    """
    Single-supplier scorecard — used by SupplierScorecardDetailView.
    Runs 4 queries, all scoped to this one supplier. Fine as-is: this
    is a per-request detail lookup, not a loop over every supplier
    (that case is handled separately by _compute_scorecards_bulk below,
    which does the same 4 things but in 4 queries total, not 4×N).
    """
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

    return _format_scorecard(supplier, components)


def _compute_scorecards_bulk(suppliers):
    """
    Bulk version of _compute_scorecard() — used by
    SupplierScorecardSummaryView, which needs every supplier's score
    at once. The single-supplier version above runs 4 queries per
    supplier; called in a loop for N suppliers that becomes 4×N queries
    (200 queries for 50 suppliers). This version runs the same 4 logical
    steps but as 4 queries TOTAL, independent of supplier count, by
    aggregating grouped by supplier_id and then grouping the per-batch
    price-stability walk in Python instead of re-querying per supplier.

    Returns: {supplier_id: {component_name: score, ...}, ...}
    Component semantics are identical to _compute_scorecard() above —
    same formulas, same exclusion rules — just computed for all
    suppliers in one pass instead of one supplier at a time.
    """
    supplier_ids = [s.id for s in suppliers]
    components_by_supplier = {sid: {} for sid in supplier_ids}

    # ── Delivery accuracy — one aggregated query for all suppliers ────────
    purchases_agg = (
        Purchase.objects
        .filter(
            supplier_id__in=supplier_ids,
            expected_days__isnull=False,
            actual_days__isnull=False,
        )
        .values('supplier_id')
        .annotate(
            total=Count('id'),
            on_time=Count('id', filter=Q(actual_days__lte=F('expected_days'))),
        )
    )
    for row in purchases_agg:
        if row['total']:
            components_by_supplier[row['supplier_id']]['delivery_accuracy'] = round(
                row['on_time'] / row['total'] * 100, 1
            )

    # ── Price stability + product-per-supplier map — one batches query
    # for every supplier, then walked in Python (same sequential-comparison
    # logic as the single-supplier version, just grouped by supplier here).
    batches = (
        PurchaseBatch.objects
        .filter(purchase__supplier_id__in=supplier_ids)
        .select_related('product', 'purchase')
        .order_by('purchase__supplier_id', 'product_id', 'id')
    )

    comparisons_by_supplier = {}
    stable_by_supplier = {}
    last_cost_by_supplier_product = {}
    product_ids_by_supplier = {sid: set() for sid in supplier_ids}

    for batch in batches:
        sid = batch.purchase.supplier_id
        product_ids_by_supplier[sid].add(batch.product_id)
        key = (sid, batch.product_id)
        prev = last_cost_by_supplier_product.get(key)
        if prev is not None and prev > 0:
            comparisons_by_supplier[sid] = comparisons_by_supplier.get(sid, 0) + 1
            increase_pct = (batch.cost_price - prev) / prev * 100
            if increase_pct <= 5:
                stable_by_supplier[sid] = stable_by_supplier.get(sid, 0) + 1
        last_cost_by_supplier_product[key] = batch.cost_price

    for sid in supplier_ids:
        comparisons = comparisons_by_supplier.get(sid, 0)
        if comparisons > 0:
            stable = stable_by_supplier.get(sid, 0)
            components_by_supplier[sid]['price_stability'] = round(
                stable / comparisons * 100, 1
            )

    # ── Return acceptance rate — one aggregated query for all suppliers ───
    returns_agg = (
        SupplierReturn.objects
        .filter(supplier_id__in=supplier_ids, status__in=['CONFIRMED', 'REJECTED'])
        .values('supplier_id')
        .annotate(
            decided=Count('id'),
            confirmed=Count('id', filter=Q(status='CONFIRMED')),
        )
    )
    for row in returns_agg:
        if row['decided']:
            components_by_supplier[row['supplier_id']]['return_acceptance_rate'] = round(
                row['confirmed'] / row['decided'] * 100, 1
            )

    # ── Avg product quality — one ratings query for every product touched
    # by any supplier, then averaged per-supplier in Python. Preserves the
    # exact same semantics as the single-supplier version: it averages
    # individual rating rows (not per-product averages) across every
    # product the supplier has ever supplied a batch of.
    all_product_ids = set()
    for pids in product_ids_by_supplier.values():
        all_product_ids.update(pids)

    ratings_by_product = {}
    if all_product_ids:
        for product_id, rating in ProductRating.objects.filter(
            product_id__in=all_product_ids, is_active=True
        ).values_list('product_id', 'rating'):
            ratings_by_product.setdefault(product_id, []).append(rating)

    for sid in supplier_ids:
        values = []
        for pid in product_ids_by_supplier[sid]:
            values.extend(ratings_by_product.get(pid, []))
        if values:
            avg_rating = sum(values) / len(values)
            components_by_supplier[sid]['avg_product_quality'] = round(
                float(avg_rating) / 5 * 100, 1
            )

    return components_by_supplier


class SupplierScorecardSummaryView(APIView):
    """
    GET /api/suppliers/scorecard-summary/
    Ranked list of all suppliers by overall_score, highest first.
    Suppliers with no score yet (no data on any component) sort last.
    ⚠ Registered BEFORE suppliers/<int:pk>/ in urls.py.

    Uses _compute_scorecards_bulk() — a fixed number of queries total
    (4), regardless of how many suppliers exist. Previously called
    _compute_scorecard() per supplier in a loop, which ran 4 queries
    PER supplier (4×N total — 200 queries for 50 suppliers).
    """
    def get(self, request):
        suppliers = list(Supplier.objects.all())
        components_by_supplier = _compute_scorecards_bulk(suppliers)
        scores = [
            _format_scorecard(s, components_by_supplier.get(s.id, {}))
            for s in suppliers
        ]
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