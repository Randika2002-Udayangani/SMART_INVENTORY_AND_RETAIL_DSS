from django.test import TestCase, Client
from datetime import date, timedelta

from products.models import Product, Brand, Category
from suppliers.models import Supplier
from purchases.models import Purchase, PurchaseBatch
from sales.models import ItemSalesRecord
from inventory.models import InventoryHealthScore, CategoryHealthScore

# ============================================================
# F08 — Inventory Health Score Tests
# Samanala Super Mart DSS
# ============================================================
# How to run:
#   python manage.py test inventory.test08 --verbosity=2
# ============================================================


class F08TestSetup(TestCase):
    """Base setup shared across all F08 tests."""

    def setUp(self):
        self.client = Client()

        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.manager = User.objects.create_user(
            username='test_manager_f08',
            password='testpass123',
            is_staff=True
        )

        response = self.client.post('/api/auth/login/', {
            'username': 'test_manager_f08',
            'password': 'testpass123'
        }, content_type='application/json')

        self.assertEqual(response.status_code, 200, "Login failed")
        data             = response.json()
        self.token       = data.get('access') or data.get('token')
        self.auth_header = {'HTTP_AUTHORIZATION': f'Bearer {self.token}'}

        today = date.today()

        # ── Brand, Category ──────────────────────────────────────
        self.brand    = Brand.objects.create(brand_name='Test Brand F08')
        self.category = Category.objects.create(
            category_name='Test Category F08')

        # ── Supplier & Purchase ───────────────────────────────────
        self.supplier = Supplier.objects.create(
            supplier_name  = 'Test Supplier F08',
            contact_number = '0771234567',
        )
        self.purchase = Purchase.objects.create(
            supplier      = self.supplier,
            purchase_date = today - timedelta(days=60),
            total_amount  = 10000.00,
        )

        # ── Product A — HEALTHY candidate ────────────────────────
        # High margin (66%), good sales, far expiry
        self.product_healthy = Product.objects.create(
            product_name    = 'Healthy Product F08',
            brand           = self.brand,
            category        = self.category,
            introduced_date = today - timedelta(days=200),
            is_active       = True,
            avg_cost_price  = 50.00,
            cost_price      = 50.00,
            unit_price      = 150.00,
        )
        PurchaseBatch.objects.create(
            purchase           = self.purchase,
            product            = self.product_healthy,
            quantity_received  = 200,
            remaining_quantity = 200,
            cost_price         = 50.00,
            expiry_date        = today + timedelta(days=120),
            status             = 'ACTIVE',
        )
        for i in range(30):
            ItemSalesRecord.objects.create(
                product       = self.product_healthy,
                quantity_sold = 10,
                unit_price    = 150.00,
                total_amount  = 1500.00,
                sale_date     = today - timedelta(days=i)
            )

        # ── Product B — CRITICAL candidate ───────────────────────
        # Low margin (5%), near expiry, no sales
        self.product_critical = Product.objects.create(
            product_name    = 'Critical Product F08',
            brand           = self.brand,
            category        = self.category,
            introduced_date = today - timedelta(days=200),
            is_active       = True,
            avg_cost_price  = 95.00,
            cost_price      = 95.00,
            unit_price      = 100.00,
        )
        PurchaseBatch.objects.create(
            purchase           = self.purchase,
            product            = self.product_critical,
            quantity_received  = 5,
            remaining_quantity = 2,
            cost_price         = 95.00,
            expiry_date        = today + timedelta(days=3),
            status             = 'ACTIVE',
        )
        # No sales for critical product


# ============================================================
# TEST 1 — POST /api/health-scores/calculate/
# ============================================================

