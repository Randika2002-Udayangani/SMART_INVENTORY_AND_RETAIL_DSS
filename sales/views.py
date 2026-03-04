from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, generics
from rest_framework.parsers import MultiPartParser, FormParser
from django.utils import timezone
import pandas as pd
from products.models import Product
from users.models import SystemConfig
from .models import UploadLog, DailyBillSummary, ItemSalesRecord
from .serializers import UploadLogSerializer, DailyBillSerializer, ItemSalesSerializer


def validate_bill_row(row):
    """Validate a single bill row from the daily bills data."""
    errors = []
    try:
        final = float(row.get('Final Amount', 0) or 0)
        discount = float(row.get('Amount', 0) or 0)
    except (ValueError, TypeError):
        return ['Could not parse amount values']

    if final <= 0:
        errors.append(f"Non-positive final amount: {final}")
    if discount > final and final > 0:
        errors.append(f"Discount ({discount}) exceeds final amount — possible credit note")
    if 0 < final < 10:
        errors.append(f"Suspiciously small amount: {final} — possible artifact")
    return errors


# ─────────────────────────────────────────────────────────────────
# Pipeline 5 — Item Ledger PDF Upload
# POST /api/sales/upload/item-ledger/
# ─────────────────────────────────────────────────────────────────
class ItemLedgerPDFUploadView(APIView):
    """
    POST /api/sales/upload/item-ledger/
    Upload Item Ledger PDF (item.pdf from easyAcc).

    File structure (confirmed from real item.pdf — 59 pages):
      - Product name on each page: "Item No : RATTHI 400g"
      - Table columns: Date | Bill No | Customer | Bill Type | IN | OUT | Balance
      - Date format: YYYY/MM/DD
      - Only rows where Bill Type contains "CASH SALE" are processed
      - OPENING STOCK rows are skipped
      - Multiple bills per day are aggregated into one daily total
      - Unit price is NOT in this file — looked up from Product.unit_price

    Result: One Item_Sales_Record row per date for this product.
    """
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        file = request.FILES.get('file')

        if not file:
            return Response(
                {'error': 'No file uploaded. Send file as form-data with key "file"'},
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
            daily_totals = defaultdict(float)  # {date: total_qty_sold}

            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page_num, page in enumerate(pdf.pages):
                    text = page.extract_text()
                    if not text:
                        continue

                    # ── Extract product name from "Item No : RATTHI 400g" ──
                    if product is None:
                        for line in text.split('\n'):
                            if 'Item No' in line and ':' in line:
                                product_name = line.split(':', 1)[1].strip()
                                # PDF sometimes has "2117 RATTHI 400g" but
                                # DB has "RATTHI 400g" — strip leading number
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
                                        f'Product not found: "{product_name}". '
                                        f'Upload Item Master first.'
                                    )
                                    upload_log.save()
                                    return Response(
                                        {
                                            'error': (
                                                f'Product "{product_name}" not in Product table. '
                                                f'Upload Item Master Excel first.'
                                            )
                                        },
                                        status=status.HTTP_400_BAD_REQUEST
                                    )
                                break

                    # ── Extract table rows ────────────────────────────────
                    tables = page.extract_tables()
                    for table in tables:
                        for row in table:
                            if not row or len(row) < 6:
                                continue

                            date_val  = str(row[0] or '').strip()
                            bill_type = str(row[3] or '').strip()
                            out_val   = str(row[5] or '').strip()

                            # Skip header rows
                            if not date_val or date_val == 'Date':
                                continue

                            # Only process CASH SALE rows
                            # Skip OPENING STOCK and any other types
                            if 'CASH SALE' not in bill_type:
                                continue

                            # Parse date — format is YYYY/MM/DD
                            try:
                                sale_date = datetime.strptime(
                                    date_val, '%Y/%m/%d'
                                ).date()
                            except ValueError:
                                errors.append(
                                    f'Page {page_num + 1}: '
                                    f'Cannot parse date "{date_val}" — skipped'
                                )
                                continue

                            # Parse OUT quantity (3 decimal places e.g. 1.000)
                            try:
                                qty_out = float(out_val) if out_val else 0.0
                            except ValueError:
                                qty_out = 0.0

                            if qty_out > 0:
                                daily_totals[sale_date] += qty_out

            # ── Product not found in PDF at all ──────────────────────────
            if product is None:
                upload_log.status = 'FAILED'
                upload_log.error_message = (
                    'Could not find "Item No :" line in PDF. '
                    'Check that this is a valid easyAcc item ledger PDF.'
                )
                upload_log.save()
                return Response(
                    {'error': 'Could not extract product name from PDF'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # ── Insert one Item_Sales_Record per date ─────────────────────
            inserted = 0
            skipped  = 0
            unit_price = float(product.unit_price)

            for sale_date, total_qty in sorted(daily_totals.items()):
                qty = int(total_qty)
                if qty <= 0:
                    continue

                # Duplicate guard — skip if record already exists
                already_exists = ItemSalesRecord.objects.filter(
                    product=product,
                    sale_date=sale_date
                ).exists()

                if already_exists:
                    skipped += 1
                    errors.append(
                        f'{sale_date}: Record already exists for '
                        f'"{product.product_name}" — skipped'
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

            # ── Update system config sync date ────────────────────────────
            SystemConfig.objects.update_or_create(
                key='last_item_ledger_sync',
                defaults={
                    'value': str(timezone.now().date()),
                    'description': 'Last item ledger PDF sync date'
                }
            )

            # ── Finalise log ──────────────────────────────────────────────
            if inserted == 0 and skipped == 0:
                upload_log.status = 'FAILED'
            elif errors:
                upload_log.status = 'PARTIAL'
            else:
                upload_log.status = 'SUCCESS'

            upload_log.error_message = '\n'.join(errors)[:2000]
            upload_log.save()

            return Response({
                'message'        : 'Item Ledger PDF upload complete',
                'product'        : product.product_name,
                'dates_inserted' : inserted,
                'dates_skipped'  : skipped,
                'upload_log_id'  : upload_log.id,
                'errors'         : errors[:10]
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            upload_log.status = 'FAILED'
            upload_log.error_message = str(e)[:2000]
            upload_log.save()
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


# ─────────────────────────────────────────────────────────────────
# Pipeline 2 — Daily Bills PDF Upload
# POST /api/sales/upload/daily-bills/
# ─────────────────────────────────────────────────────────────────
class DailyBillsUploadView(APIView):
    """
    POST /api/sales/upload/daily-bills/
    Upload Daily Bill Summary PDF.
    NOTE: Amount column = discount. Final Amount = actual revenue.
    """
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
            inserted    = 0
            skipped     = 0
            bill_errors = []

            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    tables = page.extract_tables()
                    for table in tables:
                        for row in table[1:]:  # skip header row
                            if not row or len(row) < 4:
                                continue
                            try:
                                bill_data = {
                                    'Date'        : row[0],
                                    'Bill No'     : row[1],
                                    'Customer'    : row[2],
                                    'Amount'      : row[3],
                                    'Final Amount': row[4] if len(row) > 4 else 0,
                                }

                                row_errors = validate_bill_row(bill_data)
                                if row_errors:
                                    bill_errors.append(
                                        f"Bill {bill_data['Bill No']}: {row_errors}"
                                    )
                                    skipped += 1
                                    continue

                                DailyBillSummary.objects.create(
                                    sale_date=bill_data['Date'],
                                    bill_no=str(bill_data['Bill No']),
                                    customer_name=str(bill_data['Customer'] or ''),
                                    # Amount = discount, Final Amount = revenue
                                    discount=float(bill_data['Amount'] or 0),
                                    final_amount=float(bill_data['Final Amount'] or 0),
                                    gross_amount=(
                                        float(bill_data['Amount'] or 0) +
                                        float(bill_data['Final Amount'] or 0)
                                    ),
                                    upload=upload_log,
                                    is_flagged=len(row_errors) > 0
                                )
                                inserted += 1

                            except Exception as e:
                                skipped += 1
                                bill_errors.append(f'Row parse error: {str(e)}')

            upload_log.status = 'SUCCESS' if skipped == 0 else 'PARTIAL'
            upload_log.error_message = '\n'.join(bill_errors)[:2000]
            upload_log.save()

            return Response({
                'message'       : 'Daily bills upload complete',
                'inserted'      : inserted,
                'skipped'       : skipped,
                'upload_log_id' : upload_log.id,
                'errors'        : bill_errors[:10]
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            upload_log.status = 'FAILED'
            upload_log.error_message = str(e)[:2000]
            upload_log.save()
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


# ─────────────────────────────────────────────────────────────────
# Upload Log
# ─────────────────────────────────────────────────────────────────
class UploadLogListView(generics.ListAPIView):
    """GET /api/sales/upload-log/"""
    queryset = UploadLog.objects.all().order_by('-upload_date')
    serializer_class = UploadLogSerializer


class UploadLogDetailView(generics.RetrieveAPIView):
    """GET /api/sales/upload-log/{id}/"""
    queryset = UploadLog.objects.all()
    serializer_class = UploadLogSerializer


# ─────────────────────────────────────────────────────────────────
# Item Sales Records
# ─────────────────────────────────────────────────────────────────
class ItemSalesListView(generics.ListAPIView):
    """GET /api/sales/item-sales/"""
    serializer_class = ItemSalesSerializer

    def get_queryset(self):
        queryset = ItemSalesRecord.objects.all().order_by('-sale_date')
        product = self.request.query_params.get('product')
        if product:
            queryset = queryset.filter(product__id=product)
        return queryset