from django.test import TestCase, Client
from datetime import date, timedelta

from products.models import Product, Brand, Category
from sales.models import ItemSalesRecord
from inventory.models import ProductLifecycle, InventoryHealthScore
from inventory.services.lifecycle import _resolve_declining_recommendation
from inventory.testing_helpers import (
    create_user_with_role, login_headers, local_today,
)

# ============================================================
# F06 — Product Lifecycle Monitoring Tests
# Samanala Super Mart DSS
# ============================================================
# How to run:
#   python manage.py test inventory.test06 --verbosity=2
#
# Changes from the previous version:
#   * The test user now has the MANAGER role (was is_staff only -> 403).
#   * Dates use Sri Lanka local time, matching the app.
#   * GET /api/lifecycle/<product_id>/ now returns a dict
#     {history, sales_series, date_from, date_to}, not a bare list.
#   * SLOW_MOVING recommendation is CLEARANCE (or PHASE_OUT after 3
#     consecutive runs); the old DISCONTINUE value is no longer used.
# ============================================================


class F06LifecycleTestSetup(TestCase):
    """Base setup shared across all F06 tests."""

    def setUp(self):
        self.client = Client()

        self.manager = create_user_with_role('test_manager', 'MANAGER')
        self.auth_header = login_headers(self.client, 'test_manager')

        # ---- Create Brand and Category ----
        self.brand = Brand.objects.create(brand_name='Test Brand')
        self.category = Category.objects.create(category_name='Test Category')

        today = local_today()

        # Product A — NEW (introduced < 30 days ago)
        self.product_new = Product.objects.create(
            product_name='New Product',
            brand=self.brand,
            category=self.category,
            introduced_date=today - timedelta(days=10),
            is_active=True,
            avg_cost_price=100.00,
            cost_price=100.00,
            unit_price=150.00,
        )

        # Product B — SLOW_MOVING (< 5 units in last 60 days)
        self.product_slow = Product.objects.create(
            product_name='Slow Moving Product',
            brand=self.brand,
            category=self.category,
            introduced_date=today - timedelta(days=200),
            is_active=True,
            avg_cost_price=80.00,
            cost_price=80.00,
            unit_price=120.00,
        )

        # Product C — GROWING (current velocity > historical x 1.15)
        self.product_growing = Product.objects.create(
            product_name='Growing Product',
            brand=self.brand,
            category=self.category,
            introduced_date=today - timedelta(days=200),
            is_active=True,
            avg_cost_price=50.00,
            cost_price=50.00,
            unit_price=90.00,
        )

        # Product D — DECLINING (current velocity < historical x 0.85)
        self.product_declining = Product.objects.create(
            product_name='Declining Product',
            brand=self.brand,
            category=self.category,
            introduced_date=today - timedelta(days=200),
            is_active=True,
            avg_cost_price=60.00,
            cost_price=60.00,
            unit_price=100.00,
        )

        # ---- Create sales records ----

        # SLOW_MOVING — only 3 units in last 60 days
        for i in range(3):
            ItemSalesRecord.objects.create(
                product=self.product_slow,
                quantity_sold=1,
                unit_price=120.00,
                total_amount=120.00,
                sale_date=today - timedelta(days=i * 10)
            )

        # GROWING — high recent (4/day), low historical (2/day)
        for i in range(30):
            ItemSalesRecord.objects.create(
                product=self.product_growing,
                quantity_sold=4,
                unit_price=90.00,
                total_amount=360.00,
                sale_date=today - timedelta(days=i)
            )
        for i in range(31, 121):
            ItemSalesRecord.objects.create(
                product=self.product_growing,
                quantity_sold=2,
                unit_price=90.00,
                total_amount=180.00,
                sale_date=today - timedelta(days=i)
            )

        # DECLINING — low recent (1/day), high historical (5/day)
        for i in range(30):
            ItemSalesRecord.objects.create(
                product=self.product_declining,
                quantity_sold=1,
                unit_price=100.00,
                total_amount=100.00,
                sale_date=today - timedelta(days=i)
            )
        for i in range(31, 121):
            ItemSalesRecord.objects.create(
                product=self.product_declining,
                quantity_sold=5,
                unit_price=100.00,
                total_amount=500.00,
                sale_date=today - timedelta(days=i)
            )


