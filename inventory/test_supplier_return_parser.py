from datetime import date

from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from products.models import Product
from purchases.models import Purchase
from suppliers.models import Supplier
from inventory.views import (
    _create_return_reconciliation_batch,
    _get_or_create_return_product,
    _get_or_create_return_supplier,
    _parse_return_header,
)


class SupplierReturnHeaderParserTests(SimpleTestCase):
    def test_parses_values_on_the_same_line_as_labels(self):
        header = [
            'Samanala Super Mart',
            'SUPPLY RETURN',
            'Bill No : SR-1007',
            'Date : 09-Sep-2026',
            'Customer : CBL FOODS',
            'Inv No : INV-450',
        ]

        self.assertEqual(
            _parse_return_header(header),
            ('SR-1007', date(2026, 9, 9), 'CBL FOODS', 'INV-450'),
        )

    def test_parses_label_and_value_on_separate_lines(self):
        header = [
            'Bill No :', 'SR-1008', 'Date :', '09/09/2026',
            'Supplier :', 'UNILEVER', 'Invoice No :', 'INV-451',
            'Samanala Super Mart', 'Supply Return',
        ]

        self.assertEqual(
            _parse_return_header(header),
            ('SR-1008', date(2026, 9, 9), 'UNILEVER', 'INV-451'),
        )

    def test_keeps_legacy_column_export_layout(self):
        header = [
            'SR-1009', '10-Sep-2026', 'MD FOODS', 'INV-452',
            'Samanala Super Mart', 'SUPPLY RETURN',
            'Bill No :', 'Date :', 'Customer :', 'Inv No :',
        ]

        self.assertEqual(
            _parse_return_header(header),
            ('SR-1009', date(2026, 9, 10), 'MD FOODS', 'INV-452'),
        )


class SupplierReturnProductRegistrationTests(TestCase):
    item = {
        'item_code': '9001',
        'description': 'NEW RETURN PRODUCT',
        'cost_unit': Decimal('125.50'),
        'sell_unit': Decimal('180.00'),
    }

    def test_creates_new_product_from_return_pdf_details(self):
        product, created, error = _get_or_create_return_product(self.item, date(2026, 9, 9))

        self.assertTrue(created)
        self.assertIsNone(error)
        self.assertEqual(product.sku_code, '9001')
        self.assertEqual(product.product_name, 'NEW RETURN PRODUCT')
        self.assertEqual(product.cost_price, Decimal('125.50'))
        self.assertEqual(product.unit_price, Decimal('180.00'))

    def test_uses_existing_product_matched_by_pdf_item_code(self):
        existing = Product.objects.create(
            product_name='Existing product', sku_code='9001',
            cost_price=Decimal('50.00'), unit_price=Decimal('75.00'),
        )

        product, created, error = _get_or_create_return_product(self.item, date(2026, 9, 9))

        self.assertEqual(product.pk, existing.pk)
        self.assertFalse(created)
        self.assertIsNone(error)

    def test_creates_missing_supplier_and_reuses_it_case_insensitively(self):
        supplier, created = _get_or_create_return_supplier('KIDS JOY')
        reused, created_again = _get_or_create_return_supplier('kids joy')

        self.assertTrue(created)
        self.assertEqual(supplier.supplier_name, 'KIDS JOY')
        self.assertEqual(reused.pk, supplier.pk)
        self.assertFalse(created_again)
        self.assertEqual(Supplier.objects.count(), 1)

    def test_reconciliation_batch_starts_with_return_quantity(self):
        supplier = Supplier.objects.create(supplier_name='KIDS JOY')
        product, _, _ = _get_or_create_return_product(self.item, date(2026, 9, 9))
        purchase = Purchase.objects.create(
            supplier=supplier, purchase_date=date(2026, 9, 9),
            invoice_number='RETURN-SR-1001', total_amount=Decimal('0'),
        )

        batch = _create_return_reconciliation_batch(
            purchase, product, 4, Decimal('125.50')
        )

        self.assertEqual(batch.quantity_received, 4)
        self.assertEqual(batch.remaining_quantity, 4)
        self.assertEqual(batch.status, 'ACTIVE')
