import os
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, generics
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser

from products.models import Product
from users.models import SystemConfig
from inventory.models import PurchaseBatch, StockLedger

from .models import (
    UploadLog,
    DailyBillSummary,
    ItemSalesRecord,
)

from .serializers import (
    UploadLogSerializer,
    DailyBillSerializer,
    ItemSalesSerializer,
)

# =========================================================
# HELPERS
# =========================================================

def validate_bill_row(row):
    """
    Validate a single bill row from daily bills data.
    """

    errors = []

    try:
        final = float(row.get('Final Amount', 0) or 0)
        discount = float(row.get('Amount', 0) or 0)

    except (ValueError, TypeError):
        return ['Could not parse amount values']

    if final <= 0:
        errors.append(
            f"Non-positive final amount: {final}"
        )

    if discount > final and final > 0:
        errors.append(
            f"Discount ({discount}) exceeds final amount"
        )

    if 0 < final < 10:
        errors.append(
            f"Suspiciously small amount: {final}"
        )

    return errors


# =========================================================
# ITEM LEDGER PDF UPLOAD
# POST /api/sales/upload/item-ledger/
# =========================================================

class ItemLedgerPDFUploadView(APIView):

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):

        file = request.FILES.get('file')

        if not file:
            return Response(
                {
                    'error': (
                        'No file uploaded. '
                        'Send file as form-data with key "file"'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        if not file.name.endswith('.pdf'):
            return Response(
                {'error': 'File must be a PDF'},
                status=status.HTTP_400_BAD_REQUEST
            )

        upload_log = UploadLog.objects.create(
            file_name=file.name,
            upload_type='ITEM_SALES',
            status='PARTIAL',
            error_message=''
        )

        try:

            import pdfplumber
            import io

            from collections import defaultdict
            from datetime import datetime

            pdf_bytes = file.read()

            errors = []

            product = None

            daily_totals = defaultdict(float)

            seen_bills = set()

            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:

                for page in pdf.pages:

                    text = page.extract_text()

                    if not text:
                        continue

                    # -------------------------------------------------
                    # Extract Product Name
                    # -------------------------------------------------

                    if product is None:

                        for line in text.split('\n'):

                            if 'Item No' in line and ':' in line:

                                product_name = (
                                    line.split(':', 1)[1].strip()
                                )

                                parts = product_name.split(' ', 1)

                                if parts[0].isdigit() and len(parts) > 1:
                                    product_name = parts[1].strip()

                                try:

                                    product = Product.objects.get(
                                        product_name=product_name
                                    )

                                except Product.DoesNotExist:

                                    upload_log.status = 'FAILED'

                                    upload_log.error_message = (
                                        f'Product not found: '
                                        f'"{product_name}"'
                                    )

                                    upload_log.save()

                                    return Response(
                                        {
                                            'error': (
                                                f'Product '
                                                f'"{product_name}" '
                                                f'not found'
                                            )
                                        },
                                        status=status.HTTP_400_BAD_REQUEST
                                    )

                                break

                    # -------------------------------------------------
                    # Extract Sales Rows
                    # -------------------------------------------------

                    for line in text.split('\n'):

                        parts = line.strip().split()

                        if len(parts) < 5:
                            continue

                        date_val = parts[0]

                        try:

                            sale_date = datetime.strptime(
                                date_val,
                                '%Y/%m/%d'
                            ).date()

                        except ValueError:
                            continue

                        if 'CASH' not in line or 'SALE' not in line:
                            continue

                        bill_no = parts[2]

                        bill_key = (sale_date, bill_no)

                        if bill_key in seen_bills:
                            continue

                        seen_bills.add(bill_key)

                        numeric_parts = []

                        for p in parts:

                            try:
                                numeric_parts.append(float(p))

                            except ValueError:
                                continue

                        if len(numeric_parts) < 2:
                            continue

                        qty_out = numeric_parts[-1]

                        if qty_out > 0:
                            daily_totals[sale_date] += qty_out

            # ---------------------------------------------------------
            # Product Not Found
            # ---------------------------------------------------------

            if product is None:

                upload_log.status = 'FAILED'

                upload_log.error_message = (
                    'Could not extract product name from PDF'
                )

                upload_log.save()

                return Response(
                    {
                        'error': (
                            'Could not extract product name from PDF'
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            # ---------------------------------------------------------
            # Insert Records
            # ---------------------------------------------------------

            inserted = 0
            skipped = 0

            unit_price = float(product.unit_price or 0)

            for sale_date, total_qty in sorted(daily_totals.items()):

                qty = int(total_qty)

                if qty <= 0:
                    continue

                already_exists = ItemSalesRecord.objects.filter(
                    product=product,
                    sale_date=sale_date
                ).exists()

                if already_exists:

                    skipped += 1

                    errors.append(
                        f'{sale_date}: '
                        f'Record already exists'
                    )

                    continue

                ItemSalesRecord.objects.create(
                    product=product,
                    sale_date=sale_date,
                    quantity_sold=qty,
                    unit_price=unit_price,
                    total_amount=round(qty * unit_price, 2),
                    upload=upload_log
                )

                inserted += 1

            # ---------------------------------------------------------
            # Update Sync Date
            # ---------------------------------------------------------

            SystemConfig.objects.update_or_create(
                key='last_item_ledger_sync',
                defaults={
                    'value': str(timezone.now().date()),
                    'description': (
                        'Last item ledger PDF sync date'
                    )
                }
            )

            # ---------------------------------------------------------
            # Final Upload Status
            # ---------------------------------------------------------

            if inserted == 0 and skipped == 0:
                upload_log.status = 'FAILED'

            elif errors:
                upload_log.status = 'PARTIAL'

            else:
                upload_log.status = 'SUCCESS'

            upload_log.error_message = '\n'.join(errors)[:2000]

            upload_log.save()

            return Response({
                'message': 'Item Ledger PDF upload complete',
                'product': product.product_name,
                'dates_inserted': inserted,
                'dates_skipped': skipped,
                'upload_log_id': upload_log.id,
                'errors': errors[:10]
            }, status=status.HTTP_201_CREATED)

        except Exception as e:

            upload_log.status = 'FAILED'

            upload_log.error_message = str(e)[:2000]

            upload_log.save()

            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


# =========================================================
# DAILY BILLS PDF UPLOAD
# POST /api/sales/upload/daily-bills/
# =========================================================

class DailyBillsUploadView(APIView):

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):

        file = request.FILES.get('file')

        if not file:
            return Response(
                {'error': 'No file uploaded'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not file.name.endswith('.pdf'):
            return Response(
                {'error': 'File must be a PDF'},
                status=status.HTTP_400_BAD_REQUEST
            )

        upload_log = UploadLog.objects.create(
            file_name=file.name,
            upload_type='DAILY_BILLS',
            status='PARTIAL',
            error_message=''
        )

        try:

            import pdfplumber
            import io

            pdf_bytes = file.read()

            inserted = 0
            skipped = 0

            bill_errors = []

            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:

                for page in pdf.pages:

                    tables = page.extract_tables()

                    for table in tables:

                        for row in table[1:]:

                            if not row or len(row) < 4:
                                continue

                            try:

                                bill_data = {
                                    'Date': row[0],
                                    'Bill No': row[1],
                                    'Customer': row[2],
                                    'Amount': row[3],
                                    'Final Amount': (
                                        row[4] if len(row) > 4 else 0
                                    ),
                                }

                                row_errors = validate_bill_row(
                                    bill_data
                                )

                                if row_errors:

                                    bill_errors.append(
                                        f"Bill "
                                        f"{bill_data['Bill No']}: "
                                        f"{row_errors}"
                                    )

                                    skipped += 1

                                    continue

                                DailyBillSummary.objects.create(
                                    sale_date=bill_data['Date'],
                                    bill_no=str(
                                        bill_data['Bill No']
                                    ),
                                    customer_name=str(
                                        bill_data['Customer'] or ''
                                    ),
                                    discount=float(
                                        bill_data['Amount'] or 0
                                    ),
                                    final_amount=float(
                                        bill_data['Final Amount'] or 0
                                    ),
                                    gross_amount=(
                                        float(
                                            bill_data['Amount'] or 0
                                        )
                                        +
                                        float(
                                            bill_data['Final Amount'] or 0
                                        )
                                    ),
                                    upload=upload_log,
                                    is_flagged=False
                                )

                                inserted += 1

                            except Exception as e:

                                skipped += 1

                                bill_errors.append(
                                    f'Row parse error: {str(e)}'
                                )

            upload_log.status = (
                'SUCCESS'
                if skipped == 0
                else 'PARTIAL'
            )

            upload_log.error_message = (
                '\n'.join(bill_errors)[:2000]
            )

            upload_log.save()

            return Response({
                'message': 'Daily bills upload complete',
                'inserted': inserted,
                'skipped': skipped,
                'upload_log_id': upload_log.id,
                'errors': bill_errors[:10]
            }, status=status.HTTP_201_CREATED)

        except Exception as e:

            upload_log.status = 'FAILED'

            upload_log.error_message = str(e)[:2000]

            upload_log.save()

            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


# =========================================================
# UPLOAD LOG APIs
# =========================================================

class UploadLogListView(generics.ListAPIView):

    queryset = UploadLog.objects.all().order_by(
        '-upload_date'
    )

    serializer_class = UploadLogSerializer


class UploadLogDetailView(generics.RetrieveAPIView):

    queryset = UploadLog.objects.all()

    serializer_class = UploadLogSerializer


# =========================================================
# ITEM SALES APIs
# =========================================================

class ItemSalesListView(generics.ListAPIView):

    serializer_class = ItemSalesSerializer

    def get_queryset(self):

        queryset = ItemSalesRecord.objects.all().order_by(
            "-sale_date"
        )

        product = self.request.query_params.get("product")

        if product:
            queryset = queryset.filter(
                product__id=product
            )

        return queryset
    
    
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def sales_summary(request):
    """
    GET /api/reports/sales-summary/

    Query params (both optional):
        ?date_from=YYYY-MM-DD   default: 30 days ago
        ?date_to=YYYY-MM-DD     default: today

    Response fields (agreed with Lavanya — week 4-5 plan):
        period                  { date_from, date_to }
        total_units_sold        int
        total_sales_revenue     float
        total_cost              float   (WAC-based)
        gross_profit            float   (revenue - cost)
        best_selling_product    { product_name, units_sold, revenue }
        worst_selling_product   { product_name, units_sold, revenue }
        top_products            list of 5 { product_name, units_sold, revenue }

    Data source: item_sales_record table (populated by item ledger PDF uploads)
    Cost method: WAC — uses Product.avg_cost_price, falls back to cost_price
    Auth: Staff JWT required
    """

    # ── 1. Parse and validate date range ─────────────────────────────────────
    date_to_str   = request.query_params.get('date_to')
    date_from_str = request.query_params.get('date_from')

    try:
        date_to   = date.fromisoformat(date_to_str)  if date_to_str   else date.today()
        date_from = date.fromisoformat(date_from_str) if date_from_str else date_to - timedelta(days=30)
    except ValueError:
        return Response(
            {'error': 'Invalid date format. Use YYYY-MM-DD.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    if date_from > date_to:
        return Response(
            {'error': 'date_from must be on or before date_to.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # ── 2. Query Item_Sales_Record for the date range ─────────────────────────
    records = ItemSalesRecord.objects.filter(
        sale_date__gte=date_from,
        sale_date__lte=date_to,
    )

    if not records.exists():
        return Response({
            'period':              {'date_from': str(date_from), 'date_to': str(date_to)},
            'total_units_sold':    0,
            'total_sales_revenue': 0.0,
            'total_cost':          0.0,
            'gross_profit':        0.0,
            'best_selling_product':  None,
            'worst_selling_product': None,
            'top_products':          [],
            'note': 'No sales records found for this date range. '
                    'Upload item ledger PDFs first via POST /api/sales/upload/item-sales/',
        })

    # ── 3. Store-level totals (fast — single DB query) ────────────────────────
    store_totals = records.aggregate(
        total_units   = Sum('quantity_sold'),
        total_revenue = Sum('total_amount'),
    )
    total_units_sold    = store_totals['total_units']   or 0
    total_sales_revenue = store_totals['total_revenue'] or Decimal('0')

    # ── 4. Per-product aggregation — units sold + revenue ─────────────────────
    per_product = (
        records
        .values('product_id')
        .annotate(
            units   = Sum('quantity_sold'),
            revenue = Sum('total_amount'),
        )
        .order_by('-units')  # descending — best seller is index 0
    )

    # ── 5. Enrich with product name + WAC cost ────────────────────────────────
    total_cost   = Decimal('0')
    product_rows = []

    # Bulk-fetch all needed products in one query (avoid N+1)
    product_ids  = [row['product_id'] for row in per_product]
    products_map = {
        p.id: p
        for p in Product.objects.filter(id__in=product_ids)
    }

    for row in per_product:
        product = products_map.get(row['product_id'])
        if not product:
            continue  # orphaned sales record — skip

        # WAC cost: prefer avg_cost_price, fallback to cost_price
        wac   = product.avg_cost_price or product.cost_price or Decimal('0')
        units = row['units'] or 0
        cost  = Decimal(str(units)) * wac
        total_cost += cost

        product_rows.append({
            'product_name': product.product_name,
            'units_sold':   units,
            'revenue':      round(float(row['revenue'] or 0), 2),
            '_cost':        float(cost),   # internal only — not in response
        })

    # Already sorted by units descending
    best_selling  = product_rows[0]   if product_rows else None
    worst_selling = product_rows[-1]  if product_rows else None
    top_5         = product_rows[:5]

    # ── 6. Build response ─────────────────────────────────────────────────────
    gross_profit = float(total_sales_revenue) - float(total_cost)

    def public_fields(p):
        """Return only the agreed response fields — strip internal _cost."""
        return {
            'product_name': p['product_name'],
            'units_sold':   p['units_sold'],
            'revenue':      p['revenue'],
        }

    return Response({
        'period': {
            'date_from': str(date_from),
            'date_to':   str(date_to),
        },
        'total_units_sold':    total_units_sold,
        'total_sales_revenue': round(float(total_sales_revenue), 2),
        'total_cost':          round(float(total_cost), 2),
        'gross_profit':        round(gross_profit, 2),
        'best_selling_product':  public_fields(best_selling)  if best_selling  else None,
        'worst_selling_product': public_fields(worst_selling) if worst_selling else None,
        'top_products': [public_fields(p) for p in top_5],
    })


"""
Plan spec:
    Returns counts: expiring_in_7_days, expiring_in_7_to_14_days,
                    expiring_in_14_to_30_days
    Staff JWT required

Lavanya uses this for:
    - The 3 red/orange/yellow expiry bucket cards on the dashboard
    - The expiry-risk widget (GET /api/dashboard/expiry-risk/)
"""

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def expiry_summary(request):
    """
    GET /api/reports/expiry-summary/

    Counts of ACTIVE batches (with remaining stock) expiring in
    each urgency window, plus a detailed list per window.

    Response:
        as_of                       str  — today's date
        expiring_in_7_days          int  — within 0-7 days
        expiring_in_7_to_14_days    int  — between 8-14 days
        expiring_in_14_to_30_days   int  — between 15-30 days
        total_expiring_in_30_days   int  — sum of all three
        batches_7_days              list — detailed batch list (7-day window)
        batches_7_to_14_days        list — detailed batch list (7-14 day window)
        batches_14_to_30_days       list — detailed batch list (14-30 day window)

    Auth: Staff JWT required
    """

    today = date.today()
    d7    = today + timedelta(days=7)
    d14   = today + timedelta(days=14)
    d30   = today + timedelta(days=30)

    # Base queryset: only ACTIVE batches with stock and an expiry date
    active_with_expiry = PurchaseBatch.objects.filter(
        status='ACTIVE',
        remaining_quantity__gt=0,
        expiry_date__isnull=False,
    ).select_related('product')

    # ── Counts ─────────────────────────────────────────────────────────────────
    batches_7    = active_with_expiry.filter(expiry_date__lte=d7)
    batches_7_14 = active_with_expiry.filter(expiry_date__gt=d7,  expiry_date__lte=d14)
    batches_14_30 = active_with_expiry.filter(expiry_date__gt=d14, expiry_date__lte=d30)

    count_7     = batches_7.count()
    count_7_14  = batches_7_14.count()
    count_14_30 = batches_14_30.count()

    def _serialize_batch(batch):
        """Return the detail dict for one batch."""
        days_left = (batch.expiry_date - today).days
        est_loss  = round(
            float(batch.remaining_quantity) * float(batch.cost_price or 0), 2
        )
        return {
            'batch_id':          batch.id,
            'product_id':        batch.product.id,
            'product_name':      batch.product.product_name,
            'sku_code':          batch.product.sku_code or '',
            'expiry_date':       str(batch.expiry_date),
            'days_until_expiry': days_left,
            'remaining_quantity': batch.remaining_quantity,
            'cost_price':        float(batch.cost_price or 0),
            'estimated_loss':    est_loss,  # remaining_qty x cost_price
        }

    return Response({
        'as_of':                    str(today),
        'expiring_in_7_days':       count_7,
        'expiring_in_7_to_14_days': count_7_14,
        'expiring_in_14_to_30_days': count_14_30,
        'total_expiring_in_30_days': count_7 + count_7_14 + count_14_30,
        'batches_7_days':       [_serialize_batch(b) for b in batches_7.order_by('expiry_date')],
        'batches_7_to_14_days': [_serialize_batch(b) for b in batches_7_14.order_by('expiry_date')],
        'batches_14_to_30_days': [_serialize_batch(b) for b in batches_14_30.order_by('expiry_date')],
    })

class DailyBillsListView(generics.ListAPIView):
    """
    GET /api/sales/daily-bills/

    Get Daily_Bill_Summary data.

    Optional query params:
        ?date_from=YYYY-MM-DD
        ?date_to=YYYY-MM-DD
    """

    serializer_class = DailyBillSerializer

    def get_queryset(self):

        queryset = DailyBillSummary.objects.all().order_by(
            "-sale_date"
        )

        date_from = self.request.query_params.get("date_from")
        date_to = self.request.query_params.get("date_to")

        if date_from:
            queryset = queryset.filter(
                sale_date__gte=date_from
            )

        if date_to:
            queryset = queryset.filter(
                sale_date__lte=date_to
            )

        return queryset