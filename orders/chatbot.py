import re

from django.db.models import Sum
from fuzzywuzzy import fuzz

from products.models import Product, Category
from inventory.models import (
    InventoryHealthScore,
    ReorderRecommendation
)
from inventory.services.stock import get_available_stock
from .models import (
    OnlineOrderItem,
    ProductRatingSummary
)

FUZZY_THRESHOLD = 50
LOW_STOCK_THRESHOLD = 10


def detect_intent(message):

    message = message.lower().strip()
    
        # ---------------- Greeting ----------------

    words = re.findall(r"\b\w+\b", message)


    if any(
       word in words
       for word in [
         "hello",
         "hi",
         "hey",
         "hii"
    ]
        ) or any(
           phrase in message
           for phrase in [
              "good morning",
              "good afternoon",
            "good evening"
            ]
         ):
          return "GREETING"


    # ---------------- Help ----------------

    if any(
        word in message
        for word in [
            "help",
            "assist",
            "commands",
            "options",
            "what can you do"
        ]
    ):
        return "HELP"


    # ---------------- Thank You ----------------

    if any(
        word in message
        for word in [
            "thanks",
            "thank you",
            "thank"
        ]
    ):
        return "THANK_YOU"


    # ---------------- Goodbye ----------------

    if any(
        word in message
        for word in [
            "bye",
            "goodbye",
            "see you",
            "exit"
        ]
    ):
        return "GOODBYE"

    # ---------------- Budget Query ----------------

    if any(
       word in message
       for word in [
         "budget",
         "under",
         "below",
         "less than",
         "within"
       ]
    ):

        if extract_number(message):

          return "BUDGET_QUERY"



# ---------------- Recommendation Query ----------------

    if any(
       word in message
       for word in [
         "recommend",
         "recommendation",
         "suggest",
         "popular",
         "top",
         "similar",
         "should buy"
    ]
):

        return "RECOMMENDATION_QUERY"

    if any(
        word in message
        for word in [
            "price",
            "cost",
            "how much"
        ]
    ):
        return "PRICE_QUERY"

    if any(
        word in message
        for word in [
            "available",
            "availability",
            "stock",
            "have"
        ]
    ):
        return "AVAILABILITY_QUERY"

    if any(
        word in message
        for word in [
            "brand",
            "company",
            "brands",
            "companies",
        ]
    ):
        return "BRAND_QUERY"

    if any(
       phrase in message
       for phrase in [
        "best selling",
        "top selling"
    ]
    ):
       return "BEST_SELLING_QUERY"

    if (
        re.search(r"\b\d+\s?(kg|g|ml|litre|l)\b", message)
        or any(
           word in words
           for word in [
            "pack",
            "size",
            "weight"
           ]
        )
    ):
        return "PACK_SIZE_QUERY"

    if any(
        word in message
        for word in [
            "category",
            "categories"
        ]
    ):
        return "CATEGORY_QUERY"

    if any(
        word in message
        for word in [
            "low stock",
            "running out",
            "restock"
        ]
    ):
        return "LOW_STOCK_QUERY"

    if any(
        word in message
        for word in [
            "rating",
            "review"
        ]
    ):
        return "RATING_QUERY"
    
    if any(
        word in message
        for word in [
           "cheapest",
           "lowest price",
           "cheap"
        ]
    ):
        return "CHEAPEST_QUERY"



    if any(
        word in message
        for word in [
            "health",
            "inventory health"
        ]
    ):
        return "HEALTH_QUERY"

      # Detect brand names directly

    for product in Product.objects.filter(is_active=True):

      if product.brand:

        print(
            "CHECKING BRAND:",
            product.brand.brand_name
        )

        brand_words = product.brand.brand_name.lower().split()

        if any(
            word in message
            for word in brand_words
        ):

            print(
                "BRAND FOUND:",
                product.brand.brand_name
            )

            return "BRAND_QUERY"


    print("INTENT CHECK MESSAGE:", message)

    if any(
      word in message
      for word in [
        "for me",
        "my recommendation",
        "my suggestions",
        "based on my purchase",
        "previous purchase"
      ]
    ):
      return "CUSTOMER_RECOMMENDATION_QUERY"

    return "PRODUCT_SEARCH"
    

