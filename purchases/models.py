from django.db import models

# Create your models here.

from suppliers.models import Supplier
from products.models import Product


class Purchase(models.Model):
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, db_column='supplier_id'
    )
    order_date = models.DateField(null=True, blank=True)
    purchase_date = models.DateField()
    invoice_number = models.CharField(max_length=50, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    expected_days = models.IntegerField(null=True, blank=True)
    actual_days = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = 'purchase'

    def __str__(self):
        return f"Purchase {self.invoice_number}"


class PurchaseBatch(models.Model):
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('EXPIRED', 'Expired'),
        ('DEPLETED', 'Depleted'),
        ('DISPOSED', 'Disposed'),
    ]
    purchase = models.ForeignKey(
        Purchase, on_delete=models.CASCADE, db_column='purchase_id'
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, db_column='product_id'
    )
    quantity_received = models.IntegerField()
    cost_price = models.DecimalField(max_digits=10, decimal_places=2)
    expiry_date = models.DateField(null=True, blank=True)
    remaining_quantity = models.IntegerField()
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='ACTIVE'
    )

    class Meta:
        db_table = 'purchase_batch'