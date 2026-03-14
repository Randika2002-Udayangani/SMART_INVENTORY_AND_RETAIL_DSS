"""
F05 Unit Tests — Sales, Profit & Brand Analytics
=================================================
Tests every calculation in calculate_sales_and_profit(),
aggregate_by_brand_and_category(), and get_top_products().

Each test uses hand-calculated expected values so you can verify
the function output matches the maths independently.

Run with:
    python manage.py test analytics.tests.test_f05_analytics -v 2

Structure:
    TestF05A_BasicProfit          — core revenue / profit / margin
    TestF05A_MarginFlags          — HIGH_MARGIN_LOW_VOLUME, LOW_MARGIN_HIGH_VOLUME, LOSS_PRODUCT
    TestF05A_EdgeCases            — zero revenue, no sales, deleted product, avg_cost=0
    TestF05A_StoreRevenue         — DailyBillSummary aggregation + mismatch detection
    TestF05B_BrandCategory        — brand & category aggregation + sort order
    TestF05C_TopProducts          — rank_by=profit and rank_by=qty
"""

from decimal import Decimal
from datetime import date

from django.test import TestCase

# ── Adjust these imports to match your actual app/module paths ────────────────
from products.models   import Product, Brand, Category
from sales.models      import ItemSalesRecord, DailyBillSummary
from sales.services     import (        # ← change 'analytics.f05' to wherever your file lives
    calculate_sales_and_profit,
    aggregate_by_brand_and_category,
    get_top_products,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════════

START = date(2025, 1, 1)
END   = date(2025, 1, 31)


def make_brand(name="TestBrand"):
    return Brand.objects.create(brand_name=name)


def make_category(name="TestCategory"):
    return Category.objects.create(category_name=name)


def make_product(name, avg_cost, brand=None, category=None, unit_price=100, cost_price=None):
    return Product.objects.create(
        product_name    = name,
        avg_cost_price  = Decimal(str(avg_cost)),
        unit_price      = Decimal(str(unit_price)),
        cost_price      = Decimal(str(cost_price if cost_price is not None else avg_cost)),
        brand           = brand,
        category        = category,
        is_active       = True,
    )


def make_sale(product, qty, unit_price, sale_date=None):
    """Creates one ItemSalesRecord. total_amount = qty × unit_price."""
    d = sale_date or START
    return ItemSalesRecord.objects.create(
        product       = product,
        sale_date     = d,
        quantity_sold = qty,
        unit_price    = Decimal(str(unit_price)),
        total_amount  = Decimal(str(qty)) * Decimal(str(unit_price)),
    )


def make_bill(final_amount, discount=0, sale_date=None):
    d = sale_date or START
    return DailyBillSummary.objects.create(
        sale_date    = d,
        bill_no      = f"BILL{DailyBillSummary.objects.count() + 1:05d}",
        gross_amount = Decimal(str(final_amount)) + Decimal(str(discount)),
        discount     = Decimal(str(discount)),
        final_amount = Decimal(str(final_amount)),
        payment_type = "CASH",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TestF05A_BasicProfit
# ═══════════════════════════════════════════════════════════════════════════════

class TestF05A_BasicProfit(TestCase):
    """
    Verify revenue, profit, and margin for a straightforward single-product case.

    HAND CALCULATION
    ────────────────
    Product A:  avg_cost = Rs. 80,  sold 10 units @ Rs. 120 each
        total_revenue = 10 × 120        = 1200.00
        total_profit  = 1200 − (10×80) = 1200 − 800 = 400.00
        margin_pct    = (400 / 1200) × 100 = 33.33%
    """

    def setUp(self):
        self.brand    = make_brand()
        self.category = make_category()
        self.product  = make_product("Product A", avg_cost=80,
                                     brand=self.brand, category=self.category)
        make_sale(self.product, qty=10, unit_price=120)

    def test_revenue(self):
        results, _, _, _ = calculate_sales_and_profit(START, END)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['total_revenue'], Decimal('1200.00'))

    def test_profit(self):
        results, _, _, _ = calculate_sales_and_profit(START, END)
        self.assertEqual(results[0]['total_profit'], Decimal('400.00'))

    def test_margin_pct(self):
        results, _, _, _ = calculate_sales_and_profit(START, END)
        self.assertEqual(results[0]['margin_pct'], Decimal('33.33'))

    def test_product_name_and_ids(self):
        results, _, _, _ = calculate_sales_and_profit(START, END)
        r = results[0]
        self.assertEqual(r['product_name'], "Product A")
        self.assertEqual(r['brand_name'],   self.brand.brand_name)
        self.assertEqual(r['category_name'], self.category.category_name)

    def test_no_flags_on_normal_product(self):
        # margin=33.33 and qty=10 — should only get HIGH_MARGIN_LOW_VOLUME
        # (margin>25 AND qty<50) but NOT LOW_MARGIN_HIGH_VOLUME or LOSS_PRODUCT
        results, _, _, _ = calculate_sales_and_profit(START, END)
        flags = results[0]['flags']
        self.assertNotIn('LOSS_PRODUCT', flags)
        self.assertNotIn('LOW_MARGIN_HIGH_VOLUME', flags)


class TestF05A_MultiProductMultiDay(TestCase):
    """
    Two products, sales on different days — both must appear in results.

    HAND CALCULATION
    ────────────────
    Product B:  avg_cost=50,  sold 5 @ Rs.100  → revenue=500, profit=250, margin=50%
    Product C:  avg_cost=200, sold 3 @ Rs.250  → revenue=750, profit=150, margin=20%
    """

    def setUp(self):
        cat = make_category()
        self.p_b = make_product("Product B", avg_cost=50,  category=cat)
        self.p_c = make_product("Product C", avg_cost=200, category=cat)
        make_sale(self.p_b, qty=5, unit_price=100, sale_date=date(2025, 1, 5))
        make_sale(self.p_c, qty=3, unit_price=250, sale_date=date(2025, 1, 10))

    def test_both_products_returned(self):
        results, _, _, _ = calculate_sales_and_profit(START, END)
        names = {r['product_name'] for r in results}
        self.assertIn("Product B", names)
        self.assertIn("Product C", names)

    def test_product_b_calculations(self):
        results, _, _, _ = calculate_sales_and_profit(START, END)
        r = next(x for x in results if x['product_name'] == "Product B")
        self.assertEqual(r['total_revenue'], Decimal('500.00'))
        self.assertEqual(r['total_profit'],  Decimal('250.00'))
        self.assertEqual(r['margin_pct'],    Decimal('50.00'))

    def test_product_c_calculations(self):
        results, _, _, _ = calculate_sales_and_profit(START, END)
        r = next(x for x in results if x['product_name'] == "Product C")
        self.assertEqual(r['total_revenue'], Decimal('750.00'))
        self.assertEqual(r['total_profit'],  Decimal('150.00'))
        self.assertEqual(r['margin_pct'],    Decimal('20.00'))

    def test_sales_outside_date_range_excluded(self):
        # Sale on Feb 1 — outside January range
        make_sale(self.p_b, qty=99, unit_price=100, sale_date=date(2025, 2, 1))
        results, _, _, _ = calculate_sales_and_profit(START, END)
        r = next(x for x in results if x['product_name'] == "Product B")
        # qty should still be 5, not 5+99=104
        self.assertEqual(r['total_qty'], 5)


# ═══════════════════════════════════════════════════════════════════════════════
# TestF05A_MarginFlags
# ═══════════════════════════════════════════════════════════════════════════════

class TestF05A_MarginFlags(TestCase):
    """
    Verify the three business flags fire correctly.

    HIGH_MARGIN_LOW_VOLUME  : margin_pct > 25  AND qty < 50
    LOW_MARGIN_HIGH_VOLUME  : margin_pct < 10  AND qty > 200
    LOSS_PRODUCT            : total_profit < 0

    HAND CALCULATIONS
    ─────────────────
    High margin low volume:
        avg_cost=10, unit_price=200, qty=5
        revenue = 5×200 = 1000
        profit  = 1000 − (5×10) = 950
        margin  = 95%  → > 25, qty=5 < 50  ✓ HIGH_MARGIN_LOW_VOLUME

    Low margin high volume:
        avg_cost=95, unit_price=100, qty=300
        revenue = 300×100 = 30000
        profit  = 30000 − (300×95) = 30000 − 28500 = 1500
        margin  = (1500/30000)×100 = 5%  → < 10, qty=300 > 200  ✓ LOW_MARGIN_HIGH_VOLUME

    Loss product:
        avg_cost=150, unit_price=100, qty=10
        revenue = 1000
        profit  = 1000 − (10×150) = 1000 − 1500 = −500  ✓ LOSS_PRODUCT
    """

    def setUp(self):
        cat = make_category()
        self.p_high = make_product("HighMargin",  avg_cost=10,  category=cat)
        self.p_low  = make_product("LowMargin",   avg_cost=95,  category=cat)
        self.p_loss = make_product("LossProduct", avg_cost=150, category=cat)

        make_sale(self.p_high, qty=5,   unit_price=200)
        make_sale(self.p_low,  qty=300, unit_price=100)
        make_sale(self.p_loss, qty=10,  unit_price=100)

    def _get(self, name):
        results, _, _, _ = calculate_sales_and_profit(START, END)
        return next(x for x in results if x['product_name'] == name)

    def test_high_margin_low_volume_flag(self):
        r = self._get("HighMargin")
        self.assertIn('HIGH_MARGIN_LOW_VOLUME', r['flags'])
        self.assertNotIn('LOSS_PRODUCT', r['flags'])

    def test_low_margin_high_volume_flag(self):
        r = self._get("LowMargin")
        self.assertIn('LOW_MARGIN_HIGH_VOLUME', r['flags'])

    def test_loss_product_flag(self):
        r = self._get("LossProduct")
        self.assertIn('LOSS_PRODUCT', r['flags'])
        self.assertLess(r['total_profit'], 0)

    def test_loss_product_profit_value(self):
        r = self._get("LossProduct")
        # revenue=1000, cost=1500, profit= -500
        self.assertEqual(r['total_profit'], Decimal('-500.00'))


# ═══════════════════════════════════════════════════════════════════════════════
# TestF05A_EdgeCases
# ═══════════════════════════════════════════════════════════════════════════════

class TestF05A_EdgeCases(TestCase):

    def test_no_sales_returns_empty(self):
        """No ItemSalesRecord rows → results list must be empty."""
        results, store_rev, discount, consistency = calculate_sales_and_profit(START, END)
        self.assertEqual(results, [])
        self.assertEqual(store_rev, 0)
        self.assertEqual(discount,  0)
        self.assertFalse(consistency['mismatch_flag'])

    def test_avg_cost_zero_profit_equals_revenue(self):
        """
        avg_cost_price = 0  →  profit should equal revenue.

        HAND CALCULATION
        avg_cost=0, qty=10, unit_price=100
        revenue = 1000
        profit  = 1000 − (10×0) = 1000
        margin  = 100%
        """
        cat = make_category()
        p = make_product("ZeroCost", avg_cost=0, category=cat)
        make_sale(p, qty=10, unit_price=100)
        results, _, _, _ = calculate_sales_and_profit(START, END)
        r = results[0]
        self.assertEqual(r['total_profit'],  Decimal('1000.00'))
        self.assertEqual(r['total_revenue'], Decimal('1000.00'))
        self.assertEqual(r['margin_pct'],    Decimal('100.00'))

    def test_deleted_product_skipped_gracefully(self):
        """
        Tests Fix 8: products_map.get() guard — if a product_id in the aggregated
        sales rows has no matching Product, the function skips it silently.

        ItemSalesRecord.product is PROTECT so we cannot create a real orphan row.
        Instead we use unittest.mock to patch Product.objects.filter to exclude
        one product from the map, simulating it being deleted after sales ingested.
        """
        from unittest.mock import patch

        cat = make_category()
        p_keep   = make_product("KeepMe",   avg_cost=50, category=cat)
        p_remove = make_product("RemoveMe", avg_cost=50, category=cat)
        make_sale(p_keep,   qty=5, unit_price=100)
        make_sale(p_remove, qty=5, unit_price=100)

        remove_id = p_remove.id
        original_filter = Product.objects.filter

        def patched_filter(*args, **kwargs):
            qs = original_filter(*args, **kwargs)
            if 'id__in' in kwargs:
                qs = qs.exclude(id=remove_id)
            return qs

        with patch.object(Product.objects, 'filter', side_effect=patched_filter):
            results, _, _, _ = calculate_sales_and_profit(START, END)

        names = [r['product_name'] for r in results]
        self.assertIn('KeepMe', names)
        self.assertNotIn('RemoveMe', names)

    def test_unbranded_product_label(self):
        """Product with no brand should show 'UNBRANDED'."""
        cat = make_category()
        p = make_product("NoBrand", avg_cost=50, brand=None, category=cat)
        make_sale(p, qty=5, unit_price=100)
        results, _, _, _ = calculate_sales_and_profit(START, END)
        self.assertEqual(results[0]['brand_name'], 'UNBRANDED')

    def test_uncategorised_product_label(self):
        """Product with no category should show 'UNCATEGORISED'."""
        brand = make_brand()
        p = make_product("NoCat", avg_cost=50, brand=brand, category=None)
        make_sale(p, qty=5, unit_price=100)
        results, _, _, _ = calculate_sales_and_profit(START, END)
        self.assertEqual(results[0]['category_name'], 'UNCATEGORISED')


# ═══════════════════════════════════════════════════════════════════════════════
# TestF05A_StoreRevenue
# ═══════════════════════════════════════════════════════════════════════════════

class TestF05A_StoreRevenue(TestCase):
    """
    Verify DailyBillSummary aggregation and mismatch detection.

    HAND CALCULATION
    ────────────────
    Bills: Rs. 1000 + Rs. 500 + Rs. 300 = Rs. 1800 store revenue
    Item sales: 1 product, 10 units @ Rs.120 = Rs. 1200

    mismatch_amount = |1800 − 1200| = 600
    mismatch_pct    = (600 / 1800) × 100 = 33.33%
    2% threshold    = 1800 × 0.02 = 36  →  600 > 36  →  mismatch_flag = True
    """

    def setUp(self):
        cat = make_category()
        self.p = make_product("ProductX", avg_cost=80, category=cat)
        make_sale(self.p, qty=10, unit_price=120)
        make_bill(1000)
        make_bill(500)
        make_bill(300)

    def test_store_revenue_sum(self):
        _, store_revenue, _, _ = calculate_sales_and_profit(START, END)
        self.assertAlmostEqual(store_revenue, 1800.0, places=2)

    def test_total_discount(self):
        """Bills with discount=0 → total_discount should be 0."""
        _, _, total_discount, _ = calculate_sales_and_profit(START, END)
        self.assertAlmostEqual(total_discount, 0.0, places=2)

    def test_mismatch_flag_fires(self):
        _, _, _, consistency = calculate_sales_and_profit(START, END)
        self.assertTrue(consistency['mismatch_flag'])

    def test_mismatch_amount(self):
        _, _, _, consistency = calculate_sales_and_profit(START, END)
        self.assertAlmostEqual(consistency['mismatch_amount'], 600.0, places=2)

    def test_no_mismatch_when_totals_match(self):
        """
        When item sales total ≈ bill total (within 2%), flag should be False.

        Bills: Rs. 1200 exactly = item sales total.
        mismatch = 0  → flag = False
        """
        DailyBillSummary.objects.all().delete()
        make_bill(1200)
        _, _, _, consistency = calculate_sales_and_profit(START, END)
        self.assertFalse(consistency['mismatch_flag'])

    def test_discount_aggregation(self):
        """
        HAND CALCULATION
        Bill 1: final=900, discount=100  →  gross=1000
        Bill 2: final=450, discount=50   →  gross=500
        total_discount = 100 + 50 = 150
        """
        DailyBillSummary.objects.all().delete()
        make_bill(final_amount=900, discount=100)
        make_bill(final_amount=450, discount=50)
        _, _, total_discount, _ = calculate_sales_and_profit(START, END)
        self.assertAlmostEqual(total_discount, 150.0, places=2)


# ═══════════════════════════════════════════════════════════════════════════════
# TestF05B_BrandCategory
# ═══════════════════════════════════════════════════════════════════════════════

class TestF05B_BrandCategory(TestCase):
    """
    Verify brand and category aggregation.

    HAND CALCULATION
    ────────────────
    Brand A — two products:
        Product 1: avg_cost=50,  qty=10, price=100  → profit = 500
        Product 2: avg_cost=80,  qty=5,  price=120  → profit = 200
        Brand A total_profit = 500 + 200 = 700

    Brand B — one product:
        Product 3: avg_cost=100, qty=8,  price=150  → profit = 400
        Brand B total_profit = 400

    Sort order: Brand A (700) first, Brand B (400) second.
    """

    def setUp(self):
        self.brand_a = make_brand("Brand A")
        self.brand_b = make_brand("Brand B")
        cat = make_category("Cat X")

        p1 = make_product("P1", avg_cost=50,  brand=self.brand_a, category=cat)
        p2 = make_product("P2", avg_cost=80,  brand=self.brand_a, category=cat)
        p3 = make_product("P3", avg_cost=100, brand=self.brand_b, category=cat)

        make_sale(p1, qty=10, unit_price=100)
        make_sale(p2, qty=5,  unit_price=120)
        make_sale(p3, qty=8,  unit_price=150)

    def test_brand_a_profit(self):
        brands, _ = aggregate_by_brand_and_category(START, END)
        a = next(b for b in brands if b['brand_name'] == "Brand A")
        self.assertAlmostEqual(a['total_profit'], 700.0, places=2)

    def test_brand_b_profit(self):
        brands, _ = aggregate_by_brand_and_category(START, END)
        b = next(b for b in brands if b['brand_name'] == "Brand B")
        self.assertAlmostEqual(b['total_profit'], 400.0, places=2)

    def test_brands_sorted_by_profit_descending(self):
        brands, _ = aggregate_by_brand_and_category(START, END)
        profits = [b['total_profit'] for b in brands]
        self.assertEqual(profits, sorted(profits, reverse=True))

    def test_category_profit_total(self):
        """
        All three products are in Cat X.
        Cat X total_profit = 500 + 200 + 400 = 1100
        """
        _, categories = aggregate_by_brand_and_category(START, END)
        c = next(c for c in categories if c['category_name'] == "Cat X")
        self.assertAlmostEqual(c['total_profit'], 1100.0, places=2)

    def test_accepts_precomputed_results(self):
        """Passing product_results avoids recalculating — result must be identical."""
        product_results, _, _, _ = calculate_sales_and_profit(START, END)
        brands_direct, _  = aggregate_by_brand_and_category(START, END)
        brands_precomp, _ = aggregate_by_brand_and_category(START, END, product_results=product_results)
        self.assertEqual(
            sorted(brands_direct,  key=lambda x: x['brand_name']),
            sorted(brands_precomp, key=lambda x: x['brand_name']),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# TestF05C_TopProducts
# ═══════════════════════════════════════════════════════════════════════════════

class TestF05C_TopProducts(TestCase):
    """
    Verify top-N ranking by profit and by quantity.

    HAND CALCULATION
    ────────────────
    Product  | avg_cost | qty | price | profit
    ---------|----------|-----|-------|-------
    Alpha    |   50     | 10  |  100  |  500
    Beta     |   20     | 30  |   60  | 1200
    Gamma    |  100     | 20  |  150  | 1000
    Delta    |   10     |  5  |  200  |  950
    Epsilon  |   80     | 15  |  120  |  600

    Rank by profit:  Beta(1200) > Gamma(1000) > Delta(950) > Epsilon(600) > Alpha(500)
    Rank by qty:     Beta(30) > Gamma(20) > Epsilon(15) > Alpha(10) > Delta(5)
    """

    def setUp(self):
        cat = make_category()
        data = [
            ("Alpha",   50,  10, 100),
            ("Beta",    20,  30,  60),
            ("Gamma",  100,  20, 150),
            ("Delta",   10,   5, 200),
            ("Epsilon", 80,  15, 120),
        ]
        for name, cost, qty, price in data:
            p = make_product(name, avg_cost=cost, category=cat)
            make_sale(p, qty=qty, unit_price=price)

    def test_top_by_profit_order(self):
        top = get_top_products(START, END, rank_by='profit', limit=5)
        names = [r['product_name'] for r in top]
        self.assertEqual(names, ["Beta", "Gamma", "Delta", "Epsilon", "Alpha"])

    def test_top_by_qty_order(self):
        top = get_top_products(START, END, rank_by='qty', limit=5)
        names = [r['product_name'] for r in top]
        self.assertEqual(names, ["Beta", "Gamma", "Epsilon", "Alpha", "Delta"])

    def test_limit_respected(self):
        top = get_top_products(START, END, rank_by='profit', limit=3)
        self.assertEqual(len(top), 3)

    def test_default_limit_is_5(self):
        top = get_top_products(START, END)
        self.assertLessEqual(len(top), 5)

    def test_top_profit_values(self):
        """Verify the actual profit figures for the top 2."""
        top = get_top_products(START, END, rank_by='profit', limit=2)
        self.assertEqual(top[0]['product_name'], "Beta")
        self.assertEqual(top[0]['total_profit'], Decimal('1200.00'))
        self.assertEqual(top[1]['product_name'], "Gamma")
        self.assertEqual(top[1]['total_profit'], Decimal('1000.00'))