class F08CalculateTest(F08TestSetup):

    def test_calculate_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.post('/api/health-scores/calculate/')
        self.assertEqual(response.status_code, 401)

    def test_calculate_success(self):
        """Should return 200 and process all active products."""
        response = self.client.post(
            '/api/health-scores/calculate/',
            content_type='application/json',
            **self.auth_header
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('products_processed', data)
        self.assertGreaterEqual(data['products_processed'], 2)
        print(f"\n✅ calculate/ → {data}")

    def test_healthy_product_score(self):
        """High margin + good sales + far expiry → HEALTHY or WATCH."""
        self.client.post('/api/health-scores/calculate/',
                         content_type='application/json', **self.auth_header)
        record = InventoryHealthScore.objects.filter(
            product=self.product_healthy).last()
        self.assertIsNotNone(record)
        self.assertGreaterEqual(float(record.overall_score), 40)
        self.assertIn(record.status, ['HEALTHY', 'WATCH'])
        print(f"\n✅ Healthy: score={record.overall_score}, "
              f"status={record.status}")

    def test_critical_product_score(self):
        """Low margin + near expiry + no sales → AT RISK or CRITICAL."""
        self.client.post('/api/health-scores/calculate/',
                         content_type='application/json', **self.auth_header)
        record = InventoryHealthScore.objects.filter(
            product=self.product_critical).last()
        self.assertIsNotNone(record)
        self.assertLess(float(record.overall_score), 80)
        self.assertIn(record.status, ['CRITICAL', 'AT RISK', 'WATCH'])
        print(f"\n✅ Critical: score={record.overall_score}, "
              f"status={record.status}")

    def test_all_score_components_present(self):
        """Health score must have all component scores."""
        self.client.post('/api/health-scores/calculate/',
                         content_type='application/json', **self.auth_header)
        record = InventoryHealthScore.objects.filter(
            product=self.product_healthy).last()
        self.assertIsNotNone(record.velocity_score)
        self.assertIsNotNone(record.margin_score)
        self.assertIsNotNone(record.expiry_risk_score)
        self.assertIsNotNone(record.stock_duration_score)
        self.assertIsNotNone(record.overall_score)
        print(f"\n✅ Components: vel={record.velocity_score}, "
              f"margin={record.margin_score}, "
              f"expiry={record.expiry_risk_score}, "
              f"duration={record.stock_duration_score}")

    def test_4_component_mode_default(self):
        """With no ratings, weighting_mode must be 4-COMPONENT."""
        self.client.post('/api/health-scores/calculate/',
                         content_type='application/json', **self.auth_header)
        record = InventoryHealthScore.objects.filter(
            product=self.product_healthy).last()
        self.assertEqual(record.weighting_mode, '4-COMPONENT')
        self.assertFalse(record.rating_sufficient)
        print(f"\n✅ Weighting mode: {record.weighting_mode}")

    def test_category_health_score_created(self):
        """CategoryHealthScore must be created after calculate."""
        self.client.post('/api/health-scores/calculate/',
                         content_type='application/json', **self.auth_header)
        cat_score = CategoryHealthScore.objects.filter(
            category=self.category).last()
        self.assertIsNotNone(cat_score)
        self.assertIsNotNone(cat_score.avg_health_score)
        print(f"\n✅ CategoryHealthScore: avg={cat_score.avg_health_score}, "
              f"status={cat_score.status}")

    def test_inactive_products_excluded(self):
        """Inactive products must not get a health score."""
        inactive = Product.objects.create(
            product_name    = 'Inactive F08',
            brand           = self.brand,
            category        = self.category,
            introduced_date = date.today() - timedelta(days=100),
            is_active       = False,
            avg_cost_price  = 50.00,
            cost_price      = 50.00,
            unit_price      = 80.00,
        )
        self.client.post('/api/health-scores/calculate/',
                         content_type='application/json', **self.auth_header)
        record = InventoryHealthScore.objects.filter(product=inactive).first()
        self.assertIsNone(record)
        print("\n✅ Inactive product excluded correctly")


# ============================================================
# TEST 2 — GET /api/health-scores/
# ============================================================

class F08HealthScoreListTest(F08TestSetup):

    def setUp(self):
        super().setUp()
        self.client.post('/api/health-scores/calculate/',
                         content_type='application/json', **self.auth_header)

    def test_list_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.get('/api/health-scores/')
        self.assertEqual(response.status_code, 401)

    def test_list_returns_200(self):
        """Should return 200 with health score records."""
        response = self.client.get('/api/health-scores/', **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        print(f"\n✅ GET /api/health-scores/ → {len(data)} records")

    def test_filter_by_status_critical(self):
        """Filter ?status=CRITICAL should return only CRITICAL records."""
        response = self.client.get(
            '/api/health-scores/?status=CRITICAL', **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for item in data:
            self.assertEqual(item['status'], 'CRITICAL')
        print(f"\n✅ Filter CRITICAL → {len(data)} records")

    def test_filter_by_status_healthy(self):
        """Filter ?status=HEALTHY should return only HEALTHY records."""
        response = self.client.get(
            '/api/health-scores/?status=HEALTHY', **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for item in data:
            self.assertEqual(item['status'], 'HEALTHY')
        print(f"\n✅ Filter HEALTHY → {len(data)} records")

    def test_response_has_required_fields(self):
        """Each record must have all required fields."""
        response = self.client.get('/api/health-scores/', **self.auth_header)
        data     = response.json()
        self.assertGreater(len(data), 0)
        record = data[0]
        for field in ['id', 'product', 'overall_score', 'status',
                      'recommended_action', 'weighting_mode',
                      'calculated_date']:
            self.assertIn(field, record, f"Missing field: {field}")
        print(f"\n✅ Fields OK: {list(record.keys())}")


# ============================================================
# TEST 3 — GET /api/health-scores/categories/
# ============================================================

class F08CategoryHealthScoreTest(F08TestSetup):

    def setUp(self):
        super().setUp()
        self.client.post('/api/health-scores/calculate/',
                         content_type='application/json', **self.auth_header)

    def test_categories_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.get('/api/health-scores/categories/')
        self.assertEqual(response.status_code, 401)

    def test_categories_returns_200_not_404(self):
        """
        CRITICAL URL ORDER CHECK.
        If 404 → 'categories' is being cast as int.
        Fix: register categories/ BEFORE <int:product_id>/ in urls.py.
        """
        response = self.client.get(
            '/api/health-scores/categories/', **self.auth_header)
        self.assertNotEqual(response.status_code, 404,
                            "URL routing error — register categories/ "
                            "before <product_id>/")
        self.assertEqual(response.status_code, 200)
        print("\n✅ /api/health-scores/categories/ URL routing correct")

    def test_categories_has_required_fields(self):
        """Each category entry must have required fields."""
        response = self.client.get(
            '/api/health-scores/categories/', **self.auth_header)
        data = response.json()
        self.assertGreater(len(data), 0)
        entry = data[0]
        for field in ['category', 'avg_health_score', 'healthy_count',
                      'watch_count', 'at_risk_count', 'critical_count',
                      'status']:
            self.assertIn(field, entry, f"Missing field: {field}")
        print(f"\n✅ Category fields OK: {list(entry.keys())}")

    def test_our_category_appears(self):
        """Our test category must appear in the results."""
        response = self.client.get(
            '/api/health-scores/categories/', **self.auth_header)
        data         = response.json()
        category_ids = [item['category'] for item in data]
        self.assertIn(self.category.id, category_ids)
        print(f"\n✅ Category ID {self.category.id} found in results")


# ============================================================
# TEST 4 — GET /api/health-scores/critical/
# ============================================================

class F08CriticalScoreTest(F08TestSetup):

    def setUp(self):
        super().setUp()
        self.client.post('/api/health-scores/calculate/',
                         content_type='application/json', **self.auth_header)

    def test_critical_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.get('/api/health-scores/critical/')
        self.assertEqual(response.status_code, 401)

    def test_critical_returns_200_not_404(self):
        """
        CRITICAL URL ORDER CHECK.
        If 404 → 'critical' is being cast as int.
        """
        response = self.client.get(
            '/api/health-scores/critical/', **self.auth_header)
        self.assertNotEqual(response.status_code, 404,
                            "URL routing error — register critical/ "
                            "before <product_id>/")
        self.assertEqual(response.status_code, 200)
        print("\n✅ /api/health-scores/critical/ URL routing correct")

    def test_critical_only_returns_critical(self):
        """All returned products must have status CRITICAL."""
        response = self.client.get(
            '/api/health-scores/critical/', **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for item in data:
            self.assertEqual(item['status'], 'CRITICAL',
                             f"Non-critical product found: {item}")
        print(f"\n✅ critical/ → {len(data)} products, all CRITICAL")


# ============================================================
# TEST 5 — GET /api/health-scores/<product_id>/
# ============================================================

class F08HealthScoreDetailTest(F08TestSetup):

    def setUp(self):
        super().setUp()
        self.client.post('/api/health-scores/calculate/',
                         content_type='application/json', **self.auth_header)

    def test_detail_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.get(
            f'/api/health-scores/{self.product_healthy.id}/')
        self.assertEqual(response.status_code, 401)

    def test_detail_returns_200(self):
        """Should return 200 with health score history."""
        response = self.client.get(
            f'/api/health-scores/{self.product_healthy.id}/',
            **self.auth_header
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        print(f"\n✅ GET /api/health-scores/{self.product_healthy.id}/ "
              f"→ {len(data)} records")

    def test_detail_invalid_product_returns_404(self):
        """Non-existent product ID should return 404."""
        response = self.client.get(
            '/api/health-scores/99999/', **self.auth_header)
        self.assertEqual(response.status_code, 404)
        print("\n✅ Invalid product_id → 404 correct")

    def test_detail_has_required_fields(self):
        """Each record must contain all required health score fields."""
        response = self.client.get(
            f'/api/health-scores/{self.product_healthy.id}/',
            **self.auth_header
        )
        data   = response.json()
        record = data[0]
        for field in ['overall_score', 'status', 'velocity_score',
                      'margin_score', 'expiry_risk_score',
                      'stock_duration_score', 'weighting_mode',
                      'calculated_date']:
            self.assertIn(field, record, f"Missing field: {field}")
        print(f"\n✅ Detail fields OK: {list(record.keys())}")