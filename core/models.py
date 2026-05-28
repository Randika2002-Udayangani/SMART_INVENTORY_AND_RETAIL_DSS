from django.db import models


# =========================
# CATEGORY MODEL
# =========================

class Category(models.Model):

    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


# =========================
# PRODUCT MODEL
# =========================

class Product(models.Model):

    name = models.CharField(max_length=200)

    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE
    )

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    def __str__(self):
        return self.name


# =========================
# AUDIT LOG MODEL
# =========================

class AuditLog(models.Model):

    action = models.TextField()

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return self.action