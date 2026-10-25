#!/usr/bin/env python
"""Read-only, record-level review of product-type evidence in the catalogue.

This script reports candidates for human review. It never updates products.
Name-based evidence is explicitly kept separate from a confirmed category
error because the current schema has no canonical product subtype field.
"""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "smart_inventory.settings")
import django
django.setup()

from products.models import Brand, Product
from orders.services.matching import classify_product_by_name, normalize_text, tokenize


def _record(product, issue, evidence, confidence, action, status="manual review"):
    category = product.category.category_name if product.category_id else None
    brand = product.brand.brand_name if product.brand_id else None
    return (
        f"[{status.upper()} | {confidence}] id={product.id} sku={product.sku_code!r} "
        f"name={product.product_name!r} category={category!r} brand={brand!r}\n"
        f"  Issue: {issue}\n  Evidence: {evidence}\n  Suggested action: {action}"
    )


def generate_report():
    rows = list(Product.objects.select_related("category", "brand").order_by("id"))
    brands = list(Brand.objects.all())
    reports = {"Confirmed issues": [], "Probable issues": [], "Uncertain cases": []}

    for product in rows:
        name = normalize_text(product.product_name)
        category = normalize_text(product.category.category_name if product.category_id else "")
        kind, confidence = classify_product_by_name(product.product_name, product.category.category_name if product.category_id else None)

        # Strongly explicit rice pasta is a separate product form, never
        # ordinary rice. A generic category does not establish a data error.
        if "rice pasta" in name or ("rice" in name.split() and "pasta" in name.split()):
            reports["Probable issues"].append(_record(
                product, "Rice pasta can be returned as ordinary rice by keyword search.",
                "Product name explicitly contains both 'rice' and 'pasta'; category does not distinguish the form.",
                "high for product form; low for category error",
                "Keep out of ordinary-rice searches; review whether a distinct pasta category is appropriate."))
        elif "rice flour" in name or "rice noodle" in name:
            reports["Probable issues"].append(_record(
                product, "Rice derivative may be grouped with ordinary rice.",
                "Product name explicitly identifies a processed form.", "high for product form; low for category error",
                "Represent as the named derivative in search classification; review category manually."))

        # Broad-keyword false positives: category evidence says chocolate,
        # personal care, or bakery while the name contains milk.
        if "milk" in name.split() and any(word in category for word in ("chocolate", "skin care", "bakery", "biscuit")):
            reports["Uncertain cases"].append(_record(
                product, "A broad milk keyword search can surface a non-milk-family item.",
                f"Name contains 'milk'; current category is {category!r}.",
                "high for search exclusion; no category correction inferred",
                "Keep in its existing product group; do not classify as a milk product from the name alone."))
        elif "milk" in name.split() and kind is None and category in ("", "general"):
            reports["Uncertain cases"].append(_record(
                product, "The name contains 'milk' but gives no reliable product subtype, and the category is generic.",
                f"Whole-word keyword evidence only; current category={category or None!r}.",
                "low", "Manually verify the product form; exclude it from narrow milk-subtype searches until classified."))

        # Explicit product type with only the generic General category is
        # useful for review, but not proof that the category is wrong.
        if kind and category in ("", "general"):
            reports["Uncertain cases"].append(_record(
                product, f"Name suggests subtype {kind!r}, but category is generic or absent.",
                f"Whole-word product-name evidence; classifier confidence={confidence!r}; category={category or None!r}.",
                confidence or "low", "Verify product identity and assign a structured category only after review."))

        tokens = tokenize(product.product_name)
        has_size = any(token[0].isdigit() for token in tokens)
        descriptors = [token for token in tokens if not token[0].isdigit()]
        if (kind is None and has_size and len(descriptors) == 1
                and product.brand_id is None and category in ("", "general")):
            reports["Uncertain cases"].append(_record(
                product, "Name contains only a descriptor and pack size; product type and structured brand are unavailable.",
                f"Name tokens={tokens!r}; category={category or None!r}; brand field is empty; no subtype classification matched.",
                "low", "Confirm brand and product type before using this record in type-specific searches or comparisons."))

        # Brand is absent from the structured field although the name starts
        # with a known brand. This is an attribute-quality candidate only.
        if product.brand_id is None and product.product_name:
            prefix = normalize_text(product.product_name)
            matching_brands = [b.brand_name for b in brands
                               if prefix.startswith(normalize_text(b.brand_name) + " ")]
            if matching_brands:
                reports["Uncertain cases"].append(_record(
                    product, "Brand field is empty although the product name begins with a known brand.",
                    f"Name prefix matches Brand record(s): {', '.join(sorted(matching_brands, key=len, reverse=True))}.",
                    "medium", "Confirm brand ownership, then populate the structured brand field if correct."))

    lines = ["READ-ONLY PRODUCT CATALOGUE CLASSIFICATION AUDIT",
             f"Products scanned: {len(rows)}",
             "No records were modified. No issue is labelled confirmed because this schema has no canonical subtype field."]
    for title, entries in reports.items():
        lines.extend(["", f"{title} ({len(entries)} records)", "-" * len(title)])
        lines.extend(entries or ["None identified by the current evidence rules."])
    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_report())
