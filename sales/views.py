import io
...
from django.http import HttpResponse
...
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

import os
from datetime import date, timedelta, datetime
from decimal import Decimal

import pandas as pd

from django.db import transaction
from django.db.models import Sum, OuterRef, Subquery
from django.utils import timezone

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, generics
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser

from products.models import Product
from users.models import SystemConfig
from inventory.models import PurchaseBatch, StockLedger, LossRecord, InventoryHealthScore, ReorderRecommendation

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

from inventory.models import PurchaseBatch, StockLedger, LossRecord, InventoryHealthScore
from suppliers.models import Supplier
from suppliers.views import _compute_scorecard

from inventory.services.lifecycle import get_latest_lifecycle

# =========================================================
# HELPERS
# =========================================================

def validate_bill_row(row):
    """
    Validate a single bill row from daily bills data.

    NOTE: as of the text-extraction rewrite (Aug 2026), this takes
    'gross_amount', 'discount', 'final_amount' — NOT the old
    'Amount'/'Final Amount' keys. The old contract silently treated
    the report's "Amount" column (gross, pre-discount) as if it were
    the discount value, which meant the discount>final check below
    was comparing the wrong pair of numbers. Renamed for clarity
    since this is the only call site.

    Returns (errors, warnings) — NOT a single flat list:
        errors   — genuinely invalid data (unparseable, non-positive
                   final amount, discount exceeding gross). The row
                   is skipped and NOT inserted.
        warnings — real, valid bills that are just unusual (e.g. a
                   small final amount — a single loose item or a
                   bag charge really can be Rs. 2). These bills ARE
                   inserted, with is_flagged=True, so they stay in
                   the revenue totals and remain visible for manager
                   review instead of silently vanishing from
                   DailyBillSummary. Revenue accuracy against the
                   PDF's own printed totals takes priority over
                   filtering out bills that are merely unusual.
    """

    errors = []
    warnings = []

    try:
        gross    = float(row.get('gross_amount', 0) or 0)
        discount = float(row.get('discount', 0) or 0)
        final    = float(row.get('final_amount', 0) or 0)

    except (ValueError, TypeError):
        return ['Could not parse amount values'], []

    if final <= 0:
        errors.append(
            f"Non-positive final amount: {final}"
        )

    if discount > gross and gross > 0:
        errors.append(
            f"Discount ({discount}) exceeds gross amount ({gross})"
        )

    if 0 < final < 10:
        warnings.append(
            f"Suspiciously small amount: {final}"
        )

    return errors, warnings


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
                        # parts[0]=date, parts[1]=actual bill number, parts[2]='CASH' (literal word)
                        # Using parts[2] here previously caused every row's "bill_no" to be the string
                        # "CASH", making all same-day sales look like duplicate bills and get skipped.
                        bill_no = parts[1]

                       

                        numeric_parts = []

                        for p in parts:

                            try:
                                numeric_parts.append(float(p))

                            except ValueError:
                                continue

                        if len(numeric_parts) < 2:
                            continue

                        # Row format: ... IN OUT Balance  (3 trailing numbers)
                        # OUT is second-to-last; last is the cumulative running Balance, not a transaction qty
                        qty_out = numeric_parts[-2] if len(numeric_parts) >= 2 else numeric_parts[-1]

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
            import re
            from datetime import datetime as _dt

            pdf_bytes = file.read()

            inserted = 0
            skipped = 0

            bill_errors = []

            # ── Row pattern: DATE  BILL_NO  CUSTOMER  AMOUNT  DIS.  FINAL_AMOUNT
            # Customer names can contain spaces ("SACHINI IMASHA",
            # "THARUSHI WATHSALA") so the customer group is non-greedy
            # and the match anchors on the 3 trailing numeric fields,
            # which are always plain decimals (no currency symbols) in
            # the real easyAcc export.
            BILL_ROW = re.compile(
                r'^(\d{4}/\d{2}/\d{2})\s+'      # 1: date
                r'(\d{5,8})\s+'                  # 2: bill_no
                r'(.+?)\s+'                       # 3: customer
                r'([\d,]+\.\d{2})\s+'            # 4: gross amount
                r'([\d,]+\.\d{2})\s+'            # 5: discount
                r'([\d,]+\.\d{2})\s*$'           # 6: final amount
            )

            # Section headers appear on their own line with no
            # trailing numbers — e.g. "CASH SALE" / "CREDIT SALE".
            # Subtotal lines repeat the same words WITH numbers
            # attached (e.g. "CASH SALE 55.00 325337.87") and must
            # NOT be mistaken for a new section header.
            SECTION_HEADER = re.compile(
                r'^(CASH SALE|CREDIT SALE)\s*$', re.IGNORECASE
            )

            PAYMENT_TYPE_MAP = {
                'CASH SALE':   'CASH',
                'CREDIT SALE': 'CREDIT',
            }

            rows_seen = 0
            current_payment_type = ''

            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:

                for page in pdf.pages:

                    text = page.extract_text() or ''

                    for line in text.split('\n'):

                        line = line.strip()

                        if not line:
                            continue

                        header_match = SECTION_HEADER.match(line)
                        if header_match:
                            current_payment_type = PAYMENT_TYPE_MAP[
                                header_match.group(1).upper()
                            ]
                            continue

                        row_match = BILL_ROW.match(line)
                        if not row_match:
                            # Column header row, subtotal/grand-total
                            # lines, and page numbers all fall through
                            # here harmlessly — none of them match the
                            # strict 6-field bill pattern.
                            continue

                        rows_seen += 1

                        (date_str, bill_no, customer,
                         gross_str, discount_str, final_str) = row_match.groups()

                        try:
                            sale_date = _dt.strptime(
                                date_str, '%Y/%m/%d'
                            ).date()

                            bill_data = {
                                'gross_amount': gross_str.replace(',', ''),
                                'discount':     discount_str.replace(',', ''),
                                'final_amount': final_str.replace(',', ''),
                            }

                            row_errors, row_warnings = validate_bill_row(bill_data)

                            if row_errors:
                                bill_errors.append(
                                    f"Bill {bill_no}: {row_errors}"
                                )
                                skipped += 1
                                continue

                            if row_warnings:
                                # Real, valid bill — just unusual (e.g.
                                # a small final amount). Insert it and
                                # flag it for manager review rather than
                                # discarding it: revenue totals should
                                # still reconcile against the PDF's own
                                # printed figures.
                                bill_errors.append(
                                    f"Bill {bill_no}: {row_warnings} "
                                    f"(inserted, flagged for review)"
                                )

                            DailyBillSummary.objects.create(
                                sale_date=sale_date,
                                bill_no=bill_no,
                                customer_name=customer,
                                gross_amount=float(bill_data['gross_amount']),
                                discount=float(bill_data['discount']),
                                final_amount=float(bill_data['final_amount']),
                                payment_type=current_payment_type,
                                upload=upload_log,
                                is_flagged=bool(row_warnings),
                                is_full_discount=(
                                    float(bill_data['discount'])
                                    >= float(bill_data['gross_amount'])
                                    and float(bill_data['gross_amount']) > 0
                                ),
                            )

                            inserted += 1

                        except Exception as e:

                            skipped += 1

                            bill_errors.append(
                                f'Bill {bill_no}: parse error: {str(e)}'
                            )

            if rows_seen == 0:
                # Nothing matched the bill-row pattern at all — this
                # is a structural extraction failure (wrong format,
                # unexpected layout), NOT a day with zero bills.
                # Marking it SUCCESS here would hide a broken parse
                # behind an indistinguishable "quiet day" result.
                upload_log.status = 'FAILED'
                upload_log.error_message = (
                    'No bill rows could be parsed from this PDF. '
                    'Expected lines like '
                    '"YYYY/MM/DD BILLNO CUSTOMER AMOUNT DIS. FINAL". '
                    'Check the file format matches the easyAcc '
                    'Daily Sales Report layout.'
                )[:2000]
                upload_log.save()

                return Response({
                    'message': 'Daily bills upload failed — no bill rows found',
                    'inserted': 0,
                    'skipped': 0,
                    'upload_log_id': upload_log.id,
                    'errors': [upload_log.error_message],
                }, status=status.HTTP_400_BAD_REQUEST)

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



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def profit_summary(request):
    """
    GET /api/analytics/profit-summary/

    Single summary object for the dashboard profit KPI card.
    Default: last 30 days.

    Response:
        total_revenue           float
        total_cost              float  (WAC-based)
        total_profit            float
        overall_margin_percent  float
        period                  {date_from, date_to}
    """
    raw_to   = request.query_params.get('date_to')
    raw_from = request.query_params.get('date_from')

    try:
        date_to = datetime.strptime(raw_to, '%Y-%m-%d').date() if raw_to else date.today()
    except ValueError:
        return Response({'error': 'Invalid date_to format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        date_from = datetime.strptime(raw_from, '%Y-%m-%d').date() if raw_from else date_to - timedelta(days=30)
    except ValueError:
        return Response({'error': 'Invalid date_from format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

    if date_from > date_to:
        return Response({'error': 'date_from must be before date_to.'}, status=status.HTTP_400_BAD_REQUEST)

    records = ItemSalesRecord.objects.filter(
        sale_date__gte=date_from,
        sale_date__lte=date_to,
    )

    store_totals = records.aggregate(
        total_revenue=Sum('total_amount'),
    )
    total_revenue = float(store_totals['total_revenue'] or 0)

    # WAC cost calculation
    per_product = records.values('product_id').annotate(
        units=Sum('quantity_sold')
    )
    product_ids  = [r['product_id'] for r in per_product]
    products_map = {
        p.id: p for p in Product.objects.filter(id__in=product_ids)
    }

    total_cost = 0.0
    for row in per_product:
        product = products_map.get(row['product_id'])
        if not product:
            continue
        wac = float(product.avg_cost_price or product.cost_price or 0)
        total_cost += row['units'] * wac

    total_profit = total_revenue - total_cost
    margin_pct   = round((total_profit / total_revenue * 100), 2) if total_revenue > 0 else 0.0

    return Response({
        'total_revenue':          round(total_revenue, 2),
        'total_cost':             round(total_cost, 2),
        'total_profit':           round(total_profit, 2),
        'overall_margin_percent': margin_pct,
        'period': {
            'date_from': str(date_from),
            'date_to':   str(date_to),
        }
    })


# F16 — Report Export (API Design Doc v3.1 §21.1)
#
# GET /api/reports/sales/?format=excel|pdf&date_from=&date_to=
# GET /api/reports/profit/?format=excel|pdf&date_from=&date_to=
# GET /api/reports/inventory/?format=excel|pdf
#
# Excel via openpyxl (already in requirements.txt). PDF via reportlab
# (pure Python — no system-level deps like weasyprint needs, safer
# across everyone's machine). ⚠ reportlab is NOT yet in
# requirements.txt — add `reportlab` and run
# `pip install reportlab` before testing this.
#
# Deliberately NOT reusing sales_summary()/profit_summary() directly —
# those are dashboard KPI-card endpoints with an agreed response shape
# (totals + top 5 only, per the Week 4-5 plan with Lavanya). A report
# export needs every product, not top 5, so the per-product rows are
# rebuilt here rather than risk changing that already-agreed contract.
#
# Export events are logged to Upload_Log with upload_type='EXPORT',
# per the note in API Design Doc v3.1 §21.
# =========================================================

def _report_date_range(request):
    """Same default as sales_summary()/profit_summary(): last 30 days."""
    to_str = request.query_params.get('date_to')
    from_str = request.query_params.get('date_from')

    date_to = date.fromisoformat(to_str) if to_str else date.today()
    date_from = date.fromisoformat(from_str) if from_str else date_to - timedelta(days=30)

    if date_from > date_to:
        raise ValueError('date_from must be on or before date_to.')
    return date_from, date_to


def _sales_and_profit_rows(date_from, date_to):
    """
    Per-product breakdown for the date range — ALL products, not top 5.
    Same WAC method as sales_summary()/profit_summary()
    (Product.avg_cost_price, falls back to cost_price).
    """
    records = ItemSalesRecord.objects.filter(
        sale_date__gte=date_from, sale_date__lte=date_to,
    )

    per_product = (
        records
        .values('product_id')
        .annotate(units=Sum('quantity_sold'), revenue=Sum('total_amount'))
        .order_by('-revenue')
    )

    product_ids = [row['product_id'] for row in per_product]
    products_map = {p.id: p for p in Product.objects.filter(id__in=product_ids)}

    rows = []
    for row in per_product:
        product = products_map.get(row['product_id'])
        if not product:
            continue  # orphaned sales record — same guard as sales_summary()

        units = row['units'] or 0
        revenue = float(row['revenue'] or 0)
        wac = float(product.avg_cost_price or product.cost_price or 0)
        cost = units * wac
        profit = revenue - cost
        margin_pct = round(profit / revenue * 100, 1) if revenue > 0 else 0.0

        rows.append({
            'product_name': product.product_name,
            'sku_code': product.sku_code or '',
            'units_sold': units,
            'revenue': round(revenue, 2),
            'cost': round(cost, 2),
            'profit': round(profit, 2),
            'margin_pct': margin_pct,
        })
    return rows


def _inventory_rows():
    """Same current-stock logic as StockSnapshotView (inventory app)."""
    rows = []
    for product in Product.objects.filter(is_active=True):
        current_stock = PurchaseBatch.objects.filter(
            product=product, status='ACTIVE'
        ).aggregate(total=Sum('remaining_quantity'))['total'] or 0

        reorder = product.reorder_threshold or 0
        if current_stock == 0:
            stock_status = 'OUT OF STOCK'
        elif current_stock <= reorder:
            stock_status = 'LOW STOCK'
        else:
            stock_status = 'AVAILABLE'

        rows.append({
            'product_name': product.product_name,
            'sku_code': product.sku_code or '',
            'current_stock': current_stock,
            'reorder_threshold': reorder,
            'stock_status': stock_status,
            'avg_cost_price': str(product.avg_cost_price or 0),
        })
    return rows


def _log_export(request, filename):
    UploadLog.objects.create(
        file_name=filename,
        upload_type='EXPORT',
        status='SUCCESS',
        uploaded_by=request.user.id if request.user and request.user.is_authenticated else None,
    )


def _excel_response(filename, headers, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(list(row))

    for i, header in enumerate(headers, start=1):
        col_letter = ws.cell(row=1, column=i).column_letter
        widths = [len(str(header))] + [len(str(r[i - 1])) for r in rows] if rows else [len(str(header))]
        ws.column_dimensions[col_letter].width = min(max(widths) + 2, 40)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    response = HttpResponse(
        buffer.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _pdf_response(filename, title, headers, rows):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = [Paragraph(title, styles['Title']), Spacer(1, 12)]

    if rows:
        table_data = [headers] + [list(row) for row in rows]
    else:
        table_data = [headers, ['No data for this period'] + [''] * (len(headers) - 1)]

    table = Table(table_data, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f4f4f4')]),
    ]))
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)

    response = HttpResponse(buffer.read(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def sales_report_export(request):
    """GET /api/reports/sales/?format=excel|pdf&date_from=&date_to="""
    fmt = request.query_params.get('format', 'excel').lower()
    if fmt not in ('excel', 'pdf'):
        return Response({'error': 'format must be excel or pdf'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        date_from, date_to = _report_date_range(request)
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    data = _sales_and_profit_rows(date_from, date_to)
    headers = ['Product Name', 'SKU', 'Units Sold', 'Revenue']
    rows = [(r['product_name'], r['sku_code'], r['units_sold'], r['revenue']) for r in data]

    filename_base = f'sales_report_{date_from}_to_{date_to}'
    if fmt == 'excel':
        response = _excel_response(f'{filename_base}.xlsx', headers, rows)
        logged_name = f'{filename_base}.xlsx'
    else:
        title = f'Sales Report ({date_from} to {date_to})'
        response = _pdf_response(f'{filename_base}.pdf', title, headers, rows)
        logged_name = f'{filename_base}.pdf'

    _log_export(request, logged_name)
    return response


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def profit_report_export(request):
    """GET /api/reports/profit/?format=excel|pdf&date_from=&date_to="""
    fmt = request.query_params.get('format', 'excel').lower()
    if fmt not in ('excel', 'pdf'):
        return Response({'error': 'format must be excel or pdf'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        date_from, date_to = _report_date_range(request)
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    data = _sales_and_profit_rows(date_from, date_to)
    headers = ['Product Name', 'SKU', 'Units Sold', 'Revenue', 'Cost', 'Profit', 'Margin %']
    rows = [
        (r['product_name'], r['sku_code'], r['units_sold'], r['revenue'], r['cost'], r['profit'], r['margin_pct'])
        for r in data
    ]

    filename_base = f'profit_report_{date_from}_to_{date_to}'
    if fmt == 'excel':
        response = _excel_response(f'{filename_base}.xlsx', headers, rows)
        logged_name = f'{filename_base}.xlsx'
    else:
        title = f'Profit Report ({date_from} to {date_to})'
        response = _pdf_response(f'{filename_base}.pdf', title, headers, rows)
        logged_name = f'{filename_base}.pdf'

    _log_export(request, logged_name)
    return response


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def inventory_report_export(request):
    """GET /api/reports/inventory/?format=excel|pdf"""
    fmt = request.query_params.get('format', 'excel').lower()
    if fmt not in ('excel', 'pdf'):
        return Response({'error': 'format must be excel or pdf'}, status=status.HTTP_400_BAD_REQUEST)

    data = _inventory_rows()
    headers = ['Product Name', 'SKU', 'Current Stock', 'Reorder Threshold', 'Status', 'Avg Cost Price']
    rows = [
        (r['product_name'], r['sku_code'], r['current_stock'], r['reorder_threshold'], r['stock_status'], r['avg_cost_price'])
        for r in data
    ]

    today = date.today()
    filename_base = f'inventory_report_{today}'
    if fmt == 'excel':
        response = _excel_response(f'{filename_base}.xlsx', headers, rows)
        logged_name = f'{filename_base}.xlsx'
    else:
        response = _pdf_response(f'{filename_base}.pdf', f'Inventory Report ({today})', headers, rows)
        logged_name = f'{filename_base}.pdf'

    _log_export(request, logged_name)
    return response

# ─────────────────────────────────────────────────────────────────
# GET /api/reports/health-scores/?format=excel|pdf&status=
#
# No date range — health scores are calculated on-demand (not
# date-windowed), same as GET /api/health-scores/. Same status
# filter and same ordering (-calculated_date, overall_score) as
# HealthScoreListView, for consistency with that existing endpoint.
# ─────────────────────────────────────────────────────────────────
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def health_score_report_export(request):
    fmt = request.query_params.get('format', 'excel').lower()
    if fmt not in ('excel', 'pdf'):
        return Response({'error': 'format must be excel or pdf'}, status=status.HTTP_400_BAD_REQUEST)

    # Latest-per-product dedup — same Subquery pattern as HealthScoreListView,
    # kept in sync after Nipuni's fix (PR #11) so this export doesn't show
    # the pre-fix 9x-inflated duplicate rows.
    latest_ids = (
        InventoryHealthScore.objects
        .filter(product_id=OuterRef('product_id'))
        .order_by('-calculated_date', '-id')
        .values('id')[:1]
    )
    queryset = InventoryHealthScore.objects.filter(
        id__in=Subquery(latest_ids)
    ).select_related('product').order_by('overall_score')

    status_filter = request.query_params.get('status')
    if status_filter:
        queryset = queryset.filter(status=status_filter)

    headers = [
        'Product Name', 'SKU', 'Velocity', 'Margin', 'Expiry Risk',
        'Stock Duration', 'Rating', 'Overall Score', 'Status',
        'Recommended Action', 'Calculated Date',
    ]
    rows = [
        (
            r.product.product_name, r.product.sku_code or '',
            str(r.velocity_score), str(r.margin_score), str(r.expiry_risk_score),
            str(r.stock_duration_score),
            str(r.rating_score) if r.rating_score is not None else 'N/A',
            str(r.overall_score), r.status, r.recommended_action or '',
            str(r.calculated_date),
        )
        for r in queryset
    ]

    filename_base = f'health_score_report_{date.today()}'
    if fmt == 'excel':
        response = _excel_response(f'{filename_base}.xlsx', headers, rows)
        logged_name = f'{filename_base}.xlsx'
    else:
        response = _pdf_response(f'{filename_base}.pdf', f'Health Score Report ({date.today()})', headers, rows)
        logged_name = f'{filename_base}.pdf'

    _log_export(request, logged_name)
    return response

# ─────────────────────────────────────────────────────────────────
# GET /api/reports/supplier/?format=excel|pdf
#
# Reuses _compute_scorecard() from suppliers app directly — same
# logic already tested via GET /api/suppliers/scorecard-summary/,
# not reimplemented here. Missing components (no data yet for that
# supplier) show as 'N/A', same convention as the Health Score export.
# ─────────────────────────────────────────────────────────────────
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def supplier_report_export(request):
    fmt = request.query_params.get('format', 'excel').lower()
    if fmt not in ('excel', 'pdf'):
        return Response({'error': 'format must be excel or pdf'}, status=status.HTTP_400_BAD_REQUEST)

    scores = [_compute_scorecard(s) for s in Supplier.objects.all()]
    scores.sort(key=lambda s: (s['overall_score'] is None, -(s['overall_score'] or 0)))

    headers = [
        'Supplier Name', 'Delivery Accuracy', 'Price Stability',
        'Return Acceptance Rate', 'Avg Product Quality', 'Overall Score',
    ]
    rows = []
    for s in scores:
        c = s['components']
        rows.append((
            s['supplier_name'],
            c.get('delivery_accuracy', 'N/A'),
            c.get('price_stability', 'N/A'),
            c.get('return_acceptance_rate', 'N/A'),
            c.get('avg_product_quality', 'N/A'),
            s['overall_score'] if s['overall_score'] is not None else 'N/A',
        ))

    filename_base = f'supplier_report_{date.today()}'
    if fmt == 'excel':
        response = _excel_response(f'{filename_base}.xlsx', headers, rows)
        logged_name = f'{filename_base}.xlsx'
    else:
        response = _pdf_response(f'{filename_base}.pdf', f'Supplier Performance Report ({date.today()})', headers, rows)
        logged_name = f'{filename_base}.pdf'

    _log_export(request, logged_name)
    return response

# ─────────────────────────────────────────────────────────────────
# GET /api/reports/lifecycle/?format=excel|pdf&status=
#
# Reuses get_latest_lifecycle() from inventory.services.lifecycle
# directly — same function backing GET /api/lifecycle/ and
# GET /api/lifecycle/declining/, not reimplemented here. Already
# de-duplicated to one record per product (Nipuni's Subquery fix).
# ─────────────────────────────────────────────────────────────────
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def lifecycle_report_export(request):
    fmt = request.query_params.get('format', 'excel').lower()
    if fmt not in ('excel', 'pdf'):
        return Response({'error': 'format must be excel or pdf'}, status=status.HTTP_400_BAD_REQUEST)

    status_filter = request.query_params.get('status')
    data = get_latest_lifecycle(status_filter=status_filter)

    headers = ['Product Name', 'Status', 'Sales Velocity', 'Recommendation', 'Calculated Date']
    rows = [
        (r['product_name'], r['status'], r['sales_velocity'], r['recommendation'], r['calculated_date'])
        for r in data
    ]

    filename_base = f'lifecycle_report_{date.today()}'
    if fmt == 'excel':
        response = _excel_response(f'{filename_base}.xlsx', headers, rows)
        logged_name = f'{filename_base}.xlsx'
    else:
        response = _pdf_response(f'{filename_base}.pdf', f'Product Lifecycle Report ({date.today()})', headers, rows)
        logged_name = f'{filename_base}.pdf'

    _log_export(request, logged_name)
    return response

# ─────────────────────────────────────────────────────────────────
# GET /api/reports/loss/?format=excel|pdf&date_from=&date_to=
#
# Lists individual loss records for the date range (same filter fields
# as LossRecordView/LossSummaryView in inventory app). This is the
# detailed row-level export — aggregate totals (gross_expiry_loss,
# recovered_amount, net_loss) already exist separately via
# GET /api/losses/summary/, not duplicated here.
# ─────────────────────────────────────────────────────────────────
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def loss_report_export(request):
    fmt = request.query_params.get('format', 'excel').lower()
    if fmt not in ('excel', 'pdf'):
        return Response({'error': 'format must be excel or pdf'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        date_from, date_to = _report_date_range(request)
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    queryset = LossRecord.objects.select_related('product').filter(
        loss_date__gte=date_from, loss_date__lte=date_to,
    ).order_by('-loss_date')

    headers = ['Product Name', 'SKU', 'Loss Type', 'Quantity', 'Loss Value', 'Loss Date', 'Notes']
    rows = [
        (
            r.product.product_name, r.product.sku_code or '',
            r.loss_type, r.loss_quantity, str(r.loss_value),
            str(r.loss_date), r.notes or '',
        )
        for r in queryset
    ]

    filename_base = f'loss_report_{date_from}_to_{date_to}'
    if fmt == 'excel':
        response = _excel_response(f'{filename_base}.xlsx', headers, rows)
        logged_name = f'{filename_base}.xlsx'
    else:
        title = f'Loss Report ({date_from} to {date_to})'
        response = _pdf_response(f'{filename_base}.pdf', title, headers, rows)
        logged_name = f'{filename_base}.pdf'

    _log_export(request, logged_name)
    return response

# ─────────────────────────────────────────────────────────────────
# GET /api/reports/reorder/?format=excel|pdf&urgency=&status=
#
# Deliberately does its OWN "latest per product" dedup here (same
# Subquery pattern as lifecycle_analytics()), independent of whatever
# write-side/read-side fix Nipuni applies to ReorderCalculateView /
# ReorderRecommendationListView. This way the export is correct
# regardless of which fix lands — if the write side gets fixed later,
# deduping an already-clean table is harmless; if it doesn't, this
# export still shows accurate current data.
#
# "Latest per product" here means the most recent recommendation
# regardless of status — so an already-actioned (ORDERED/IGNORED)
# recommendation correctly stays visible until a newer calculation
# run replaces it, rather than showing a stale duplicate.
# ─────────────────────────────────────────────────────────────────
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def reorder_report_export(request):
    fmt = request.query_params.get('format', 'excel').lower()
    if fmt not in ('excel', 'pdf'):
        return Response({'error': 'format must be excel or pdf'}, status=status.HTTP_400_BAD_REQUEST)

    latest_ids = (
        ReorderRecommendation.objects
        .filter(product_id=OuterRef('product_id'))
        .order_by('-calculation_date', '-id')
        .values('id')[:1]
    )
    queryset = ReorderRecommendation.objects.filter(
        id__in=Subquery(latest_ids)
    ).select_related('product', 'supplier').order_by('product__product_name')

    urgency_filter = request.query_params.get('urgency')
    status_filter = request.query_params.get('status')
    if urgency_filter:
        queryset = queryset.filter(urgency=urgency_filter)
    if status_filter:
        queryset = queryset.filter(status=status_filter)

    headers = [
        'Product Name', 'SKU', 'Supplier', 'Current Stock', 'Avg Daily Sales',
        'Days of Stock', 'Suggested Qty', 'Estimated Cost', 'Urgency',
        'Status', 'Calculation Date',
    ]
    rows = [
        (
            r.product.product_name, r.product.sku_code or '',
            r.supplier.supplier_name if r.supplier else 'N/A',
            r.current_stock, str(r.avg_daily_sales), r.days_of_stock,
            r.suggested_quantity, str(r.estimated_cost), r.urgency,
            r.status, str(r.calculation_date),
        )
        for r in queryset
    ]

    filename_base = f'reorder_report_{date.today()}'
    if fmt == 'excel':
        response = _excel_response(f'{filename_base}.xlsx', headers, rows)
        logged_name = f'{filename_base}.xlsx'
    else:
        response = _pdf_response(f'{filename_base}.pdf', f'Reorder Recommendations Report ({date.today()})', headers, rows)
        logged_name = f'{filename_base}.pdf'

    _log_export(request, logged_name)
    return response