def extract_number(message):

    numbers = re.findall(r"\d+", message)

    return float(numbers[0]) if numbers else None

def clean_message(message):

    message = message.lower()

    message = re.sub(
        r"[^\w\s]",
        "",
        message
    )

    remove_words = [
        "please",
        "can",
        "could",
        "would",
        "show",
        "find",
        "give",
        "need",
        "want",
        "looking",
        "looking for",
        "tell",
        "about",
        "do",
        "you",
        "have",
        "the",
        "a",
        "an",
        "is",
        "are",
        "some",
        "what",
        "price",
        "cost",
        "of",
        "how",
        "much"
    ]

    words = message.split()

    words = [
        word
        for word in words
        if word not in remove_words
    ]

    return " ".join(words).strip()

def search_product(name):

    name = clean_message(name)

    products = Product.objects.filter(
        is_active=True
    )

    best_match = None
    highest_score = 0


    for product in products:

        product_name = product.product_name.lower()


        if name in product_name:

            return product


        score = max(

            fuzz.ratio(
                name,
                product_name
            ),

            fuzz.partial_ratio(
                name,
                product_name
            ),

            fuzz.token_sort_ratio(
                name,
                product_name
            )

        )


        if score > highest_score:

            highest_score = score
            best_match = product



    if highest_score >= 50:

        return best_match


    return None


def product_details(product):

    return {

        "id": product.id,

        "name": product.product_name,

        "category":
            product.category.category_name
            if product.category
            else "Unknown",

        "brand":
            product.brand.brand_name
            if product.brand
            else "Unknown",

        "price": float(product.unit_price),

        "stock": get_available_stock(product.id)

    }


def best_selling_products():

    return (
        OnlineOrderItem.objects
        .values("product")
        .annotate(
            total_sales=Sum("quantity")
        )
        .order_by("-total_sales")
    )

def recommendation_score(product):

    score = 0


    # Sales score (40%)

    sales = (
        OnlineOrderItem.objects
        .filter(product=product)
        .aggregate(
            total=Sum("quantity")
        )
    )

    total_sales = sales["total"] or 0


    if total_sales > 0:

        score += min(total_sales / 10, 40)



    # Stock score (30%)

    stock = get_available_stock(product.id)

    if stock > 0:

        score += min(stock / 5, 30)



    # Rating score (20%)

    try:

        rating = ProductRatingSummary.objects.get(
            product=product
        )

        score += float(
            rating.avg_rating
        ) * 4


    except ProductRatingSummary.DoesNotExist:

        pass



    # Inventory health score (10%)

    try:

        health = InventoryHealthScore.objects.get(
            product=product
        )

        score += float(
            health.overall_score
        ) / 10


    except InventoryHealthScore.DoesNotExist:

        pass


    return round(score, 2)

def customer_purchase_history(customer_id):

    purchased_products = (

        OnlineOrderItem.objects

        .filter(
            order__customer_id=customer_id
        )

        .values(
            "product"
        )

        .annotate(
            total_quantity=Sum("quantity")
        )

        .order_by(
            "-total_quantity"
        )

    )

    return purchased_products


def customer_recommendations(customer_id):


    history = customer_purchase_history(
        customer_id
    )


    recommendations = []


    for item in history:


        product = Product.objects.filter(
            id=item["product"]
        ).first()


        if product:


            similar = similar_products(
                product
            )


            for p in similar:


                recommendations.append(
                    product_details(p)
                )


    return recommendations[:5]

def similar_products(product):

    if not product.category:
        return Product.objects.none()

    return Product.objects.filter(
        category=product.category,
        is_active=True
    ).exclude(
        id=product.id
    )[:5]


def search_brand(message):

    message = clean_message(message)

    for product in Product.objects.filter(is_active=True):

        if product.brand:

            if product.brand.brand_name.lower() in message:

                return product.brand

    return None

