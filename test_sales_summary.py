"""
test_sales_summary.py
======================
Test GET /api/reports/sales-summary/  — no Postman needed.

USAGE:
    1. Set STAFF_USERNAME / STAFF_PASSWORD below
    2. python manage.py runserver   (in another terminal)
    3. python test_sales_summary.py
"""

import sys
import requests

BASE_URL       = 'http://127.0.0.1:8000'
STAFF_USERNAME = 'admin'
STAFF_PASSWORD = 'Admin123'

PASS, FAIL, INFO = '\033[92m✓\033[0m', '\033[91m✗\033[0m', '\033[94m→\033[0m'

print('\n' + '='*58)
print('  Sales Summary Endpoint Test')
print('='*58 + '\n')

# ── Login ───────────────────────────────────────────────────────────────────
try:
    r = requests.post(f'{BASE_URL}/api/auth/login/',
                       json={'username': STAFF_USERNAME, 'password': STAFF_PASSWORD},
                       timeout=10)
    token = r.json().get('access')
    if not token:
        print(f'{FAIL}  Login failed — {r.status_code} {r.text[:150]}')
        sys.exit(1)
    print(f'{PASS}  Logged in')
    headers = {'Authorization': f'Bearer {token}'}
except requests.exceptions.ConnectionError:
    print(f'{FAIL}  Cannot connect to {BASE_URL} — is runserver running?')
    sys.exit(1)

# ── Test 1: default range (last 30 days) ──────────────────────────────────────
print(f'\n{INFO}  Test 1: default date range (no params)')
r = requests.get(f'{BASE_URL}/api/reports/sales-summary/', headers=headers)
if r.status_code == 200:
    body = r.json()
    print(f'  {PASS}  HTTP 200')
    print(f'  {INFO}  Period: {body["period"]}')
    print(f'  {INFO}  total_units_sold    = {body["total_units_sold"]}')
    print(f'  {INFO}  total_sales_revenue = {body["total_sales_revenue"]}')
    print(f'  {INFO}  total_cost          = {body["total_cost"]}')
    print(f'  {INFO}  gross_profit        = {body["gross_profit"]}')
    if body.get('best_selling_product'):
        print(f'  {INFO}  best_selling  = {body["best_selling_product"]}')
        print(f'  {INFO}  worst_selling = {body["worst_selling_product"]}')
        print(f'  {INFO}  top_products  = {len(body["top_products"])} items')
    else:
        print(f'  {INFO}  No sales data — note: {body.get("note")}')
else:
    print(f'  {FAIL}  HTTP {r.status_code} — {r.text[:300]}')

# ── Test 2: custom date range ─────────────────────────────────────────────────
print(f'\n{INFO}  Test 2: custom date range (?date_from=2026-01-01&date_to=2026-01-31)')
r = requests.get(f'{BASE_URL}/api/reports/sales-summary/',
                  params={'date_from': '2026-01-01', 'date_to': '2026-01-31'},
                  headers=headers)
print(f'  {"PASS" if r.status_code == 200 else "FAIL"}  HTTP {r.status_code}')

# ── Test 3: invalid date range (from > to) ────────────────────────────────────
print(f'\n{INFO}  Test 3: invalid range (date_from after date_to) — expect 400')
r = requests.get(f'{BASE_URL}/api/reports/sales-summary/',
                  params={'date_from': '2026-02-01', 'date_to': '2026-01-01'},
                  headers=headers)
ok = r.status_code == 400
print(f'  {PASS if ok else FAIL}  HTTP {r.status_code} {"(correct)" if ok else "(expected 400)"}')

# ── Test 4: bad date format ───────────────────────────────────────────────────
print(f'\n{INFO}  Test 4: malformed date — expect 400')
r = requests.get(f'{BASE_URL}/api/reports/sales-summary/',
                  params={'date_from': 'not-a-date'},
                  headers=headers)
ok = r.status_code == 400
print(f'  {PASS if ok else FAIL}  HTTP {r.status_code} {"(correct)" if ok else "(expected 400)"}')

print('\n' + '='*58)
print('  Done. If Test 1 shows 0 records, upload an item ledger PDF first.')
print('='*58 + '\n')