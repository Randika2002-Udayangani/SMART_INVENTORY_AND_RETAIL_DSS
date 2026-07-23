import re
import datetime
from decimal import Decimal, InvalidOperation

from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser
from django.utils import timezone
from django.db import transaction
from datetime import timedelta

from .models import Purchase, PurchaseBatch
from users.audit import log_action
from .serializers import (
    PurchaseSerializer, PurchaseCreateSerializer, PurchaseBatchSerializer
)
from suppliers.models import Supplier
from products.models import Product
from sales.models import UploadLog


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
            output = PurchaseSerializer(purchase)

            log_action(
                user=request.user, action='CREATE', table_name='purchase',
                record_id=purchase.id, old_value=None,
                new_value=output.data, request=request,
            )

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
    Filter by: ?status=ACTIVE|EXPIRED|DEPLETED|DISPOSED|PENDING_EXPIRY
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
    Valid statuses: ACTIVE, EXPIRED, DEPLETED, DISPOSED, PENDING_EXPIRY
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
        valid_statuses = ['ACTIVE', 'EXPIRED', 'DEPLETED', 'DISPOSED', 'PENDING_EXPIRY']

        if new_status not in valid_statuses:
            return Response(
                {'error': f'Status must be one of {valid_statuses}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        old_status = batch.status
        batch.status = new_status
        batch.save()

        log_action(
            user=request.user, action='UPDATE', table_name='purchase_batch',
            record_id=batch.id,
            old_value={'status': old_status},
            new_value={'status': batch.status},
            request=request,
        )

        return Response({
            'message'  : f'Batch {batch.id} status updated',
            'id'       : batch.id,
            'product'  : batch.product.product_name if batch.product else None,
            'status'   : batch.status
        })


# =========================================================
# PURCHASE INVOICE PDF UPLOAD
# POST /api/purchases/upload/invoice/
#
# Implements Pipeline 4 per Data_Ingestion_Rules_v3.pdf Section 11.
# Rules R1-R9 as confirmed by Randika from the source document:
#
#   R1  Supplier must exact-match Supplier table       -> ABORT file, LOG_ERROR
#   R2  Duplicate (supplier, invoice_number)            -> REJECT, show existing
#   R3  Invoice date missing/unparseable/future          -> REJECT, LOG_ERROR
#   R4  Cost price <= 0 on a line                        -> SKIP line, LOG_ERROR
#   R5  Qty <= 0 on a line                                -> SKIP line, LOG_ERROR
#   R6  |CostTotal - UnitCost*Qty| > 0.05                 -> LOG_WARNING,
#                                                             use computed value
#   R7  Product name unmatched                           -> create batch with
#                                                             product=NULL,
#                                                             FLAG for review
#                                                             (does NOT abort file)
#   R8  Invoice selling price != Product.unit_price      -> LOG_WARNING, alert
#                                                             staff, do NOT
#                                                             auto-update price
#   R9  Expiry date (never present in this invoice        -> batch created with
#       format)                                              status='PENDING_EXPIRY',
#                                                             excluded from active
#                                                             stock until staff
#                                                             enters expiry
#
# ADDITION BEYOND SPEC (flagged for Randika's awareness):
#   Before falling through to R7's product=NULL path, a narrow truncation
#   auto-correction is attempted (see _try_resolve_truncated_product). It
#   only fires when a description matches the specific "digits + trailing
#   period" pattern (e.g. "...17." meaning "...170g") AND exactly one
#   Product uniquely matches the prefix. This is strictly safer than R7's
#   fallback -- it reduces how often product=NULL/staff-review is needed,
#   without ever guessing among ambiguous candidates. If Randika wants R7
#   applied strictly with no auto-correction, remove the call to
#   _try_resolve_truncated_product() below.
#
# Requires model changes (see model_changes_required.txt):
#   - PurchaseBatch.STATUS_CHOICES: add ('PENDING_EXPIRY', 'Pending Expiry')
#   - PurchaseBatch.product: add null=True, blank=True
#   - UploadLog.UPLOAD_TYPES: add ('SUPPLIER_INVOICE', 'Supplier Invoice PDF')
# =========================================================

# Matches: Bill No : 0002147 Supplier : UNILEVER
_BILL_SUPPLIER_PATTERN = re.compile(r'Bill No\s*:\s*(\S+)\s+Supplier\s*:\s*(.+)')

# Matches: Date : 15-Jan-2025 Invoice No :
_DATE_PATTERN = re.compile(r'Date\s*:\s*(\d{1,2}-\w{3}-\d{4})')

# Matches an item line:
#   1369 SIGNAL DEEP CLEAN DBL PACK 24.00 0.00 236.96 5,687.04 24.00 280.00 6,720.00
#   item_code  description(non-greedy)  qty  free_qty  cost_unit  cost_total  sell_qty  sell_unit  sell_total
_ITEM_LINE_PATTERN = re.compile(
    r'^(\d{3,6})\s+(.+?)\s+'
    r'([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+'
    r'([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})$'
)

_SKIP_LINE_KEYWORDS = ('Gross Total', 'Discount', 'Net Total', 'Loading Charge',
                        'Other Charge', 'Return', 'Item Description', 'No Qty')

# R6 tolerance for cost total cross-check
_COST_TOTAL_TOLERANCE = Decimal('0.05')

# Detects descriptions truncated by the invoice's own layout, e.g.
# "RITZBURY PEBBALS PEANUT PARTY TIME 17." (should be "...170g").
_TRUNCATED_DESCRIPTION_PATTERN = re.compile(r'\d+\.$')


def _parse_decimal(text):
    """Strip thousands-separator commas and convert to Decimal."""
    try:
        clean = text.replace(',', '')
        return Decimal(clean)
    except InvalidOperation:
        return None


def _try_resolve_truncated_product(description):
    """
    Narrow, safe fallback before R7's product=NULL path. Only fires on the
    specific truncation signature (digits + trailing period), and only
    auto-applies when exactly one Product matches the stripped prefix.
    Returns the matched Product, or None (falls through to R7).
    """
    if not _TRUNCATED_DESCRIPTION_PATTERN.search(description):
        return None
    prefix = description.rstrip('.').strip()
    if not prefix:
        return None
    candidates = list(Product.objects.filter(product_name__istartswith=prefix))
    if len(candidates) == 1:
        return candidates[0]
    return None


class PurchaseInvoicePDFUploadView(APIView):
    """
    Upload a single supplier invoice PDF. Implements Pipeline 4 rules
    R1-R9 from Data_Ingestion_Rules_v3.pdf Section 11.
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
            upload_type='SUPPLIER_INVOICE',
            status='PARTIAL',
            error_message='',
        )

        try:
            import pdfplumber
            import io

            pdf_bytes = file.read()

            bill_no = None
            supplier_name = None
            purchase_date = None
            item_lines = []

            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if not text:
                        continue

                    for line in text.split('\n'):
                        line = line.strip()
                        if not line:
                            continue

                        if bill_no is None:
                            m = _BILL_SUPPLIER_PATTERN.search(line)
                            if m:
                                bill_no = m.group(1).strip()
                                supplier_name = m.group(2).strip()
                                continue

                        if purchase_date is None:
                            m = _DATE_PATTERN.search(line)
                            if m:
                                try:
                                    purchase_date = datetime.datetime.strptime(
                                        m.group(1), '%d-%b-%Y'
                                    ).date()
                                except ValueError:
                                    pass  # R3 handles this below (still None)
                                continue

                        if any(kw in line for kw in _SKIP_LINE_KEYWORDS):
                            continue

                        m = _ITEM_LINE_PATTERN.match(line)
                        if m:
                            item_code, desc, qty, free_qty, cost_unit, cost_total, \
                                sell_qty, sell_unit, sell_total = m.groups()
                            item_lines.append({
                                'item_code': item_code,
                                'description': desc.strip(),
                                'qty': _parse_decimal(qty),
                                'cost_unit': _parse_decimal(cost_unit),
                                'cost_total_stated': _parse_decimal(cost_total),
                                'sell_unit': _parse_decimal(sell_unit),
                                'raw_line': line,
                            })

            # ── Header validation ────────────────────────────────────────────
            if not bill_no or not supplier_name:
                upload_log.status = 'FAILED'
                upload_log.error_message = 'Could not extract Bill No / Supplier from PDF header'
                upload_log.save()
                return Response(
                    {'error': upload_log.error_message},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # ── R3: date missing / unparseable / future -> REJECT, LOG_ERROR ──
            today = datetime.date.today()
            if not purchase_date:
                upload_log.status = 'FAILED'
                upload_log.error_message = 'R3: Invoice date missing or unparseable'
                upload_log.save()
                return Response(
                    {'error': 'R3 violation: could not extract a valid purchase date from PDF'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if purchase_date > today:
                upload_log.status = 'FAILED'
                upload_log.error_message = f'R3: Invoice date {purchase_date} is in the future'
                upload_log.save()
                return Response(
                    {'error': f'R3 violation: invoice date {purchase_date} is in the future'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if not item_lines:
                upload_log.status = 'FAILED'
                upload_log.error_message = 'No item lines could be parsed from this invoice'
                upload_log.save()
                return Response(
                    {'error': 'No item lines could be parsed from this invoice'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # ── R1: supplier exact match, ABORT file if not found ─────────────
            try:
                supplier = Supplier.objects.get(supplier_name__iexact=supplier_name)
            except Supplier.DoesNotExist:
                upload_log.status = 'FAILED'
                upload_log.error_message = f'R1: Supplier "{supplier_name}" not found'
                upload_log.save()
                return Response(
                    {'error': f'R1 violation: Supplier "{supplier_name}" not found. '
                              f'Staff must create this supplier record first via POST /api/suppliers/'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            except Supplier.MultipleObjectsReturned:
                upload_log.status = 'FAILED'
                upload_log.error_message = f'Multiple suppliers matched "{supplier_name}"'
                upload_log.save()
                return Response(
                    {'error': f'Multiple suppliers matched "{supplier_name}" - ambiguous, needs manual resolution'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # ── R2: duplicate invoice (supplier + invoice_number) -> REJECT ────
            existing = Purchase.objects.filter(
                supplier=supplier, invoice_number=bill_no
            ).first()
            if existing:
                upload_log.status = 'FAILED'
                upload_log.error_message = f'R2: Duplicate invoice {bill_no} for {supplier_name}'
                upload_log.save()
                return Response(
                    {
                        'error': f'R2 violation: invoice {bill_no} for {supplier_name} already exists',
                        'existing_purchase_id': existing.id,
                        'existing_purchase_date': str(existing.purchase_date),
                        'existing_total_amount': str(existing.total_amount),
                    },
                    status=status.HTTP_409_CONFLICT
                )

            # ── Process line items ─────────────────────────────────────────────
            inserted = []
            auto_corrected = []
            flagged_for_review = []   # R7: product=NULL, batch still created
            skipped = []              # R4/R5: line dropped entirely
            warnings = []             # R6/R8: logged, doesn't block anything
            total_amount = Decimal('0')

            with transaction.atomic():
                purchase = Purchase.objects.create(
                    supplier=supplier,
                    purchase_date=purchase_date,
                    invoice_number=bill_no,
                    total_amount=Decimal('0'),
                )

                for line in item_lines:
                    product_name = line['description']
                    qty = line['qty']
                    cost_unit = line['cost_unit']
                    cost_total_stated = line['cost_total_stated']
                    sell_unit = line['sell_unit']

                    # ── R5: qty <= 0 -> SKIP line, LOG_ERROR ───────────────────
                    if qty is None or qty <= 0:
                        skipped.append({
                            'item_code': line['item_code'],
                            'description': product_name,
                            'reason': f'R5: qty <= 0 or unparseable (qty={qty})',
                        })
                        continue

                    # ── R4: cost price <= 0 -> SKIP line, LOG_ERROR ────────────
                    if cost_unit is None or cost_unit <= 0:
                        skipped.append({
                            'item_code': line['item_code'],
                            'description': product_name,
                            'reason': f'R4: cost price <= 0 or unparseable (cost={cost_unit})',
                        })
                        continue

                    # ── R6: cost total cross-check -> LOG_WARNING, use computed ─
                    computed_total = (cost_unit * qty).quantize(Decimal('0.01'))
                    final_cost_price = cost_unit  # always use computed unit cost as batch basis
                    if cost_total_stated is not None:
                        diff = abs(computed_total - cost_total_stated)
                        if diff > _COST_TOTAL_TOLERANCE:
                            warnings.append({
                                'item_code': line['item_code'],
                                'description': product_name,
                                'rule': 'R6',
                                'message': f'Stated cost total {cost_total_stated} differs from '
                                           f'computed (qty*unit={computed_total}) by {diff} '
                                           f'- using computed value',
                            })

                    # ── Product matching: exact match, then narrow auto-correct,
                    #     then R7 fallback (product=NULL, flagged) ────────────────
                    product = None
                    was_auto_corrected = False

                    try:
                        product = Product.objects.get(product_name__iexact=product_name)
                    except Product.DoesNotExist:
                        resolved = _try_resolve_truncated_product(product_name)
                        if resolved is not None:
                            product = resolved
                            was_auto_corrected = True
                    except Product.MultipleObjectsReturned:
                        pass  # ambiguous -> falls through to R7 below

                    # ── R8: selling price differs from Product.unit_price ──────
                    if product is not None and sell_unit is not None:
                        current_unit_price = product.unit_price
                        if current_unit_price and abs(sell_unit - current_unit_price) > Decimal('0.01'):
                            warnings.append({
                                'item_code': line['item_code'],
                                'description': product_name,
                                'rule': 'R8',
                                'message': f'Invoice selling price {sell_unit} differs from '
                                           f'Product.unit_price {current_unit_price} - '
                                           f'NOT auto-updated, staff review needed',
                            })

                    # ── R9: expiry never present -> PENDING_EXPIRY status ───────
                    batch = PurchaseBatch.objects.create(
                        purchase=purchase,
                        product=product,  # may be None -> R7
                        quantity_received=int(qty),
                        cost_price=final_cost_price,
                        expiry_date=None,
                        remaining_quantity=int(qty),
                        status='PENDING_EXPIRY',
                    )

                    entry = {
                        'item_code': line['item_code'],
                        'description': product_name,
                        'product': product.product_name if product else None,
                        'quantity_received': int(qty),
                        'cost_price': str(final_cost_price),
                        'batch_id': batch.id,
                        'batch_status': 'PENDING_EXPIRY',
                    }

                    if product is None:
                        # R7: product unmatched, batch created with product=NULL, flagged
                        entry['reason'] = 'R7: product name unmatched - flagged for staff review'
                        flagged_for_review.append(entry)
                        continue

                    # Update Product cost (NOT full WAC -- see TODO)
                    product.cost_price = final_cost_price
                    if not product.avg_cost_price:
                        product.avg_cost_price = final_cost_price
                    product.save(update_fields=['cost_price', 'avg_cost_price'])

                    total_amount += final_cost_price * qty

                    if was_auto_corrected:
                        entry['auto_corrected_from'] = product_name
                        auto_corrected.append(entry)
                    else:
                        inserted.append(entry)

                purchase.total_amount = total_amount
                purchase.save(update_fields=['total_amount'])

            # ── Finalize UploadLog ──────────────────────────────────────────────
            log_notes = []
            for s in skipped:
                log_notes.append(f"SKIPPED [{s['item_code']}] {s['reason']}")
            for f in flagged_for_review:
                log_notes.append(f"FLAGGED [{f['item_code']}] {f['reason']}")
            for w in warnings:
                log_notes.append(f"WARNING [{w['rule']}] [{w['item_code']}] {w['message']}")

            if skipped or flagged_for_review or warnings:
                upload_log.status = 'PARTIAL'
            else:
                upload_log.status = 'SUCCESS'
            upload_log.error_message = '\n'.join(log_notes)[:2000]
            upload_log.save()

            return Response({
                'message': 'Purchase invoice PDF upload complete',
                'upload_log_id': upload_log.id,
                'supplier': supplier.supplier_name,
                'invoice_number': bill_no,
                'purchase_date': str(purchase_date),
                'purchase_id': purchase.id,
                'total_amount': str(total_amount),
                'batches_created': len(inserted) + len(auto_corrected) + len(flagged_for_review),
                'inserted_count': len(inserted),
                'auto_corrected_count': len(auto_corrected),
                'flagged_for_review_count': len(flagged_for_review),
                'lines_skipped_count': len(skipped),
                'warnings_count': len(warnings),
                'inserted': inserted,
                'auto_corrected': auto_corrected,
                'flagged_for_review': flagged_for_review,
                'skipped': skipped,
                'warnings': warnings,
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            upload_log.status = 'FAILED'
            upload_log.error_message = str(e)[:2000]
            upload_log.save()
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )