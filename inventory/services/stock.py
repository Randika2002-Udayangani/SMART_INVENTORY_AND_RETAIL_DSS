from django.db.models import Sum

from inventory.models import StockLedger
from purchases.models import PurchaseBatch


def get_available_stock(product_id):

    stock = PurchaseBatch.objects.filter(
        product_id=product_id,
        status__in=["ACTIVE", "PENDING_EXPIRY"],
        remaining_quantity__gt=0,
    ).aggregate(total_stock=Sum("remaining_quantity"))["total_stock"]

    return stock or 0