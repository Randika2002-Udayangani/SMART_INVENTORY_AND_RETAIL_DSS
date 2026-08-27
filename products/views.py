from rest_framework import generics, permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from django.shortcuts import render
from django.utils import timezone
import pandas as pd

from .models import Brand, Category, StoreZone, Product, ZoneRecommendation
from .serializers import (
    BrandSerializer, CategorySerializer,
    StoreZoneSerializer, ProductSerializer, ProductPublicSerializer,
    ZoneRecommendationSerializer
)
from sales.models import UploadLog


def product_list(request):
    return render(request, "customer/products.html")


# ─────────────────────────────────────────────
# Brand
# ─────────────────────────────────────────────
class BrandListCreateView(generics.ListCreateAPIView):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer


class BrandDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer


# ─────────────────────────────────────────────
# Category
# ─────────────────────────────────────────────
class CategoryListCreateView(generics.ListCreateAPIView):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer


class CategoryDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer


# ─────────────────────────────────────────────
# StoreZone — staff-only, no customer traffic, default auth is fine
# ─────────────────────────────────────────────
class StoreZoneListCreateView(generics.ListCreateAPIView):
    queryset = StoreZone.objects.all()
    serializer_class = StoreZoneSerializer


class StoreZoneDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = StoreZone.objects.all()
    serializer_class = StoreZoneSerializer


# ─────────────────────────────────────────────
# Product
# ─────────────────────────────────────────────
class ProductListCreateView(generics.ListCreateAPIView):
    authentication_classes = [LenientJWTAuthentication]

    def get_queryset(self):
        queryset = Product.objects.filter(is_active=True)
        category = self.request.query_params.get('category')
        brand    = self.request.query_params.get('brand')
        search   = self.request.query_params.get('search')
        if category:
            queryset = queryset.filter(category__id=category)
        if brand:
            queryset = queryset.filter(brand__id=brand)
        if search:
            queryset = queryset.filter(product_name__icontains=search)
        return queryset

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return ProductSerializer
        if self.request.user and self.request.user.is_authenticated:
            return ProductSerializer
        return ProductPublicSerializer

    def get_permissions(self):
        if self.request.method == 'POST':
            return [permissions.IsAuthenticated()]
        return [permissions.AllowAny()]


# ─────────────────────────────────────────────
# Customer-safe stock check  (F01, API Design Doc v3.1 §5.4)
# GET /api/products/<id>/availability/
# Public (Auth: No). Used by M3 Chalani (product detail/browse)
# and Kiritharan's Chatbot AVAILABILITY_QUERY intent — the chatbot
# is meant to call THIS endpoint rather than query stock directly,
# since exact quantity must never be exposed to customers.
# Logic: >10 units = AVAILABLE, 1–10 = LIMITED_STOCK, 0 = UNAVAILABLE.
# ─────────────────────────────────────────────
class ProductAvailabilityView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = [LenientJWTAuthentication]

    def get(self, request, pk):
        try:
            product = Product.objects.get(pk=pk, is_active=True)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'}, status=status.HTTP_404_NOT_FOUND)

        current_stock = PurchaseBatch.objects.filter(
            product=product, status='ACTIVE'
        ).aggregate(total=Sum('remaining_quantity'))['total'] or 0

        if current_stock == 0:
            availability_status = 'UNAVAILABLE'
            can_order = False
        elif current_stock <= 10:
            availability_status = 'LIMITED_STOCK'
            can_order = True
        else:
            availability_status = 'AVAILABLE'
            can_order = True

        return Response({
            'status': availability_status,
            'can_order': can_order,
        })


class ProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Product.objects.all()
    authentication_classes = [LenientJWTAuthentication]

    def get_serializer_class(self):
        if self.request.method in ('PUT', 'PATCH', 'DELETE'):
            return ProductSerializer
        if self.request.user and self.request.user.is_authenticated:
            return ProductSerializer
        return ProductPublicSerializer

    def get_permissions(self):
        if self.request.method in ('PUT', 'PATCH', 'DELETE'):
            return [permissions.IsAuthenticated()]
        return [permissions.AllowAny()]


