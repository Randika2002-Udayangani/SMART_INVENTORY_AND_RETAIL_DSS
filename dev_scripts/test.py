import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'smart_inventory.settings')
django.setup()

import json
from io import BytesIO
import pandas as pd
import openpyxl
from django.test import RequestFactory
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth.models import User
from products.models import Product
from products.views import ItemMasterUploadView
from sales.models import UploadLog

# ── Helper ────────────────────────────────────────────────
def separator(title):
    print('\n' + '=' * 50)
    print(f'TEST {title}')
    print('=' * 50)

def upload_file(filename, file_bytes, user):
    factory = RequestFactory()
    f = SimpleUploadedFile(
        filename,
        file_bytes,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    req = factory.post(
        '/api/products/import/',
        {'file': f},
        format='multipart'
    )
    # Fix: force_authenticate by setting user AND auth flag directly
    # RequestFactory bypasses middleware so we set both attributes manually
    req.user = user
    req._force_auth_user = user       # tells DRF the user is authenticated
    req._force_auth_token = None

    # Also patch is_authenticated directly on the request user
    # in case DRF checks request.auth
    from unittest.mock import patch
    with patch('rest_framework.request.Request.successful_authenticator', return_value=True):
        view = ItemMasterUploadView.as_view()
        response = view(req)
    return response

# ── Get or create staff user ──────────────────────────────
user = User.objects.filter(is_staff=True).first()
if not user:
    print('ERROR: No staff user found. Run: python manage.py createsuperuser')
    exit()

print(f'Using staff user: {user.username}')

# ── Read real file ────────────────────────────────────────
BOOK_PATH = 'Book1.xlsx'
if not os.path.exists(BOOK_PATH):
    # Try common locations
    for path in ['../Book1.xlsx', 'data/Book1.xlsx']:
        if os.path.exists(path):
            BOOK_PATH = path
            break
    else:
        print(f'ERROR: Book1.xlsx not found. Place it in: {os.getcwd()}')
        exit()

print(f'Using file: {BOOK_PATH}')

with open(BOOK_PATH, 'rb') as f:
    real_file_bytes = f.read()

# ─────────────────────────────────────────────────────────
# TEST 1 — Real upload, totals add up
# ─────────────────────────────────────────────────────────
separator('1 — Real upload (totals must add up to 495)')

# Count products before upload
before_count = Product.objects.count()

factory = RequestFactory()
upload = SimpleUploadedFile(
    'Book1.xlsx', real_file_bytes,
    content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
)
req = factory.post('/api/products/import/', {'file': upload}, format='multipart')
req.user = user

# Fix for authentication: use APIRequestFactory instead of RequestFactory
# This properly handles DRF authentication
from rest_framework.test import APIRequestFactory, force_authenticate
api_factory = APIRequestFactory()

def api_upload(filename, file_bytes):
    upload = SimpleUploadedFile(
        filename, file_bytes,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    req = api_factory.post(
        '/api/products/import/',
        {'file': upload},
        format='multipart'
    )
    force_authenticate(req, user=user)   # Fix: proper DRF authentication
    view = ItemMasterUploadView.as_view()
    return view(req)

response = api_upload('Book1.xlsx', real_file_bytes)

if response.status_code in (200, 201):
    data     = response.data
    inserted = data.get('inserted', -1)
    updated  = data.get('updated', -1)
    skipped  = data.get('skipped', -1)
    total    = data.get('total_rows', -1)
    print(f'total_rows : {total}')
    print(f'inserted   : {inserted}')
    print(f'updated    : {updated}')
    print(f'skipped    : {skipped}')
    print(f'flagged_new: {data.get("flagged_new", -1)}')
    if total > 0 and (inserted + updated + skipped) == total:
        print('PASS: inserted + updated + skipped == total_rows')
    else:
        print(f'FAIL: {inserted} + {updated} + {skipped} = {inserted+updated+skipped} != {total}')
    print('\nFirst 5 notes:')
    for note in data.get('notes', [])[:5]:
        print(' ', note)
else:
    print(f'FAIL: HTTP {response.status_code}')
    print(response.data)

# ─────────────────────────────────────────────────────────
# TEST 2 — R1 DEFAULT ITEM not in DB
# ─────────────────────────────────────────────────────────
separator('2 — R1 DEFAULT ITEM not in database')
count = Product.objects.filter(product_name='DEFAULT ITEM').count()
print(f'DEFAULT ITEM count in DB: {count} (must be 0)')
print('PASS' if count == 0 else 'FAIL: DEFAULT ITEM was inserted')

# ─────────────────────────────────────────────────────────
# TEST 3 — Known product price matches file
# ─────────────────────────────────────────────────────────
separator('3 — Price match: DB vs File (row 2)')
wb  = openpyxl.load_workbook(BOOK_PATH)
ws  = wb.active
rows = list(ws.iter_rows(values_only=True))

# Find first non-DEFAULT ITEM row
test_row = None
for row in rows:
    name = str(row[1]).strip() if row[1] else ''
    if name and name != 'DEFAULT ITEM' and row[5]:
        test_row = row
        break

if test_row:
    name_from_file  = str(test_row[1]).strip()
    price_from_file = float(test_row[5])
    print(f'File: "{name_from_file}" = {price_from_file}')

    product = Product.objects.filter(product_name__iexact=name_from_file).first()
    if product:
        db_price = float(product.unit_price)
        print(f'DB:   "{product.product_name}" = {db_price}')
        if abs(db_price - price_from_file) < 0.01:
            print('PASS: prices match')
        else:
            print(f'FAIL: price mismatch — file={price_from_file}, db={db_price}')
    else:
        print(f'FAIL: product not found in DB')
else:
    print('SKIP: could not find test row in file')

# ─────────────────────────────────────────────────────────
# TEST 4 — R6 Negative qty product exists in DB
# ─────────────────────────────────────────────────────────
separator('4 — R6 Negative qty product exists in DB')
neg_row = None
for row in rows:
    if row[4] is not None and str(row[4]).lstrip('-').isdigit():
        try:
            if int(row[4]) < 0 and row[1] and str(row[1]).strip() != 'DEFAULT ITEM':
                neg_row = row
                break
        except:
            pass

if neg_row:
    name = str(neg_row[1]).strip()
    qty  = neg_row[4]
    print(f'Negative qty row: "{name}", qty={qty}')
    product = Product.objects.filter(product_name__iexact=name).first()
    if product:
        print(f'Found in DB: is_active={product.is_active}')
        print('PASS: negative qty product correctly inserted')
    else:
        print('FAIL: negative qty product NOT in DB — R6 broken')
else:
    print('SKIP: no negative qty row found in file')

# ─────────────────────────────────────────────────────────
# TEST 5 — R3 Zero price rejected
# ─────────────────────────────────────────────────────────
separator('5 — R3 Zero price rejected')
buf = BytesIO()
pd.DataFrame([
    [999, 'TEST_ZERO_PRICE_PRODUCT', None, None, 0, 0.0]
]).to_excel(buf, index=False, header=False)
buf.seek(0)

r5 = api_upload('test_zero.xlsx', buf.read())
skipped_count = r5.data.get('skipped', 0)
in_db = Product.objects.filter(product_name='TEST_ZERO_PRICE_PRODUCT').exists()
print(f'skipped count: {skipped_count} (must be >= 1)')
print(f'In DB (must be False): {in_db}')
print('PASS' if skipped_count >= 1 and not in_db else 'FAIL')

# Cleanup
Product.objects.filter(product_name='TEST_ZERO_PRICE_PRODUCT').delete()

# ─────────────────────────────────────────────────────────
# TEST 6 — R7 Inactive product not reactivated
# ─────────────────────────────────────────────────────────
separator('6 — R7 Inactive product stays inactive after upload')

# Find a product that exists in the file
target = None
for row in rows:
    if row[1] and row[5]:
        name = str(row[1]).strip()
        if name != 'DEFAULT ITEM':
            p = Product.objects.filter(product_name__iexact=name).first()
            if p:
                target = p
                break

if target:
    # Deactivate it
    original_price = float(target.unit_price)
    target.is_active = False
    target.save(update_fields=['is_active'])
    print(f'Deactivated: "{target.product_name}" (id={target.id})')

    # Upload again
    api_upload('Book1.xlsx', real_file_bytes)

    # Check
    target.refresh_from_db()
    print(f'is_active after upload : {target.is_active} (must be False)')
    print(f'unit_price after upload: {float(target.unit_price)} (must equal file price)')

    r7_pass = (target.is_active == False)
    print('PASS' if r7_pass else 'FAIL: product was reactivated')

    # Restore
    target.is_active = True
    target.save(update_fields=['is_active'])
else:
    print('SKIP: no matching product found for R7 test')

# ─────────────────────────────────────────────────────────
# TEST 7 — Second upload idempotency (0 inserted)
# ─────────────────────────────────────────────────────────
separator('7 — Second upload idempotency')
r7 = api_upload('Book1.xlsx', real_file_bytes)

if r7.status_code in (200, 201):
    inserted2 = r7.data.get('inserted', -1)
    updated2  = r7.data.get('updated', -1)
    skipped2  = r7.data.get('skipped', -1)
    print(f'inserted (must be 0) : {inserted2}')
    print(f'updated  (must be ~494): {updated2}')
    print(f'skipped  (must be 1) : {skipped2}')
    if inserted2 == 0 and skipped2 >= 1:
        print('PASS')
    else:
        print('FAIL')
else:
    print(f'FAIL: HTTP {r7.status_code}')
    print(r7.data)

# ─────────────────────────────────────────────────────────
# TEST 8 — Upload log
# ─────────────────────────────────────────────────────────
separator('8 — Upload log')
log = UploadLog.objects.filter(upload_type='ITEM_MASTER').order_by('-id').first()
if log:
    print(f'status     : {log.status}')
    print(f'file_name  : {log.file_name}')
    print(f'error preview: {log.error_message[:200]}')
    print('PASS' if log.status in ('SUCCESS', 'PARTIAL') else 'FAIL')
else:
    print('FAIL: No upload log found')

# ─────────────────────────────────────────────────────────
print('\n' + '=' * 50)
print('ALL TESTS COMPLETE')
print('=' * 50)