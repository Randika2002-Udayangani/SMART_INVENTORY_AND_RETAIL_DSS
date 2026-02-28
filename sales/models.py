from django.db import models

# Create your models here.

from products.models import Product


class UploadLog(models.Model):
    UPLOAD_TYPES = [
        ('ITEM_SALES', 'Item Sales Excel'),
        ('DAILY_BILLS', 'Daily Bills PDF'),
        ('ITEM_MASTER', 'Item Master Excel'),
        ('EXPORT', 'Report Export'),
    ]
    STATUS_CHOICES = [
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
        ('PARTIAL', 'Partial'),
    ]
    file_name = models.CharField(max_length=150)
    upload_date = models.DateTimeField(auto_now_add=True)
    upload_type = models.CharField(max_length=30, choices=UPLOAD_TYPES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    error_message = models.CharField(max_length=500, blank=True)
    uploaded_by = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = 'upload_log'


class DailyBillSummary(models.Model):
    PAYMENT_TYPES = [
        ('CASH', 'Cash'),
        ('CREDIT', 'Credit'),
        ('CARD', 'Card'),
    ]
    sale_date = models.DateField()
    bill_no = models.CharField(max_length=20)
    customer_name = models.CharField(max_length=150, blank=True)
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    final_amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_type = models.CharField(
        max_length=10, choices=PAYMENT_TYPES, blank=True
    )
    is_flagged = models.BooleanField(default=False)
    upload = models.ForeignKey(
        UploadLog, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='upload_id'
    )

    class Meta:
        db_table = 'daily_bill_summary'


class ItemSalesRecord(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, db_column='product_id'
    )
    sale_date = models.DateField()
    quantity_sold = models.IntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    upload = models.ForeignKey(
        UploadLog, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='upload_id'
    )

    class Meta:
        db_table = 'item_sales_record'