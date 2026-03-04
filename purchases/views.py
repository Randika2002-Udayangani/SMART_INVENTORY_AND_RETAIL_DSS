from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from datetime import timedelta
from .models import Purchase, PurchaseBatch
from .serializers import (
    PurchaseSerializer, PurchaseCreateSerializer, PurchaseBatchSerializer
)


class PurchaseListCreateView(generics.ListCreateAPIView):
    queryset = Purchase.objects.all().order_by('-purchase_date')

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return PurchaseCreateSerializer
        return PurchaseSerializer


class PurchaseDetailView(generics.RetrieveUpdateAPIView):
    queryset = Purchase.objects.all()
    serializer_class = PurchaseSerializer


class BatchListView(generics.ListAPIView):
    serializer_class = PurchaseBatchSerializer

    def get_queryset(self):
        queryset = PurchaseBatch.objects.all()
        status_filter = self.request.query_params.get('status')
        product = self.request.query_params.get('product')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if product:
            queryset = queryset.filter(product__id=product)
        return queryset


class BatchExpiringSoonView(APIView):
    def get(self, request):
        days = int(request.query_params.get('days', 30))
        cutoff = timezone.now().date() + timedelta(days=days)
        batches = PurchaseBatch.objects.filter(
            status='ACTIVE',
            expiry_date__lte=cutoff,
            expiry_date__gte=timezone.now().date()
        ).order_by('expiry_date')
        serializer = PurchaseBatchSerializer(batches, many=True)
        return Response({
            'days_filter': days,
            'count': batches.count(),
            'batches': serializer.data
        })


class BatchStatusUpdateView(APIView):
    def patch(self, request, pk):
        try:
            batch = PurchaseBatch.objects.get(pk=pk)
        except PurchaseBatch.DoesNotExist:
            return Response(
                {'error': 'Batch not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        new_status = request.data.get('status')
        valid = ['ACTIVE', 'EXPIRED', 'DEPLETED', 'DISPOSED']
        if new_status not in valid:
            return Response(
                {'error': f'Status must be one of {valid}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        batch.status = new_status
        batch.save()
        return Response({'id': batch.id, 'status': batch.status})