# ─────────────────────────────────────────────
# Item Master Excel Upload (Pipeline 1)
# POST /api/products/import/
# ─────────────────────────────────────────────
class ItemMasterUploadView(APIView):
    parser_classes     = [MultiPartParser, FormParser]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        file = request.FILES.get('file')

        if not file:
            return Response(
                {'error': 'No file uploaded. Send file as form-data with key "file"'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if not file.name.endswith('.xlsx'):
            return Response(
                {'error': 'File must be an Excel .xlsx file'},
                status=status.HTTP_400_BAD_REQUEST
            )

        upload_log = UploadLog.objects.create(
            file_name=file.name,
            upload_type='ITEM_MASTER',
            status='PARTIAL',
            error_message=''
        )

        try:
            df = pd.read_excel(file, header=None)
        except Exception as e:
            upload_log.status = 'FAILED'
            upload_log.error_message = f'Could not read Excel file: {str(e)}'
            upload_log.save()
            return Response(
                {'error': f'Could not read file: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        products_by_sku  = {
            p.sku_code: p
            for p in Product.objects.exclude(sku_code__isnull=True)
                                    .exclude(sku_code='')
        }
        products_by_name = {
            p.product_name.lower(): p
            for p in Product.objects.all()
        }

        inserted = 0
        updated  = 0
        skipped  = 0
        flagged  = 0
        errors   = []
        seen_skus  = {}
        seen_names = {}

        for index, row in df.iterrows():
            row_num = index + 1

            if len(row) < 6:
                skipped += 1
                errors.append(
                    f'Row {row_num}: Only {len(row)} columns found — '
                    f'expected at least 6. Row skipped.'
                )
                continue

            raw_name     = row.iloc[1] if not pd.isna(row.iloc[1]) else ''
            product_name = str(raw_name).strip()

            if product_name == 'DEFAULT ITEM':
                skipped += 1
                continue

            if not product_name:
                skipped += 1
                errors.append(f'Row {row_num}: Empty product name — skipped')
                continue

            raw_sku  = row.iloc[3] if not pd.isna(row.iloc[3]) else None
            sku_code = str(raw_sku).strip() if raw_sku is not None else None
            if not sku_code or sku_code.lower() in ('nan', 'none', ''):
                sku_code = None

            try:
                unit_price = float(row.iloc[5]) if not pd.isna(row.iloc[5]) else 0.0
            except (ValueError, TypeError):
                unit_price = 0.0

            if unit_price <= 0:
                skipped += 1
                errors.append(
                    f'Row {row_num}: "{product_name}" price={unit_price} — skipped'
                )
                continue

            if sku_code:
                if sku_code in seen_skus:
                    skipped += 1
                    errors.append(
                        f'Row {row_num}: Duplicate SKU "{sku_code}" '
                        f'(first seen row {seen_skus[sku_code]}) — skipped'
                    )
                    continue
                seen_skus[sku_code] = row_num

            if not sku_code:
                normalized_name = product_name.lower()
                if normalized_name in seen_names:
                    skipped += 1
                    errors.append(
                        f'Row {row_num}: Duplicate name "{product_name}" '
                        f'(first seen row {seen_names[normalized_name]}) — skipped'
                    )
                    continue
                seen_names[normalized_name] = row_num

            existing = None
            if sku_code:
                existing = products_by_sku.get(sku_code)
            if not existing:
                existing = products_by_name.get(product_name.lower())

            if existing:
                existing.unit_price = unit_price
                if sku_code and not existing.sku_code:
                    existing.sku_code = sku_code
                    existing.save(update_fields=['unit_price', 'sku_code'])
                    products_by_sku[sku_code] = existing
                else:
                    existing.save(update_fields=['unit_price'])
                updated += 1

            else:
                new_product = Product.objects.create(
                    product_name      = product_name,
                    sku_code          = sku_code,
                    unit_price        = unit_price,
                    cost_price        = 0,
                    avg_cost_price    = 0,
                    is_active         = True,
                    category          = None,
                    brand             = None,
                    reorder_threshold = 0,
                    introduced_date   = timezone.now().date(),
                )
                inserted += 1
                flagged  += 1
                products_by_name[product_name.lower()] = new_product
                if sku_code:
                    products_by_sku[sku_code] = new_product
                errors.append(
                    f'Row {row_num}: NEW product "{product_name}" inserted — '
                    f'needs category assignment'
                )

        if inserted == 0 and updated == 0:
            upload_log.status = 'FAILED'
        elif skipped == 0 and flagged == 0:
            upload_log.status = 'SUCCESS'
        else:
            upload_log.status = 'PARTIAL'

        upload_log.error_message = '\n'.join(errors[:100])
        upload_log.save()

        return Response({
            'message'       : 'Item Master upload complete',
            'file'          : file.name,
            'total_rows'    : len(df),
            'inserted'      : inserted,
            'updated'       : updated,
            'skipped'       : skipped,
            'flagged_new'   : flagged,
            'upload_log_id' : upload_log.id,
            'notes'         : errors[:20],
        }, status=status.HTTP_201_CREATED)


# ─────────────────────────────────────────────
# Zone Recommendation
# ─────────────────────────────────────────────
class ZoneRecommendationListView(generics.ListAPIView):
    queryset = ZoneRecommendation.objects.select_related(
        'product', 'current_zone', 'suggested_zone'
    ).order_by('-recommendation_date')
    serializer_class   = ZoneRecommendationSerializer
    permission_classes = [permissions.IsAuthenticated]


# ─────────────────────────────────────────────
# Recalculate WAC
# ─────────────────────────────────────────────
class RecalculateWACView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        from purchases.models import PurchaseBatch

        try:
            product = Product.objects.get(pk=pk)
        except Product.DoesNotExist:
            return Response(
                {'error': 'Product not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        batches     = PurchaseBatch.objects.filter(product=product)
        total_units = sum(b.quantity_received for b in batches)

        if total_units == 0:
            return Response(
                {'error': 'No purchase batches found for this product — cannot calculate WAC'},
                status=status.HTTP_400_BAD_REQUEST
            )

        total_cost = sum(b.quantity_received * b.cost_price for b in batches)
        old_wac    = product.avg_cost_price
        new_wac    = total_cost / total_units

        product.avg_cost_price = new_wac
        product.save(update_fields=['avg_cost_price'])

        return Response({
            'product_id'          : product.id,
            'product_name'        : product.product_name,
            'old_avg_cost_price'  : old_wac,
            'new_avg_cost_price'  : round(new_wac, 2),
            'total_units_received': total_units,
            'batches_used'        : batches.count(),
        })