# products/views.py

from rest_framework import generics, permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from django.utils import timezone
import pandas as pd

from .models import Brand, Category, StoreZone, Product
from .serializers import (
    BrandSerializer, CategorySerializer,
    StoreZoneSerializer, ProductSerializer
)
# UploadLog lives in sales app — import it here
from sales.models import UploadLog


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
# StoreZone
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
    serializer_class = ProductSerializer
    permission_classes = [permissions.AllowAny]  # public — v3.0 fix

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


class ProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [permissions.AllowAny]  # public — v3.0 fix


# ─────────────────────────────────────────────
# Item Master Excel Upload  (Pipeline 1)
# POST /api/products/import/
# ─────────────────────────────────────────────
class ItemMasterUploadView(APIView):
    """
    Uploads the easyAcc Item Master Excel file (Book1.xlsx).

    File structure — NO header row, 495 rows in real file:
      Col A (index 0) — seq_number    : row number, ignored
      Col B (index 1) — product_name  : MANDATORY, primary match key
      Col C (index 2) — sinhala_name  : optional, ignored for now
      Col D (index 3) — sku_code      : sparse — only ~10/495 rows have a value
      Col E (index 4) — qty_on_hand   : informational only, ignored
      Col F (index 5) — unit_price    : MANDATORY, must be > 0

    Rules (from Data_Ingestion_Rules_v3.pdf):
      R1 — Skip row where name == 'DEFAULT ITEM'
      R2 — Skip row where name is blank
      R3 — Skip row where price <= 0
      R4 — Skip 2nd occurrence of same sku_code within this file
      R5 — Skip 2nd occurrence of same product_name when sku_code is NULL
      R6 — Negative qty_on_hand is allowed (real data shows -2) — ignored anyway
      R7 — Inactive product: update price only, do NOT reactivate
      R8 — New product with no category: insert with category=NULL, flag for staff
    """
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        file = request.FILES.get('file')

        # ── Basic validation ──────────────────────────────────────────────
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

        # ── Read Excel ────────────────────────────────────────────────────
        try:
            # header=None because easyAcc export has NO header row
            df = pd.read_excel(file, header=None)
        except Exception as e:
            upload_log.status = 'FAILED'
            upload_log.error_message = f'Could not read Excel file: {str(e)}'
            upload_log.save()
            return Response(
                {'error': f'Could not read file: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ── Processing counters ───────────────────────────────────────────
        inserted = 0
        updated  = 0
        skipped  = 0
        flagged  = 0   # new products with no category
        errors   = []

        # Duplicate tracking within THIS file (rules R4, R5)
        seen_skus   = {}
        seen_names  = {}

        # ── Row-by-row loop ───────────────────────────────────────────────
        for index, row in df.iterrows():
            row_num = index + 1  # human-readable row number

            # ── Extract fields ────────────────────────────────────────────
            # Col B (index 1) — product name
            raw_name = row.iloc[1] if pd.notna(row.iloc[1]) else ''
            product_name = str(raw_name).strip()

            # R1 — skip DEFAULT ITEM placeholder (always row 1 in easyAcc)
            if product_name == 'DEFAULT ITEM':
                skipped += 1
                continue

            # R2 — skip blank name
            if not product_name:
                skipped += 1
                errors.append(f'Row {row_num}: Empty product name — skipped')
                continue

            # Col D (index 3) — sku_code (sparse)
            raw_sku = row.iloc[3] if pd.notna(row.iloc[3]) else None
            sku_code = str(raw_sku).strip() if raw_sku is not None else None
            # Treat empty string as None
            if sku_code == '' or sku_code == 'nan':
                sku_code = None

            # Col F (index 5) — unit_price
            try:
                unit_price = float(row.iloc[5]) if pd.notna(row.iloc[5]) else 0.0
            except (ValueError, TypeError):
                unit_price = 0.0

            # R3 — skip invalid price
            if unit_price <= 0:
                skipped += 1
                errors.append(f'Row {row_num}: "{product_name}" has price {unit_price} — skipped')
                continue

            # R4 — duplicate SKU within this file
            if sku_code:
                if sku_code in seen_skus:
                    skipped += 1
                    errors.append(f'Row {row_num}: Duplicate SKU "{sku_code}" in file — skipped')
                    continue
                seen_skus[sku_code] = row_num

            # R5 — duplicate name within this file (only when sku_code is NULL)
            if not sku_code:
                if product_name in seen_names:
                    skipped += 1
                    errors.append(f'Row {row_num}: Duplicate name "{product_name}" in file — skipped')
                    continue
                seen_names[product_name] = row_num

            # ── Match existing product in database ────────────────────────
            # Strategy: try SKU first (if present), then fall back to name
            existing = None
            if sku_code:
                existing = Product.objects.filter(sku_code=sku_code).first()
            if not existing:
                existing = Product.objects.filter(product_name=product_name).first()

            if existing:
                # ── UPDATE existing product ───────────────────────────────
                # R7 — update price only; do NOT touch is_active
                existing.unit_price = unit_price
                # If DB had no SKU but file now provides one, save it
                if sku_code and not existing.sku_code:
                    existing.sku_code = sku_code
                existing.save()
                updated += 1

            else:
                # ── INSERT new product ────────────────────────────────────
                # R8 — new product with no category: insert with NULL, flag
                Product.objects.create(
                    product_name=product_name,
                    sku_code=sku_code,          # may be None for ~98% of rows
                    unit_price=unit_price,
                    cost_price=0,               # unknown — staff enters manually
                    avg_cost_price=0,           # unknown — updated on first purchase
                    is_active=True,
                    category=None,              # R8 — staff assigns later
                    brand=None,                 # staff assigns later
                    reorder_threshold=0,        # staff sets later
                    introduced_date=timezone.now().date(),
                )
                inserted += 1
                flagged += 1  # all new products need category assignment
                errors.append(
                    f'Row {row_num}: NEW product "{product_name}" inserted — '
                    f'needs category assignment'
                )

        # ── Finalise upload log ───────────────────────────────────────────
        if inserted == 0 and updated == 0:
            upload_log.status = 'FAILED'
        elif skipped == 0:
            upload_log.status = 'SUCCESS'
        else:
            upload_log.status = 'PARTIAL'

        upload_log.error_message = '\n'.join(errors)[:2000]
        upload_log.save()

        return Response({
            'message'         : 'Item Master upload complete',
            'file'            : file.name,
            'total_rows_read' : len(df),
            'inserted'        : inserted,
            'updated'         : updated,
            'skipped'         : skipped,
            'flagged_new'     : flagged,
            'upload_log_id'   : upload_log.id,
            # Only show first 20 errors in response (full list is in upload log)
            'notes'           : errors[:20],
        }, status=status.HTTP_201_CREATED)