from django.test import TestCase, Client
from datetime import date, timedelta

from products.models import Product, Brand, Category
from suppliers.models import Supplier
from purchases.models import Purchase, PurchaseBatch
from inventory.models import LossRecord, SupplierReturn

# ============================================================
# F07 — Loss & Supplier Returns Tests
# Samanala Super Mart DSS
# ============================================================
# How to run:
#   python manage.py test inventory.test07 --verbosity=2
# ============================================================


class F07TestSetup(TestCase):
    """Base setup shared across all F07 tests."""

    def setUp(self):
        self.client = Client()

        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.manager = User.objects.create_user(
            username='test_manager_f07',
            password='testpass123',
            is_staff=True
        )

        response = self.client.post('/api/auth/login/', {
            'username': 'test_manager_f07',
            'password': 'testpass123'
        }, content_type='application/json')

        self.assertEqual(response.status_code, 200, "Login failed")
        data        = response.json()
        self.token  = data.get('access') or data.get('token')
        self.auth_header = {'HTTP_AUTHORIZATION': f'Bearer {self.token}'}

        today = date.today()

        # ── Brand, Category ──────────────────────────────────────
        self.brand    = Brand.objects.create(brand_name='Test Brand F07')
        self.category = Category.objects.create(category_name='Test Category F07')

        # ── Supplier ─────────────────────────────────────────────
        self.supplier = Supplier.objects.create(
            supplier_name  = 'Test Supplier F07',
            contact_number = '0771234567',
        )

        # ── Product ──────────────────────────────────────────────
        self.product = Product.objects.create(
            product_name   = 'Test Product F07',
            brand          = self.brand,
            category       = self.category,
            introduced_date= today - timedelta(days=100),
            is_active      = True,
            avg_cost_price = 100.00,
            cost_price     = 100.00,
            unit_price     = 150.00,
        )

        # ── Purchase ─────────────────────────────────────────────
        self.purchase = Purchase.objects.create(
            supplier      = self.supplier,
            purchase_date = today - timedelta(days=60),
            total_amount  = 5000.00,
        )

        # ── Active batch (not expired) ────────────────────────────
        self.active_batch = PurchaseBatch.objects.create(
            purchase           = self.purchase,
            product            = self.product,
            quantity_received  = 50,
            remaining_quantity = 50,
            cost_price         = 100.00,
            expiry_date        = today + timedelta(days=30),
            status             = 'ACTIVE',
        )

        # ── Expired batch (for auto-detect test) ─────────────────
        self.expired_batch = PurchaseBatch.objects.create(
            purchase           = self.purchase,
            product            = self.product,
            quantity_received  = 20,
            remaining_quantity = 10,
            cost_price         = 100.00,
            expiry_date        = today - timedelta(days=5),
            status             = 'ACTIVE',  # auto-detect should catch this
        )

        # ── Existing damage loss record ───────────────────────────
        self.loss_record = LossRecord.objects.create(
            product       = self.product,
            batch         = self.active_batch,
            loss_type     = 'DAMAGE',
            loss_quantity = 5,
            loss_value    = 500.00,
            loss_date     = today,
            notes         = 'Test damage',
        )

        # ── Existing supplier return (PENDING) ────────────────────
        self.supplier_return = SupplierReturn.objects.create(
            supplier          = self.supplier,
            batch             = self.active_batch,
            product           = self.product,
            return_date       = today,
            quantity_returned = 10,
            return_value      = 1000.00,
            return_reason     = 'EXPIRY',
            recovery_type     = 'CREDIT_NOTE',
            status            = 'PENDING',
        )


# ============================================================
# TEST 1 — GET /api/losses/
# ============================================================

