#!/usr/bin/env python
"""Quick manual test of product-type resolution."""
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'smart_inventory.settings')
django.setup()
from orders.services.matching import resolve_query_type, classify_product_by_name, set_brand_prefixes
from products.models import Brand

# Set brand prefixes
brands = [b.brand_name for b in Brand.objects.all()]
set_brand_prefixes(brands)

# Test query resolution
tests = [
    'milk products',
    'milk powder',
    'powdered milk',
    'milk powders',
    'Ratthi milk powder',
    'Anchor milk powder',
    'Ratthi or Anchor',
    'compare Ratthi and Anchor milk powder',
    'cheapest milk powder',
    'Anchor 400g price',
    'coconut milk',
    'milk chocolate',
    'rice products',
    'cooking oil',
    'cheapest tea',
    'compare two brands of coffee',
    'UHT milk',
    'fresh milk',
    'yoghurt',
    'drinking yoghurt',
]
for q in tests:
    r = resolve_query_type(q)
    print(f'  "{q}" -> type={r.product_type}, conf={r.confidence}, brand={r.brand}, size={r.size}, op={r.operation}, terms={r.search_terms}')

print()
print('=== Product classification ===')
prod_tests = [
    ('MAGGI COCO MILK POWDER', 'General'),
    ('RITZBURY MILK 93g', 'Chocolate'),
    ('KOTMALE FRESH MILK 180ML', 'General'),
    ('AMBEWELA SET YOGHURT', 'General'),
    ('COLO COCONUT MILK', 'General'),
    ('KNORR COCONUT MILK', 'Dry Groceries'),
    ('RATTHI 400g', 'General'),
    ('ANCHOR 400G', 'General'),
    ('RATTHI YOUGHART 80g', 'General'),
    ('ANCHOR DRINKING YOUGURT', 'General'),
    ('FORTUNE VEGETABLE OIL 1L', 'Cooking Oils'),
    ('KOTAGALA TEA 50 BAGS', 'Beverages'),
    ('RICE 5KG', 'Dry Groceries'),
]
for name, cat in prod_tests:
    pt, conf = classify_product_by_name(name, cat)
    print(f'  "{name}" / "{cat}" -> type={pt}, conf={conf}')