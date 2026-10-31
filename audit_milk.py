#!/usr/bin/env python
"""Read-only audit focused on milk products."""
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'smart_inventory.settings')
django.setup()
from products.models import Product, Category, Brand

print('=== MILK POWDER CANDIDATES ===')
for p in Product.objects.select_related('category', 'brand').all():
    name = p.product_name.lower()
    if any(k in name for k in ['milk powder', 'powdered milk', 'milkpowder']):
        cat = p.category.category_name if p.category else 'NULL'
        brand = p.brand.brand_name if p.brand else 'NULL'
        print(f'  id={p.id} "{p.product_name}" category="{cat}" brand="{brand}" price={p.unit_price}')

print()
print('=== RATTHI PRODUCTS ===')
for p in Product.objects.select_related('category', 'brand').filter(product_name__icontains='ratthi'):
    cat = p.category.category_name if p.category else 'NULL'
    brand = p.brand.brand_name if p.brand else 'NULL'
    print(f'  id={p.id} "{p.product_name}" category="{cat}" brand="{brand}" price={p.unit_price}')

print()
print('=== ANCHOR PRODUCTS ===')
for p in Product.objects.select_related('category', 'brand').filter(product_name__icontains='anchor'):
    cat = p.category.category_name if p.category else 'NULL'
    brand = p.brand.brand_name if p.brand else 'NULL'
    print(f'  id={p.id} "{p.product_name}" category="{cat}" brand="{brand}" price={p.unit_price}')

print()
print('=== NIDO PRODUCTS ===')
for p in Product.objects.select_related('category', 'brand').filter(product_name__icontains='nido'):
    cat = p.category.category_name if p.category else 'NULL'
    brand = p.brand.brand_name if p.brand else 'NULL'
    print(f'  id={p.id} "{p.product_name}" category="{cat}" brand="{brand}" price={p.unit_price}')

print()
print('=== COCONUT MILK ===')
for p in Product.objects.select_related('category', 'brand').filter(product_name__icontains='coconut milk'):
    cat = p.category.category_name if p.category else 'NULL'
    brand = p.brand.brand_name if p.brand else 'NULL'
    print(f'  id={p.id} "{p.product_name}" category="{cat}" brand="{brand}" price={p.unit_price}')

print()
print('=== MILO PRODUCTS ===')
for p in Product.objects.select_related('category', 'brand').filter(product_name__icontains='milo'):
    cat = p.category.category_name if p.category else 'NULL'
    brand = p.brand.brand_name if p.brand else 'NULL'
    print(f'  id={p.id} "{p.product_name}" category="{cat}" brand="{brand}" price={p.unit_price}')

print()
print('=== UHT / LIQUID / YOGHURT ===')
for p in Product.objects.select_related('category', 'brand').all():
    name = p.product_name.lower()
    if any(k in name for k in ['uht milk', 'fresh milk', 'liquid milk', 'set yoghurt', 'yoghurt', 'yogurt']):
        cat = p.category.category_name if p.category else 'NULL'
        brand = p.brand.brand_name if p.brand else 'NULL'
        print(f'  id={p.id} "{p.product_name}" category="{cat}" brand="{brand}" price={p.unit_price}')

print()
print('=== CHOCOLATE MILK PRODUCTS ===')
for p in Product.objects.select_related('category', 'brand').all():
    name = p.product_name.lower()
    if 'milk' in name and any(k in name for k in ['chocolate', 'choco', 'cocoa']):
        cat = p.category.category_name if p.category else 'NULL'
        brand = p.brand.brand_name if p.brand else 'NULL'
        print(f'  id={p.id} "{p.product_name}" category="{cat}" brand="{brand}" price={p.unit_price}')