class F07LossRecordGetTest(F07TestSetup):

    def test_get_losses_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.get('/api/losses/')
        self.assertEqual(response.status_code, 401)

    def test_get_losses_returns_200(self):
        """Should return 200 with loss records."""
        response = self.client.get('/api/losses/', **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        print(f"\n✅ GET /api/losses/ → {len(data)} records")

    def test_filter_by_loss_type(self):
        """Filter ?loss_type=DAMAGE should return only DAMAGE records."""
        response = self.client.get('/api/losses/?loss_type=DAMAGE',
                                   **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for item in data:
            self.assertEqual(item['loss_type'], 'DAMAGE')
        print(f"\n✅ Filter DAMAGE → {len(data)} records")

    def test_filter_by_product(self):
        """Filter ?product=<id> should return records for that product only."""
        response = self.client.get(
            f'/api/losses/?product={self.product.id}', **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for item in data:
            self.assertEqual(item['product'], self.product.id)
        print(f"\n✅ Filter by product → {len(data)} records")


# ============================================================
# TEST 2 — POST /api/losses/
# ============================================================

class F07LossRecordPostTest(F07TestSetup):

    def test_post_loss_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.post('/api/losses/', {
            'product_id'   : self.product.id,
            'loss_type'    : 'DAMAGE',
            'loss_quantity': 2,
        }, content_type='application/json')
        self.assertEqual(response.status_code, 401)

    def test_post_loss_success(self):
        """Should create a loss record and return 201."""
        response = self.client.post('/api/losses/', {
            'product_id'   : self.product.id,
            'loss_type'    : 'DAMAGE',
            'loss_quantity': 3,
            'notes'        : 'Dropped during handling',
        }, content_type='application/json', **self.auth_header)
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['loss_type'], 'DAMAGE')
        self.assertEqual(data['loss_quantity'], 3)
        print(f"\n✅ POST /api/losses/ → loss_id={data['loss_id']}")

    def test_post_loss_missing_fields(self):
        """Should return 400 if required fields are missing."""
        response = self.client.post('/api/losses/', {
            'product_id': self.product.id,
        }, content_type='application/json', **self.auth_header)
        self.assertEqual(response.status_code, 400)
        print("\n✅ Missing fields → 400 correct")

    def test_post_loss_invalid_loss_type(self):
        """Should return 400 for invalid loss_type."""
        response = self.client.post('/api/losses/', {
            'product_id'   : self.product.id,
            'loss_type'    : 'INVALID_TYPE',
            'loss_quantity': 2,
        }, content_type='application/json', **self.auth_header)
        self.assertEqual(response.status_code, 400)
        print("\n✅ Invalid loss_type → 400 correct")

    def test_post_loss_invalid_product(self):
        """Should return 404 for non-existent product."""
        response = self.client.post('/api/losses/', {
            'product_id'   : 99999,
            'loss_type'    : 'DAMAGE',
            'loss_quantity': 2,
        }, content_type='application/json', **self.auth_header)
        self.assertEqual(response.status_code, 404)
        print("\n✅ Invalid product_id → 404 correct")


# ============================================================
# TEST 3 — GET /api/losses/summary/
# ============================================================

class F07LossSummaryTest(F07TestSetup):

    def test_summary_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.get('/api/losses/summary/')
        self.assertEqual(response.status_code, 401)

    def test_summary_returns_200(self):
        """Should return 200 with all required summary fields."""
        response = self.client.get('/api/losses/summary/', **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for field in ['gross_expiry_loss', 'recovered_amount',
                      'net_expiry_loss', 'discount_loss',
                      'damage_loss', 'total_net_loss']:
            self.assertIn(field, data, f"Missing field: {field}")
        print(f"\n✅ GET /api/losses/summary/ → {data}")

    def test_summary_damage_reflects_records(self):
        """damage_loss should include our test damage record (500.00)."""
        response = self.client.get('/api/losses/summary/', **self.auth_header)
        data     = response.json()
        damage   = float(data['damage_loss'])
        self.assertGreaterEqual(damage, 500.00)
        print(f"\n✅ damage_loss={damage} includes test record")


# ============================================================
# TEST 4 — POST /api/losses/auto-detect/
# ============================================================

class F07LossAutoDetectTest(F07TestSetup):

    def test_auto_detect_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.post('/api/losses/auto-detect/')
        self.assertEqual(response.status_code, 401)

    def test_auto_detect_is_post_not_get(self):
        """GET should not be allowed — must be POST only."""
        response = self.client.get('/api/losses/auto-detect/',
                                   **self.auth_header)
        self.assertIn(response.status_code, [405, 401])
        print("\n✅ GET on auto-detect/ correctly rejected")

    def test_auto_detect_creates_loss_for_expired_batch(self):
        """Should detect the expired batch and create a LossRecord."""
        response = self.client.post(
            '/api/losses/auto-detect/',
            content_type='application/json',
            **self.auth_header
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(data['batches_expired'], 1)

        record = LossRecord.objects.filter(
            batch=self.expired_batch, loss_type='EXPIRY'
        ).first()
        self.assertIsNotNone(record, "LossRecord not created for expired batch")
        self.assertEqual(record.loss_quantity,
                         self.expired_batch.remaining_quantity)
        print(f"\n✅ auto-detect → {data['batches_expired']} expired, "
              f"LossRecord created")

    def test_auto_detect_marks_batch_expired(self):
        """Expired batch status should be updated to EXPIRED."""
        self.client.post('/api/losses/auto-detect/',
                         content_type='application/json', **self.auth_header)
        self.expired_batch.refresh_from_db()
        self.assertEqual(self.expired_batch.status, 'EXPIRED')
        print("\n✅ Batch status → EXPIRED")

    def test_auto_detect_no_duplicates(self):
        """Running auto-detect twice must not create duplicate LossRecords."""
        self.client.post('/api/losses/auto-detect/',
                         content_type='application/json', **self.auth_header)
        self.client.post('/api/losses/auto-detect/',
                         content_type='application/json', **self.auth_header)
        records = LossRecord.objects.filter(
            batch=self.expired_batch, loss_type='EXPIRY'
        )
        self.assertEqual(records.count(), 1,
                         "Should not create duplicate loss records")
        print("\n✅ auto-detect idempotent — no duplicates")


# ============================================================
# TEST 5 — GET + POST /api/supplier-returns/
# ============================================================

class F07SupplierReturnListTest(F07TestSetup):

    def test_get_returns_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.get('/api/supplier-returns/')
        self.assertEqual(response.status_code, 401)

    def test_get_returns_200(self):
        """Should return 200 with supplier return records."""
        response = self.client.get('/api/supplier-returns/', **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        print(f"\n✅ GET /api/supplier-returns/ → {len(data)} records")

    def test_filter_by_status_pending(self):
        """Filter ?status=PENDING should return only PENDING returns."""
        response = self.client.get('/api/supplier-returns/?status=PENDING',
                                   **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for item in data:
            self.assertEqual(item['status'], 'PENDING')
        print(f"\n✅ Filter PENDING → {len(data)} records")

    def test_post_supplier_return_success(self):
        """Should create a supplier return with status PENDING."""
        response = self.client.post('/api/supplier-returns/', {
            'supplier_id'      : self.supplier.id,
            'batch_id'         : self.active_batch.id,
            'product_id'       : self.product.id,
            'quantity_returned': 5,
            'return_reason'    : 'EXPIRY',
            'recovery_type'    : 'CREDIT_NOTE',
        }, content_type='application/json', **self.auth_header)
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['status'], 'PENDING')
        self.assertEqual(data['quantity_returned'], 5)
        print(f"\n✅ POST /api/supplier-returns/ → return_id={data['return_id']}")

    def test_post_supplier_return_missing_fields(self):
        """Should return 400 if required fields missing."""
        response = self.client.post('/api/supplier-returns/', {
            'supplier_id': self.supplier.id,
        }, content_type='application/json', **self.auth_header)
        self.assertEqual(response.status_code, 400)
        print("\n✅ Missing fields → 400 correct")

    def test_post_invalid_supplier(self):
        """Should return 404 for non-existent supplier."""
        response = self.client.post('/api/supplier-returns/', {
            'supplier_id'      : 99999,
            'batch_id'         : self.active_batch.id,
            'product_id'       : self.product.id,
            'quantity_returned': 5,
        }, content_type='application/json', **self.auth_header)
        self.assertEqual(response.status_code, 404)
        print("\n✅ Invalid supplier_id → 404 correct")


# ============================================================
# TEST 6 — PATCH /api/supplier-returns/<pk>/status/
# ============================================================

class F07SupplierReturnStatusTest(F07TestSetup):

    def test_patch_status_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.patch(
            f'/api/supplier-returns/{self.supplier_return.id}/status/',
            {'status': 'CONFIRMED'},
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 401)

    def test_patch_status_confirmed(self):
        """Should update return status to CONFIRMED."""
        response = self.client.patch(
            f'/api/supplier-returns/{self.supplier_return.id}/status/',
            {'status': 'CONFIRMED'},
            content_type='application/json',
            **self.auth_header
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'CONFIRMED')
        self.supplier_return.refresh_from_db()
        self.assertEqual(self.supplier_return.status, 'CONFIRMED')
        print("\n✅ PATCH status → CONFIRMED")

    def test_patch_status_rejected(self):
        """Should update return status to REJECTED."""
        response = self.client.patch(
            f'/api/supplier-returns/{self.supplier_return.id}/status/',
            {'status': 'REJECTED'},
            content_type='application/json',
            **self.auth_header
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'REJECTED')
        print("\n✅ PATCH status → REJECTED")

    def test_patch_invalid_status(self):
        """Should return 400 for invalid status value."""
        response = self.client.patch(
            f'/api/supplier-returns/{self.supplier_return.id}/status/',
            {'status': 'MAYBE'},
            content_type='application/json',
            **self.auth_header
        )
        self.assertEqual(response.status_code, 400)
        print("\n✅ Invalid status → 400 correct")

    def test_patch_invalid_return_id(self):
        """Should return 404 for non-existent return."""
        response = self.client.patch(
            '/api/supplier-returns/99999/status/',
            {'status': 'CONFIRMED'},
            content_type='application/json',
            **self.auth_header
        )
        self.assertEqual(response.status_code, 404)
        print("\n✅ Invalid return_id → 404 correct")


# ============================================================
# TEST 7 — GET /api/supplier-returns/summary/
# ============================================================

class F07SupplierReturnSummaryTest(F07TestSetup):

    def test_summary_requires_auth(self):
        """Should return 401 without token."""
        response = self.client.get('/api/supplier-returns/summary/')
        self.assertEqual(response.status_code, 401)

    def test_summary_returns_200(self):
        """Should return 200 with supplier summary list."""
        response = self.client.get('/api/supplier-returns/summary/',
                                   **self.auth_header)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        print(f"\n✅ GET /api/supplier-returns/summary/ → {len(data)} suppliers")

    def test_summary_has_required_fields(self):
        """Each supplier entry must have all required summary fields."""
        response = self.client.get('/api/supplier-returns/summary/',
                                   **self.auth_header)
        data = response.json()
        self.assertGreater(len(data), 0)
        entry = data[0]
        for field in ['supplier_id', 'total_returns', 'total_confirmed',
                      'total_rejected', 'total_qty_returned', 'recovery_value']:
            self.assertIn(field, entry, f"Missing field: {field}")
        print(f"\n✅ Summary fields OK: {list(entry.keys())}")

    def test_summary_includes_our_supplier(self):
        """Our test supplier must appear in the summary."""
        response = self.client.get('/api/supplier-returns/summary/',
                                   **self.auth_header)
        data         = response.json()
        supplier_ids = [item['supplier_id'] for item in data]
        self.assertIn(self.supplier.id, supplier_ids)
        print(f"\n✅ Supplier ID {self.supplier.id} found in summary")