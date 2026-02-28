from django.db import models

# Create your models here.

class Supplier(models.Model):
    supplier_name = models.CharField(max_length=150)
    contact_number = models.CharField(max_length=15, blank=True)
    email = models.CharField(max_length=100, blank=True)
    address = models.CharField(max_length=255, blank=True)
    lead_time_days = models.IntegerField(default=0)
    payment_terms = models.CharField(max_length=50, blank=True)
    return_policy = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'supplier'

    def __str__(self):
        return self.supplier_name