# products/views.py
from rest_framework import generics, permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from django.utils import timezone
from core.permissions import ReadPublicWriteAuthenticated
from core.authentication import LenientJWTAuthentication
from users.audit import log_action
import pandas as pd
from users.permissions import IsManagerOrAdmin

from .models import Brand, Category, StoreZone, Product, ZoneRecommendation
from .serializers import (
    BrandSerializer, CategorySerializer,
    StoreZoneSerializer, ProductSerializer, ProductPublicSerializer, ZoneRecommendationSerializer
)
from sales.models import UploadLog
from sales.services.excel_parser import parse_item_master
from inventory.services.auto_categorise import classify_product

from purchases.models import PurchaseBatch
from django.db.models import Sum

# ─────────────────────────────────────────────
# Brand
# ─────────────────────────────────────────────
class BrandListCreateView(ReadPublicWriteAuthenticated, generics.ListCreateAPIView):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer
    authentication_classes = [LenientJWTAuthentication]


class BrandDetailView(ReadPublicWriteAuthenticated, generics.RetrieveUpdateDestroyAPIView):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer
    authentication_classes = [LenientJWTAuthentication]


# ─────────────────────────────────────────────
# Category
# ─────────────────────────────────────────────
class CategoryListCreateView(ReadPublicWriteAuthenticated, generics.ListCreateAPIView):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    authentication_classes = [LenientJWTAuthentication]


class CategoryDetailView(ReadPublicWriteAuthenticated, generics.RetrieveUpdateDestroyAPIView):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    authentication_classes = [LenientJWTAuthentication]

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
        # POST (create) — staff only, always full serializer
        if self.request.method == 'POST':
            return ProductSerializer
        # GET — public but cost fields hidden for unauthenticated
        if self.request.user and self.request.user.is_authenticated:
            return ProductSerializer
        return ProductPublicSerializer

    # Point 1 fix: method-level permissions
    # Before: permission_classes = [permissions.AllowAny]
    #         AllowAny applied to ALL methods including POST
    #         Any unauthenticated user could create products
    # After:  GET → public (customers browse product list)
    #         POST → authenticated staff only
    def get_permissions(self):
        if self.request.method == 'POST':
            return [permissions.IsAuthenticated()]
        return [permissions.AllowAny()]

    def perform_create(self, serializer):
        product = serializer.save()
        log_action(
            user=self.request.user,
            action='CREATE',
            table_name='product',
            record_id=product.id,
            old_value=None,
            new_value=ProductSerializer(product).data,
            request=self.request,
        )


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

    # Point 2 fix: method-level permissions
    # Before: permission_classes = [permissions.AllowAny]
    #         Anyone could PUT/PATCH/DELETE any product
    # After:  GET → public (customers view product detail)
    #         PUT/PATCH/DELETE → authenticated staff only
    def get_permissions(self):
        if self.request.method == 'DELETE':
            return [permissions.IsAuthenticated(), IsManagerOrAdmin()]
        if self.request.method in ('PUT', 'PATCH'):
             return [permissions.IsAuthenticated()]
        return [permissions.AllowAny()]

    def perform_update(self, serializer):
        old_data = ProductSerializer(self.get_object()).data
        product = serializer.save()
        new_data = ProductSerializer(product).data

        # API Design Doc §22 lists 'price_change' as a mandatory audit
        # action distinct from a generic field edit — flag it specifically
        # when unit_price or cost_price actually moved, otherwise log as a
        # plain UPDATE so non-price edits (name, category, etc.) still show.
        price_changed = (
            str(old_data.get('unit_price')) != str(new_data.get('unit_price')) or
            str(old_data.get('cost_price')) != str(new_data.get('cost_price'))
        )
        log_action(
            user=self.request.user,
            action='PRICE_CHANGE' if price_changed else 'UPDATE',
            table_name='product',
            record_id=product.id,
            old_value=old_data,
            new_value=new_data,
            request=self.request,
        )

    def perform_destroy(self, instance):
        # FIX: API Design Doc §5.4 requires DELETE to *deactivate*
        # (is_active=False), never hard-delete — a real delete would break
        # every PurchaseBatch/ZoneRecommendation/etc. FK pointing at this
        # product. The previous version had no perform_destroy() override,
        # so it fell through to DRF's default instance.delete().
        #
        # NOT YET IMPLEMENTED: the spec also says this should be "Blocked
        # if product has PENDING/CONFIRMED online orders" — that check
        # needs the Orders app's Order model, which doesn't exist in this
        # codebase yet. Add that guard here once orders/models.py lands.
        old_data = ProductSerializer(instance).data
        instance.is_active = False
        instance.save(update_fields=['is_active'])
        log_action(
            user=self.request.user,
            action='PRODUCT_DEACTIVATION',
            table_name='product',
            record_id=instance.id,
            old_value=old_data,
            new_value=ProductSerializer(instance).data,
            request=self.request,
        )


