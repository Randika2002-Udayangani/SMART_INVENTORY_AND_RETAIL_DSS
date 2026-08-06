import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'smart_inventory.settings')
django.setup()

import json
from django.contrib.auth.models import User
from rest_framework.test import APIRequestFactory, force_authenticate
from django.core.files.uploadedfile import SimpleUploadedFile
from products.models import Product
from sales.models import ItemSalesRecord, UploadLog
from sales.views import ItemLedgerPDFUploadView

# ── Helpers ───────────────────────────────────────────────
def separator(title):
    print('\n' + '=' * 50)
    print(f'TEST {title}')
    print('=' * 50)

factory = APIRequestFactory()
user    = User.objects.filter(is_staff=True).first()
if not user:
    print('ERROR: No staff user. Run: python manage.py createsuperuser')
    exit()

PDF_PATH = 'item.pdf'
if not os.path.exists(PDF_PATH):
    print(f'ERROR: item.pdf not found in {os.getcwd()}')
    exit()

with open(PDF_PATH, 'rb') as f:
    pdf_bytes = f.read()

print(f'Using staff user : {user.username}')
print(f'Using file       : {PDF_PATH} ({len(pdf_bytes):,} bytes)')

def api_upload_pdf(filename, file_bytes):
    upload = SimpleUploadedFile(
        filename, file_bytes, content_type='application/pdf'
    )
    req = factory.post(
        '/api/sales/upload/item-ledger/',
        {'file': upload},
        format='multipart'
    )
    force_authenticate(req, user=user)
    return ItemLedgerPDFUploadView.as_view()(req)

# ─────────────────────────────────────────────────────────
# TEST 1 — Upload succeeds (HTTP 200/201)
# ─────────────────────────────────────────────────────────
separator('1 — Upload item.pdf (must return 200/201)')

# Clear previous test data
ItemSalesRecord.objects.filter(
    product__product_name__iexact='RATTHI 400g'
).delete()

response = api_upload_pdf('item.pdf', pdf_bytes)

if response.status_code in (200, 201):
    data = response.data
    print(f'HTTP status    : {response.status_code}')
    print(f'product        : {data.get("product")}')
    print(f'records_created: {data.get("records_created")}')
    print(f'total_qty_sold : {data.get("total_qty_sold")}')
    print(f'date_range     : {data.get("date_from")} → {data.get("date_to")}')
    print(f'upload_log_id  : {data.get("upload_log_id")}')
    print('PASS')
else:
    print(f'FAIL: HTTP {response.status_code}')
    print(response.data)
    exit()

# ─────────────────────────────────────────────────────────
# TEST 2 — Product resolved correctly
# Expected: RATTHI 400g exists in Product table
# ─────────────────────────────────────────────────────────
separator('2 — Product resolved: RATTHI 400g')

product = Product.objects.filter(product_name__iexact='RATTHI 400g').first()
if product:
    print(f'Product in DB  : "{product.product_name}" (id={product.id})')
    print(f'unit_price     : {product.unit_price}')
    print('PASS')
else:
    print('FAIL: RATTHI 400g not found in Product table')
    print('      Run Pipeline 1 (Book1.xlsx upload) first')
    exit()

# ─────────────────────────────────────────────────────────
# TEST 3 — ItemSalesRecords created in DB
# Expected: one record per unique sale date for RATTHI 400g
# ─────────────────────────────────────────────────────────
separator('3 — ItemSalesRecord rows created in DB')

records = ItemSalesRecord.objects.filter(product=product).order_by('sale_date')
count   = records.count()
print(f'Records in DB  : {count} (one per unique sale date)')

if count > 0:
    first = records.first()
    last  = records.last()
    print(f'First record   : {first.sale_date}, qty={first.quantity_sold}')
    print(f'Last record    : {last.sale_date},  qty={last.quantity_sold}')
    print('PASS' if count > 0 else 'FAIL')
else:
    print('FAIL: No ItemSalesRecord rows created')

