from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from datetime import timedelta
from .models import Purchase, PurchaseBatch
from .serializers import (
    PurchaseSerializer, PurchaseCreateSerializer, PurchaseBatchSerializer
)


# ─────────────────────────────────────────────────────────────────
# POST /api/purchases/   — Create GRN (Goods Received Note)
# GET  /api/purchases/   — List all purchases
# ─────────────────────────────────────────────────────────────────
class PurchaseListCreateView(generics.ListCreateAPIView):
    """
    GET  — returns all purchases with their batches
    POST — creates a purchase + batches + stock ledger entries + WAC update

    POST body example:
    {
        "supplier": 1,
        "purchase_date": "2026-03-05",
        "invoice_number": "INV-001",
        "expected_days": 3,
        "actual_days": 3,
        "batches": [
            {
                "product": 1,
                "quantity_received": 50,
                "cost_price": "380.00",
                "expiry_date": "2026-09-01"
            }
        ]
    }
    """
    queryset = Purchase.objects.all().order_by('-purchase_date')

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return PurchaseCreateSerializer
        return PurchaseSerializer

    def create(self, request, *args, **kwargs):
        serializer = PurchaseCreateSerializer(data=request.data)
        if serializer.is_valid():
            purchase = serializer.save()
            # Return full purchase with batches in response
            output = PurchaseSerializer(purchase)
            return Response(
                {
                    'message': 'Purchase recorded successfully',
                    'purchase': output.data
                },
                status=status.HTTP_201_CREATED
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ─────────────────────────────────────────────────────────────────
# GET /api/purchases/<id>/  — Get one purchase with batches
# ─────────────────────────────────────────────────────────────────
class PurchaseDetailView(generics.RetrieveAPIView):
    queryset = Purchase.objects.all()
    serializer_class = PurchaseSerializer


# ─────────────────────────────────────────────────────────────────
# GET /api/batches/   — List all batches (with optional filters)
# Query params: ?status=ACTIVE  ?product=<id>
# ─────────────────────────────────────────────────────────────────
class BatchListView(generics.ListAPIView):
    """
    Returns all batches.
    Filter by: ?status=ACTIVE|EXPIRED|DEPLETED|DISPOSED
               ?product=<product_id>
    """
    serializer_class = PurchaseBatchSerializer

    def get_queryset(self):
        queryset = PurchaseBatch.objects.select_related(
            'product', 'purchase'
        ).all().order_by('-id')
        status_filter = self.request.query_params.get('status')
        product = self.request.query_params.get('product')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if product:
            queryset = queryset.filter(product__id=product)
        return queryset


# ─────────────────────────────────────────────────────────────────
# GET /api/batches/expiring-soon/  — Batches expiring within N days
# Query param: ?days=30 (default 30)
# ─────────────────────────────────────────────────────────────────
class BatchExpiringSoonView(APIView):
    """
    Returns all ACTIVE batches with expiry_date within the next N days.
    Default: 30 days.
    Usage: GET /api/batches/expiring-soon/?days=14
    """
    def get(self, request):
        days = int(request.query_params.get('days', 30))
        today = timezone.now().date()
        cutoff = today + timedelta(days=days)

        batches = PurchaseBatch.objects.filter(
            status='ACTIVE',
            expiry_date__isnull=False,
            expiry_date__lte=cutoff,
            expiry_date__gte=today
        ).order_by('expiry_date')

        serializer = PurchaseBatchSerializer(batches, many=True)
        return Response({
            'days_filter'  : days,
            'cutoff_date'  : str(cutoff),
            'count'        : batches.count(),
            'batches'      : serializer.data
        })


# ─────────────────────────────────────────────────────────────────
# PATCH /api/batches/<id>/status/  — Update batch status manually
# Body: {"status": "EXPIRED"}
# ─────────────────────────────────────────────────────────────────
class BatchStatusUpdateView(APIView):
    """
    Manually update a batch status.
    Valid statuses: ACTIVE, EXPIRED, DEPLETED, DISPOSED
    """
    def patch(self, request, pk):
        try:
            batch = PurchaseBatch.objects.get(pk=pk)
        except PurchaseBatch.DoesNotExist:
            return Response(
                {'error': 'Batch not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        new_status = request.data.get('status')
        valid_statuses = ['ACTIVE', 'EXPIRED', 'DEPLETED', 'DISPOSED']

        if new_status not in valid_statuses:
            return Response(
                {'error': f'Status must be one of {valid_statuses}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        batch.status = new_status
        batch.save()

        return Response({
            'message'  : f'Batch {batch.id} status updated',
            'id'       : batch.id,
            'product'  : batch.product.product_name,
            'status'   : batch.status
        })