def chatbot_response(message, customer_id=None):

    intent = detect_intent(message)
    
        # ---------------- Greeting ----------------

    if intent == "GREETING":

        return {
            "intent": intent,

            "message": (
               "Hello! 👋 Welcome to Smart Inventory & Retail DSS.\n"
               "I'm your virtual shopping assistant.\n\n"
               "I can help you find products, check prices, verify stock availability, "
               "recommend products, and answer inventory-related questions."
            ),

             "suggestions": [

                "Recommend products",

                "Show products under 500",

                "Price of Rice",

                "Is Milk Powder available?",

                "Show best selling products",

                "Show categories",

                "Help"

           ]            

        }


    # ---------------- Help ----------------

    if intent == "HELP":

        return {

            "intent": intent,

            "message":
                "I can help you with product and inventory information.",

            "available_queries": [

                "Product Search",

                "Budget Query",

                "Price Query",

                "Brand Query",

                "Availability Query",

                "Category Query",

                "Recommendation",

                "Low Stock",

                "Best Selling",

                "Ratings",

                "Inventory Health"

            ]

        }


    # ---------------- Thank You ----------------

    if intent == "THANK_YOU":

        return {

            "intent": intent,

            "message":
                "You're welcome 😊 Happy to help!"

        }


    # ---------------- Goodbye ----------------

    if intent == "GOODBYE":

        return {

            "intent": intent,

            "message":
                "Thank you for using Smart Inventory & Retail DSS. Have a wonderful day! 👋"

        }

    # ---------------- Recommendation Query ----------------    

    if intent == "RECOMMENDATION_QUERY":

      products = Product.objects.filter(
        is_active=True
      )


      recommendations = []


      for product in products:


         stock = get_available_stock(
            product.id
         )


         if stock > 0:


            details = product_details(
                product
            )


            details["recommendation_score"] = recommendation_score(
                product
            )


            recommendations.append(
                details
            )


      recommendations = sorted(
        recommendations,
        key=lambda x: x["recommendation_score"],
        reverse=True
        )[:5]


      return {

        "intent": intent,

        "message":
            "Recommended products based on sales, stock, ratings and inventory health.",

        "products": recommendations

      }
     
     # ---------------- Customer Recommendation Query ----------------

    if intent == "CUSTOMER_RECOMMENDATION_QUERY":

      customer_id = message.get(
        "customer_id"
      )


      if not customer_id:

        return {

            "intent": intent,

            "message":
            "Customer information is required."

        }


      products = customer_recommendations(
        customer_id
      )


      return {

        "intent": intent,

        "message":
        "Recommended products based on your previous purchases.",

        "products": products

      }

    # ---------------- Budget Query ----------------

    if intent == "BUDGET_QUERY":

        amount = extract_number(message)

        if amount is None:

            return {
                "intent": intent,
                "message": "Please mention your budget amount."
            }

        products = Product.objects.filter(
            unit_price__lte=amount,
            is_active=True
        )

        return {

            "intent": intent,

            "budget": amount,

            "products": [
                product_details(product)
                for product in products[:10]
            ]
        }


    # ---------------- Price Query ----------------

    if intent == "PRICE_QUERY":

       print("PRICE QUERY RECEIVED:", message)

       search_text = clean_message(message)

       print("CLEANED TEXT:", search_text)

       product = search_product(search_text)

       print("FOUND PRODUCT:", product)




       if product:

         return {

            "intent": intent,

            "product": product.product_name,

            "price": float(product.unit_price),

            "brand":
                product.brand.brand_name
                if product.brand
                else "Unknown"

         }


       return {

        "intent": intent,

        "message": "Product not found."

    }
    
    # ---------------- Brand Query ----------------


    if intent == "BRAND_QUERY":

        brand = search_brand(message)

        if brand:

          products = Product.objects.filter(
            brand=brand,
            is_active=True
        )

          return {

            "intent": intent,

            "brand": brand.brand_name,

            "products": [

                product_details(product)

                for product in products

            ]

          }


        products = Product.objects.filter(
            is_active=True
        )[:10]


        return {

            "intent": intent,

             "products": [

             {

                 "name": product.product_name,

                 "brand":
                    product.brand.brand_name
                    if product.brand
                    else "Unknown"

            }

            for product in products

        ]

      }

    # ---------------- Availability Query ----------------

    if intent == "AVAILABILITY_QUERY":

        product = search_product(message)

        if product:

            stock = get_available_stock(product.id)

            return {

                "intent": intent,

                "product": product.product_name,

                "available_quantity": stock,

                "status":
                    "Available"
                    if stock > 0
                    else "Out of stock"

            }

        return {

            "intent": intent,

            "message": "Product not found."

        }


    # ---------------- Pack Size Query ----------------

    if intent == "PACK_SIZE_QUERY":

        product = search_product(message)

        if product:

            return {

                "intent": intent,

                "product": product.product_name,

                "message":
                    "Pack size information is not stored."

            }

        return {

            "intent": intent,

            "message": "Product not found."

        }


    # ---------------- Category Query ----------------

    if intent == "CATEGORY_QUERY":

        categories = Category.objects.all()

        return {

            "intent": intent,

            "categories": [

                category.category_name

                for category in categories

            ]

        }


    # ---------------- Low Stock Query ----------------

    if intent == "LOW_STOCK_QUERY":

        low_stock = []

        for product in Product.objects.filter(
            is_active=True
        ):

            stock = get_available_stock(
                product.id
            )

            if stock <= product.reorder_threshold:

                low_stock.append({

                    "name": product.product_name,

                    "stock": stock,

                    "reorder_threshold":
                        product.reorder_threshold

                })

        return {

            "intent": intent,

            "products": low_stock

        }
    
        # ---------------- Best Selling Query ----------------

    if intent == "BEST_SELLING_QUERY":

        sales = (
            OnlineOrderItem.objects
            .values(
                "product",
                "product__product_name"
            )
            .annotate(
                total_sales=Sum("quantity")
            )
            .order_by("-total_sales")[:10]
        )

        return {

            "intent": intent,

            "products": [

                {
                    "name": item["product__product_name"],
                    "total_sales": item["total_sales"]
                }

                for item in sales

            ]

        }


    # ---------------- Rating Query ----------------

    if intent == "RATING_QUERY":

        ratings = ProductRatingSummary.objects.select_related(
            "product"
        ).order_by(
            "-avg_rating"
        )[:10]

        return {

            "intent": intent,

            "ratings": [

                {

                    "product": rating.product.product_name,

                    "average_rating": float(
                        rating.avg_rating
                    ),

                    "rating_count": rating.rating_count,

                    "trend": rating.trend

                }

                for rating in ratings

            ]

        }


    # ---------------- Inventory Health ----------------

    if intent == "HEALTH_QUERY":

        health = InventoryHealthScore.objects.select_related(
            "product"
        ).order_by(
            "-overall_score"
        )[:10]

        return {

            "intent": intent,

            "products": [

                {

                    "product": item.product.product_name,

                    "health_score": float(
                        item.overall_score
                    ),

                    "status": item.status,

                    "recommended_action":
                        item.recommended_action

                }

                for item in health

            ]

        }
    
    # ---------------- Cheapest Query ----------------

    if intent == "CHEAPEST_QUERY":

      product = Product.objects.filter(
        is_active=True
        ).order_by(
          "unit_price"
        ).first()


      if product:

        return {

            "intent": intent,

            "product": product_details(product)

        }


      return {

        "intent": intent,

        "message": "No products available."

      }

    # ---------------- Product Search ----------------

    if intent == "PRODUCT_SEARCH":

      product = search_product(message)

      if product:

        related = similar_products(product)

        return {

            "intent": intent,

            "product": product_details(product),

            "similar_products": [

                product_details(p)

                for p in related

            ]

        }


      return {

        "intent": intent,

        "message": "Product not found."

     }
    
    # ---------------- Unknown ----------------

    categories = Category.objects.values_list(
        "category_name",
        flat=True
    ).distinct()

    return {

        "intent": "UNKNOWN",

        "message": "Sorry, I couldn't understand your request.",

        "available_queries": [

            "Price query",

            "Budget query",

            "Brand query",

            "Availability query",

            "Category query",

            "Low stock query",

            "Best selling products",

            "Recommendation",

            "Ratings",

            "Inventory health"

        ],

        "categories": list(categories)

    }