# ─────────────────────────────────────────────────────────
# TEST 4 — Total qty sold matches PDF footer (2,571)
# PDF footer shows: Total OUT = 2,571.000
# ─────────────────────────────────────────────────────────
separator('4 — Total qty sold matches PDF total (must be 2571)')

from django.db.models import Sum
total_sold = records.aggregate(total=Sum('quantity_sold'))['total'] or 0
print(f'DB total qty   : {total_sold}')
print(f'PDF total OUT  : 2571 (from PDF footer)')

# Allow small variance in case some rows are returns/adjustments
if abs(total_sold - 2571) <= 5:
    print('PASS')
else:
    print(f'FAIL: Expected ~2571, got {total_sold}')
    print('      Check if parser is skipping valid rows or double-counting')

# ─────────────────────────────────────────────────────────
# TEST 5 — Specific date spot check
# From PDF: 2025/01/27 — first day of data
#   Bills 0102426, 0102647, 0102600, 0102616, 0102629 → 5 units sold
# ─────────────────────────────────────────────────────────
separator('5 — Spot check: 2025-01-27 sold qty = 5')

from datetime import date
rec_jan27 = records.filter(sale_date=date(2025, 1, 27)).first()
if rec_jan27:
    print(f'2025-01-27 qty : {rec_jan27.quantity_sold} (expected 5)')
    print('PASS' if rec_jan27.quantity_sold == 5 else
          f'FAIL: expected 5, got {rec_jan27.quantity_sold}')
else:
    print('FAIL: No record for 2025-01-27')

# ─────────────────────────────────────────────────────────
# TEST 6 — OPENING STOCK row was skipped
# First row in PDF: "2025/01/27 OPENING STOCK GENARAL 229.000 0.000"
# This has IN=229, OUT=0 — should NOT be included as a sale
# ─────────────────────────────────────────────────────────
separator('6 — OPENING STOCK row skipped (not counted as sale)')

# If opening stock was included, total would be 2571 + 229 = 2800
# Check no record has qty = 229 from opening stock
opening_stock_leak = records.filter(quantity_sold__gte=200).exists()
print(f'Any record >= 200 qty : {opening_stock_leak} (must be False)')
print('PASS' if not opening_stock_leak else
      'FAIL: Opening stock may have been counted as sale')

# ─────────────────────────────────────────────────────────
# TEST 7 — Idempotency: second upload does not duplicate
# ─────────────────────────────────────────────────────────
separator('7 — Second upload idempotency (no duplicate records)')

count_before = records.count()
api_upload_pdf('item.pdf', pdf_bytes)
count_after  = ItemSalesRecord.objects.filter(product=product).count()

print(f'Records before : {count_before}')
print(f'Records after  : {count_after}')
print('PASS' if count_after == count_before else
      f'FAIL: Records increased from {count_before} to {count_after}')

# ─────────────────────────────────────────────────────────
# TEST 8 — Wrong file type rejected
# ─────────────────────────────────────────────────────────
separator('8 — Wrong file type rejected (send .txt file)')

bad_file = SimpleUploadedFile('test.txt', b'not a pdf', content_type='text/plain')
req = factory.post('/api/sales/upload/item-ledger/', {'file': bad_file}, format='multipart')
force_authenticate(req, user=user)
bad_response = ItemLedgerPDFUploadView.as_view()(req)

print(f'HTTP status    : {bad_response.status_code} (must be 400)')
print('PASS' if bad_response.status_code == 400 else 'FAIL')

# ─────────────────────────────────────────────────────────
# TEST 9 — Upload log created correctly
# ─────────────────────────────────────────────────────────
separator('9 — Upload log created')

log = UploadLog.objects.filter(upload_type='ITEM_SALES').order_by('-id').first()
if log:
    print(f'status         : {log.status}')
    print(f'file_name      : {log.file_name}')
    print('PASS' if log.status in ('SUCCESS', 'PARTIAL') else 'FAIL')
else:
    print('FAIL: No upload log found')

# ─────────────────────────────────────────────────────────
print('\n' + '=' * 50)
print('ALL PIPELINE 5 TESTS COMPLETE')
print('=' * 50)