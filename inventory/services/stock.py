from django.db.models import Sum

from inventory.models import StockLedger


def get_available_stock(product_id):

    stock = StockLedger.objects.filter(
        product_id=product_id
    ).aggregate(
        total_stock=Sum("quantity_change")
    )["total_stock"]


    if stock is None:
        return 0


    return stock