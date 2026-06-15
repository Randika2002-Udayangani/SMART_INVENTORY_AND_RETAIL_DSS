
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, generics
from rest_framework.parsers import MultiPartParser, FormParser

from django.utils import timezone

from products.models import Product
from users.models import SystemConfig

from .models import (
    UploadLog,
    DailyBillSummary,
    ItemSalesRecord
)

from .serializers import (
    UploadLogSerializer,
    DailyBillSerializer,
    ItemSalesSerializer
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
            '-sale_date'
        )

        product = self.request.query_params.get('product')

        if product:
            queryset = queryset.filter(
                product__id=product
            )

        return queryset


# =========================================================
# DAILY BILL APIs
# =========================================================

class DailyBillListView(generics.ListAPIView):

    serializer_class = DailyBillSerializer

    def get_queryset(self):

        queryset = DailyBillSummary.objects.all().order_by(
            '-sale_date'
        )

        bill_no = self.request.query_params.get('bill_no')

        if bill_no:
            queryset = queryset.filter(
                bill_no__icontains=bill_no
            )

        return queryset