# ─────────────────────────────────────────────
# Item Master Excel Upload  (Pipeline 1)
# POST /api/products/import/
# ─────────────────────────────────────────────
class ItemMasterUploadView(APIView):
    """
    Uploads the easyAcc Item Master Excel file (Book1.xlsx).

    File structure — NO header row, 495 rows in real file:
      Col A (index 0) — seq_number   : row reference only, never stored
      Col B (index 1) — product_name : MANDATORY, primary match key
      Col C (index 2) — sinhala_name : optional, ignored
      Col D (index 3) — sku_code     : sparse — only ~10/495 rows have a value
      Col E (index 4) — qty_on_hand  : informational only, ignored (R6)
      Col F (index 5) — unit_price   : MANDATORY, must be > 0

    Rules (Data_Ingestion_Rules_v3.pdf — all 8 verified):
      R1 ✅ Skip 'DEFAULT ITEM' placeholder silently
      R2 ✅ Skip blank product name — log error
      R3 ✅ Skip price <= 0 — log error
      R4 ✅ Skip 2nd duplicate SKU in file — log warning
      R5 ✅ Skip 2nd duplicate name in file when sku_code is NULL
             Case-insensitive check: "MILK" and "Milk" treated as same product
      R6 ✅ Negative qty_on_hand allowed — field is ignored entirely
      R7 ✅ Inactive product: update_fields=['unit_price'] — is_active never touched
      R8 ✅ New product no category: insert with category=None, flagged for staff

    Performance:
      Products preloaded into memory before loop — 2 DB queries total
      instead of ~1000 queries for 495 rows
    """
    parser_classes   = [MultiPartParser, FormParser]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        file = request.FILES.get('file')

        # ── Basic file validation ─────────────────────────────────────────
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

        # ── Create upload log ─────────────────────────────────────────────
        upload_log = UploadLog.objects.create(
            file_name=file.name,
            upload_type='ITEM_MASTER',
            status='PARTIAL',
            error_message=''
        )

        # ── Parse Excel via extracted service function ────────────────────
        parsed = parse_item_master(file)

        if parsed['read_error']:
            upload_log.status = 'FAILED'
            upload_log.error_message = parsed['read_error']
            upload_log.save()
            return Response(
                {'error': parsed['read_error']},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ── Preload all products into memory ──────────────────────────────
        products_by_sku  = {
            p.sku_code: p
            for p in Product.objects.exclude(sku_code__isnull=True)
                                    .exclude(sku_code='')
        }
        products_by_name = {
            p.product_name.lower(): p
            for p in Product.objects.all()
        }

        # ── Processing counters ───────────────────────────────────────────
        inserted = 0
        updated  = 0
        skipped  = parsed['skipped']
        flagged  = 0
        errors   = parsed['errors'][:]

        # ── Process parsed rows ───────────────────────────────────────────
        for item in parsed['rows']:
            product_name = item['product_name']
            sku_code     = item['sku_code']
            unit_price   = item['unit_price']
            row_num      = item['row_num']

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
                detected_category_name, detected_brand_name = classify_product(product_name)

                category_obj, _ = Category.objects.get_or_create(
                    category_name=detected_category_name,
                    defaults={'default_zone': None}
                )

                brand_obj = None
                if detected_brand_name != 'Unbranded':
                    brand_obj, _ = Brand.objects.get_or_create(
                        brand_name=detected_brand_name,
                        defaults={'manufacturer': ''}
                    )

                new_product = Product.objects.create(
                    product_name      = product_name,
                    sku_code          = sku_code,
                    unit_price        = unit_price,
                    cost_price        = 0,
                    avg_cost_price    = 0,
                    is_active         = True,
                    category          = category_obj,
                    brand             = brand_obj,
                    reorder_threshold = 0,
                    introduced_date   = timezone.now().date(),
                )
                inserted += 1
                products_by_name[product_name.lower()] = new_product
                if sku_code:
                    products_by_sku[sku_code] = new_product

                if detected_category_name == 'General':
                    flagged += 1
                    errors.append(
                        f'Row {row_num}: NEW product "{product_name}" inserted — '
                        f'category could not be auto-detected, manual assignment needed'
                    )
                else:
                    errors.append(
                        f'Row {row_num}: NEW product "{product_name}" inserted — '
                        f'auto-assigned to {detected_category_name} / {detected_brand_name}'
                    )

        # ── Finalise upload log ───────────────────────────────────────────
        if inserted == 0 and updated == 0:
            upload_log.status = 'FAILED'
        elif skipped == 0 and flagged == 0:
            upload_log.status = 'SUCCESS'
        else:
            upload_log.status = 'PARTIAL'

        # Point 7 fix: truncate by line count not character count
        # Before: '\n'.join(errors)[:2000] — cuts mid-line, corrupts last message
        # After:  '\n'.join(errors[:100])  — keeps complete lines, max 100 entries
        upload_log.error_message = '\n'.join(errors[:100])
        upload_log.save()

        return Response({
            'message'       : 'Item Master upload complete',
            'file'          : file.name,
            'total_rows'    : len(parsed['rows']) + parsed['skipped'],
            'inserted'      : inserted,
            'updated'       : updated,
            'skipped'       : skipped,
            'flagged_new'   : flagged,
            'upload_log_id' : upload_log.id,
            'notes'         : errors[:20],
        }, status=status.HTTP_201_CREATED)


class ZoneRecommendationListView(generics.ListAPIView):
    """
    GET /api/zones/recommendations/

    Zone placement recommendations per product.

    IMPORTANT — read-only for now: this serves whatever rows already
    exist in ZoneRecommendation. It does NOT calculate new
    recommendations on the fly. Per the API doc, the actual scoring
    logic (velocity_score + margin_score -> high-traffic zone;
    expiry_risk_score high -> end-of-aisle promo zone) depends on
    Nipuni's health-score/lifecycle engines for velocity and margin
    data. Until something populates this table (either a future
    'calculate' endpoint or a management command), this will just
    return an empty list — that's expected, not a bug.
    """
    queryset = ZoneRecommendation.objects.select_related(
        'product', 'current_zone', 'suggested_zone'
    ).order_by('-recommendation_date')
    serializer_class = ZoneRecommendationSerializer
    permission_classes = [permissions.IsAuthenticated]


class RecalculateWACView(APIView):
    """
    POST /api/products/{id}/recalculate-wac/

    Recalculates avg_cost_price using the WAC formula:
        avg_cost_price = total_purchase_cost / total_units_received

    Computed across ALL purchase batches ever received for this product
    (not just ACTIVE ones) — WAC is a historical weighted average of
    cost, not a current-stock snapshot. This is the same formula
    purchases/views.py already runs automatically when a new batch is
    created (per API doc: "M1 — called automatically on batch creation").
    This endpoint is just the manual re-trigger version.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        # Local import to avoid any risk of circular import between
        # products and purchases apps.
        from purchases.models import PurchaseBatch

        try:
            product = Product.objects.get(pk=pk)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'}, status=status.HTTP_404_NOT_FOUND)

        batches = PurchaseBatch.objects.filter(product=product)
        total_units = sum(b.quantity_received for b in batches)

        if total_units == 0:
            return Response(
                {'error': 'No purchase batches found for this product — cannot calculate WAC'},
                status=status.HTTP_400_BAD_REQUEST
            )

        total_cost = sum(b.quantity_received * b.cost_price for b in batches)
        old_wac = product.avg_cost_price
        new_wac = total_cost / total_units

        product.avg_cost_price = new_wac
        product.save(update_fields=['avg_cost_price'])

        log_action(
            user=request.user,
            action='PRICE_CHANGE',
            table_name='product',
            record_id=product.id,
            old_value={'avg_cost_price': str(old_wac)},
            new_value={'avg_cost_price': str(round(new_wac, 2))},
            request=request,
        )

        return Response({
            'product_id': product.id,
            'product_name': product.product_name,
            'old_avg_cost_price': old_wac,
            'new_avg_cost_price': round(new_wac, 2),
            'total_units_received': total_units,
            'batches_used': batches.count(),
        })


class ReclassifyProductsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from inventory.services.auto_categorise import classify_all_products
        from django.db import models as django_models

        products_to_classify = Product.objects.filter(
            is_active=True
        ).filter(
            django_models.Q(category__isnull=True) | django_models.Q(brand__isnull=True)
        )

        result = classify_all_products(products_to_classify)

        log_action(
            user=request.user,
            action='RECLASSIFY',
            table_name='product',
            record_id=None,
            old_value=None,
            new_value={
                'classified': result['classified'],
                'already_had_category': result['already_had_category'],
                'total_processed': result['total_processed'],
            },
            request=request,
        )

        return Response({
            'message':              'Reclassification complete',
            'classified':           result['classified'],
            'already_had_category': result['already_had_category'],
            'errors':               result['errors'],
            'total_processed':      result['total_processed'],
        }, status=status.HTTP_200_OK)





class ZoneRecommendationCalculateView(APIView):
    """
    POST /api/zones/recommendations/calculate/
 
    Manager triggers zone placement recalculation for all active
    products. Reads each product's latest InventoryHealthScore and
    writes ZoneRecommendation rows for products that should move —
    see inventory/services/zone_recommendation.py for the scoring
    rule and the StoreZone data-model limitation noted there (no
    zone "purpose" field, so this maps onto traffic_level only).
    """
    permission_classes = [permissions.IsAuthenticated]
 
    def post(self, request):
        # Local import — same reasoning as RecalculateWACView above:
        # avoids any risk of a circular import between products and
        # inventory apps.
        from inventory.services.zone_recommendation import calculate_zone_recommendations
 
        result = calculate_zone_recommendations()
 
        return Response({
            "message": "Zone recommendations recalculated",
            **result,
        })