# ============================================================
# TEST 1 — POST /api/lifecycle/calculate/
# ============================================================

class F06CalculateTest(F06LifecycleTestSetup):

    def test_calculate_requires_auth(self):
        """Should return 401 if no token provided."""
        response = self.client.post('/api/lifecycle/calculate/')
        self.assertEqual(response.status_code, 401)

    def test_calculate_forbidden_for_non_manager(self):
        """A logged-in user without MANAGER/ADMIN role must get 403."""
        create_user_with_role('plain_staff_f06', None)
        staff_header = login_headers(self.client, 'plain_staff_f06')
        response = self.client.post(
            '/api/lifecycle/calculate/',
            content_type='application/json',
            **staff_header
        )
        self.assertEqual(response.status_code, 403)
        print("\n✅ Non-manager blocked from lifecycle calculate (403)")

    def test_calculate_success(self):
        """Should return 200 and trigger lifecycle calculation."""
        response = self.client.post(
            '/api/lifecycle/calculate/',
            content_type='application/json',
            **self.auth_header
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        print(f"\n✅ calculate/ → {data}")

    def test_new_product_gets_new_status(self):
        """Product introduced < 30 days ago must be NEW → MONITOR."""
        self.client.post('/api/lifecycle/calculate/',
                         content_type='application/json', **self.auth_header)
        record = ProductLifecycle.objects.filter(product=self.product_new).last()
        self.assertIsNotNone(record, "No lifecycle record created for NEW product")
        self.assertEqual(record.status, 'NEW')
        self.assertEqual(record.recommendation, 'MONITOR')
        print(f"\n✅ NEW: status={record.status}, recommendation={record.recommendation}")

    def test_slow_moving_product_status(self):
        """
        Product with < 5 units in 60 days must be SLOW_MOVING.
        Recommendation is CLEARANCE on the first run (PHASE_OUT after 3
        consecutive SLOW_MOVING runs) — see inventory/services/lifecycle.py.
        """
        self.client.post('/api/lifecycle/calculate/',
                         content_type='application/json', **self.auth_header)
        record = ProductLifecycle.objects.filter(product=self.product_slow).last()
        self.assertIsNotNone(record)
        self.assertEqual(record.status, 'SLOW_MOVING')
        self.assertIn(record.recommendation, ['CLEARANCE', 'PHASE_OUT'])
        print(f"\n✅ SLOW_MOVING: status={record.status}, recommendation={record.recommendation}")

    def test_growing_product_status(self):
        """Product with current velocity > historical x 1.15 must be GROWING → RETAIN."""
        self.client.post('/api/lifecycle/calculate/',
                         content_type='application/json', **self.auth_header)
        record = ProductLifecycle.objects.filter(product=self.product_growing).last()
        self.assertIsNotNone(record)
        self.assertEqual(record.status, 'GROWING')
        self.assertEqual(record.recommendation, 'RETAIN')
        print(f"\n✅ GROWING: status={record.status}, recommendation={record.recommendation}")

    def test_declining_product_status(self):
        """
        Velocity < historical x 0.85 must be DECLINING.
        Since lifecycle v5 the recommendation is scaled by the product's
        latest health score; with no health score calculated yet it defaults
        to MONITOR (see F06DecliningRecommendationTest for the full mapping).
        """
        self.client.post('/api/lifecycle/calculate/',
                         content_type='application/json', **self.auth_header)
        record = ProductLifecycle.objects.filter(product=self.product_declining).last()
        self.assertIsNotNone(record)
        self.assertEqual(record.status, 'DECLINING')
        self.assertEqual(record.recommendation, 'MONITOR')
        print(f"\n✅ DECLINING: status={record.status}, recommendation={record.recommendation}")

    def test_inactive_products_excluded(self):
        """Inactive products must be skipped — no lifecycle record created."""
        inactive = Product.objects.create(
            product_name='Inactive Product',
            brand=self.brand,
            category=self.category,
            introduced_date=local_today() - timedelta(days=200),
            is_active=False,
            avg_cost_price=50.00,
            cost_price=50.00,
            unit_price=80.00,
        )
        self.client.post('/api/lifecycle/calculate/',
                         content_type='application/json', **self.auth_header)
        record = ProductLifecycle.objects.filter(product=inactive).first()
        self.assertIsNone(record, "Inactive product should NOT get a lifecycle record")
        print("\n✅ Inactive product correctly excluded")

    def test_calculate_creates_new_record_each_run(self):
        """Running calculate twice same day updates existing record (unique per day)."""
        self.client.post('/api/lifecycle/calculate/',
                         content_type='application/json', **self.auth_header)
        self.client.post('/api/lifecycle/calculate/',
                         content_type='application/json', **self.auth_header)
        records = ProductLifecycle.objects.filter(product=self.product_growing)
        self.assertEqual(records.count(), 1,
                         "Same-day runs should update — not duplicate — the record")
        print(f"\n✅ Same-day idempotent: {records.count()} record (correct)")


# ============================================================
# TEST 2 — GET /api/lifecycle/
# ============================================================

class F06GetAllLifecycleTest(F06LifecycleTestSetup):

    def setUp(self):
        super().setUp()
        self.client.post('/api/lifecycle/calculate/',
                         content_type='application/json', **self.auth_header)

    def test_get_all_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.get('/api/lifecycle/')
        self.assertEqual(response.status_code, 401)

    def test_get_all_returns_200(self):
        """Should return 200 with lifecycle records."""
        response = self.client.get('/api/lifecycle/', **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        print(f"\n✅ GET /api/lifecycle/ → {len(data)} records")

    def test_filter_by_status_declining(self):
        """Filter ?status=DECLINING should return only DECLINING products."""
        response = self.client.get('/api/lifecycle/?status=DECLINING',
                                   **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for item in data:
            self.assertEqual(item['status'], 'DECLINING')
        print(f"\n✅ Filter DECLINING → {len(data)} products")

    def test_filter_by_status_slow_moving(self):
        """Filter ?status=SLOW_MOVING should return only SLOW_MOVING products."""
        response = self.client.get('/api/lifecycle/?status=SLOW_MOVING',
                                   **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for item in data:
            self.assertEqual(item['status'], 'SLOW_MOVING')
        print(f"\n✅ Filter SLOW_MOVING → {len(data)} products")


# ============================================================
# TEST 3 — GET /api/lifecycle/declining/
# ⚠ Must be registered BEFORE /lifecycle/<product_id>/ in urls.py
# ============================================================

class F06DecliningEndpointTest(F06LifecycleTestSetup):

    def setUp(self):
        super().setUp()
        self.client.post('/api/lifecycle/calculate/',
                         content_type='application/json', **self.auth_header)

    def test_declining_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.get('/api/lifecycle/declining/')
        self.assertEqual(response.status_code, 401)

    def test_declining_returns_200_not_404(self):
        """
        CRITICAL URL ORDER CHECK.
        If this returns 404/500, 'declining' is being cast as int.
        Fix: register declining/ BEFORE <int:product_id>/ in urls.py.
        """
        response = self.client.get('/api/lifecycle/declining/', **self.auth_header)
        self.assertNotEqual(response.status_code, 404,
                            "URL routing error — register declining/ before <product_id>/")
        self.assertNotEqual(response.status_code, 500,
                            "Server error — likely int cast on 'declining'")
        self.assertEqual(response.status_code, 200)
        print("\n✅ /api/lifecycle/declining/ URL routing correct")

    def test_declining_only_returns_declining_products(self):
        """All products returned must have status DECLINING."""
        response = self.client.get('/api/lifecycle/declining/', **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for item in data:
            self.assertEqual(item['status'], 'DECLINING',
                             f"Non-declining product found: {item}")
        print(f"\n✅ declining/ → {len(data)} products, all DECLINING")

    def test_declining_includes_our_declining_product(self):
        """The declining test product must appear in results."""
        response = self.client.get('/api/lifecycle/declining/', **self.auth_header)
        data = response.json()
        product_ids = [item.get('product') or item.get('product_id') or item.get('id')
                       for item in data]
        self.assertIn(self.product_declining.id, product_ids)
        print(f"\n✅ Declining product ID {self.product_declining.id} found in results")


# ============================================================
# TEST 4 — GET /api/lifecycle/{product_id}/
# Response shape: {history: [...], sales_series: [...], date_from, date_to}
# ============================================================

class F06ProductHistoryTest(F06LifecycleTestSetup):

    def setUp(self):
        super().setUp()
        # Run twice to build history
        self.client.post('/api/lifecycle/calculate/',
                         content_type='application/json', **self.auth_header)
        self.client.post('/api/lifecycle/calculate/',
                         content_type='application/json', **self.auth_header)

    def test_product_history_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.get(f'/api/lifecycle/{self.product_growing.id}/')
        self.assertEqual(response.status_code, 401)

    def test_product_history_returns_200(self):
        """Should return 200 with a history list and a daily sales series."""
        response = self.client.get(
            # calculated_date uses auto_now_add, which stamps the computer's own
            # date.today(). Ask for exactly that date so the test does not
            # depend on the gap between the computer clock and Colombo time.
            f'/api/lifecycle/{self.product_growing.id}/?date_to={date.today()}',
            **self.auth_header
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, dict)
        for key in ['history', 'sales_series', 'date_from', 'date_to']:
            self.assertIn(key, data, f"Missing key: {key}")
        self.assertIsInstance(data['history'], list)
        self.assertGreaterEqual(len(data['history']), 1,
                                "Should have at least 1 history record after calculating")
        self.assertIsInstance(data['sales_series'], list)
        self.assertGreater(len(data['sales_series']), 0)
        print(f"\n✅ Product history → {len(data['history'])} records, "
              f"{len(data['sales_series'])} daily points")

    def test_invalid_product_id_returns_404(self):
        """Non-existent product ID should return 404."""
        response = self.client.get('/api/lifecycle/99999/', **self.auth_header)
        self.assertEqual(response.status_code, 404)
        print("\n✅ Invalid product ID → 404 correct")

    def test_invalid_date_range_returns_400(self):
        """Bad date_from must return 400, not crash."""
        response = self.client.get(
            f'/api/lifecycle/{self.product_growing.id}/?date_from=not-a-date',
            **self.auth_header
        )
        self.assertEqual(response.status_code, 400)
        print("\n✅ Invalid date_from → 400 correct")

    def test_history_records_have_required_fields(self):
        """Each history record must contain status, recommendation, calculated_date."""
        response = self.client.get(
            # Same reason as above: match the date auto_now_add stamped.
            f'/api/lifecycle/{self.product_declining.id}/?date_to={date.today()}',
            **self.auth_header
        )
        data = response.json()
        history = data['history']
        self.assertGreater(len(history), 0)
        record = history[0]
        for field in ['status', 'recommendation', 'calculated_date']:
            self.assertIn(field, record, f"Missing field: {field}")
        print(f"\n✅ History record fields OK: {list(record.keys())}")


# ============================================================
# TEST 5 — DECLINING recommendation is health-tiered (lifecycle v5)
# DECLINING is a sales-trend signal. The recommendation severity depends
# on the product's latest Inventory Health Score status.
# ============================================================

class F06DecliningRecommendationTest(F06LifecycleTestSetup):

    def test_mapping_function(self):
        """The health-status -> recommendation mapping must match the design."""
        expected = {
            'HEALTHY' : 'MONITOR',
            'WATCH'   : 'REVIEW_DISCOUNT',
            'AT RISK' : 'DISCOUNT_REVIEW',
            'CRITICAL': 'IMMEDIATE_ACTION',
            None      : 'MONITOR',
        }
        for health_status, recommendation in expected.items():
            self.assertEqual(
                _resolve_declining_recommendation(health_status),
                recommendation,
                f"Wrong recommendation for health status {health_status!r}",
            )
        print("\n✅ Health status → DECLINING recommendation mapping correct")

    def test_declining_recommendation_follows_health_score(self):
        """
        After a health calculation, the DECLINING product's lifecycle
        recommendation must be the mapping of its latest health status.
        """
        self.client.post('/api/health-scores/calculate/',
                         content_type='application/json', **self.auth_header)
        health = InventoryHealthScore.objects.filter(
            product=self.product_declining
        ).order_by('-calculated_date', '-id').first()
        self.assertIsNotNone(health, "Health score was not calculated")

        self.client.post('/api/lifecycle/calculate/',
                         content_type='application/json', **self.auth_header)
        record = ProductLifecycle.objects.filter(
            product=self.product_declining
        ).last()
        self.assertIsNotNone(record)
        self.assertEqual(record.status, 'DECLINING')
        self.assertEqual(
            record.recommendation,
            _resolve_declining_recommendation(health.status),
        )
        print(f"\n✅ DECLINING + health {health.status} → {record.recommendation}")