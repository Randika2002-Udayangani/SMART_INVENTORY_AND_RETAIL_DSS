import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'smart_inventory.settings')
django.setup()

from rest_framework.test import APIRequestFactory, force_authenticate
from django.contrib.auth.models import User
from products.models import Product
from purchases.models import Supplier
from datetime import date, timedelta
import json

factory = APIRequestFactory()
user = User.objects.filter(is_staff=True).first()
if not user:
    print('ERROR: No staff user. Run: python manage.py createsuperuser')
    exit()

# ── Step 1: Create supplier if not exists ─────────────────
supplier, created = Supplier.objects.get_or_create(
    supplier_name='FONTERRA BRANDS',
    defaults={
        'contact_number': '',
        'email': '',
        'address': '',
        'lead_time_days': 7,
        'payment_terms': '',
        'return_policy': ''
    }
)
print(f'Supplier : {supplier.supplier_name} (id={supplier.id}, created={created})')

# ── Step 2: Get product ───────────────────────────────────
product = Product.objects.filter(product_name__iexact='RATTHI 400g').first()
if not product:
    print('ERROR: RATTHI 400g not found — run Pipeline 1 first')
    exit()
print(f'Product  : {product.product_name} (id={product.id})')
print(f'avg_cost_price BEFORE: {product.avg_cost_price}')

# ── Step 3: POST purchase via F02 ─────────────────────────
expiry = date.today() + timedelta(days=365)

purchase_data = {
    "supplier": supplier.id,
    "invoice_number": "0001578",
    "purchase_date": "2025-02-04",
    "batches": [
        {
            "product": product.id,
            "quantity_received": 72,
            "cost_price": "85.00",
            "expiry_date": str(expiry)
        }
    ]
}

req = factory.post(
    '/api/purchases/',
    data=json.dumps(purchase_data),
    content_type='application/json'
)
force_authenticate(req, user=user)

from purchases.views import PurchaseCreateView
response = PurchaseCreateView.as_view()(req)
print(f'\nHTTP status : {response.status_code}')
print(response.data)

# ── Step 4: Verify WAC updated ────────────────────────────
product.refresh_from_db()
print(f'\navg_cost_price AFTER : {product.avg_cost_price}')
if float(product.avg_cost_price) == 85.00:
    print('PASS: WAC updated correctly')
else:
    print(f'FAIL: expected 85.00, got {product.avg_cost_price}')