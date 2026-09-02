import json

from datetime import date
from django.db.models import Avg, Count
from django.db import IntegrityError
from core.authentication import LenientJWTAuthentication
from .models import ChatbotLog, ProductRating, ProductRatingSummary
from .serializers import RatingCreateSerializer, ProductRatingPublicSerializer
from products.serializers import ProductSerializer 
from django.contrib.auth.hashers import make_password, check_password
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from users.audit import log_action
from products.models import Product

from inventory.services.stock import get_available_stock
from inventory.models import StockLedger

from .models import Customer, OnlineOrder, OnlineOrderItem
from .tokens import get_tokens_for_customer
from .authentication import CustomerJWTAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from .chatbot import chatbot_response

class CustomerRegisterView(APIView):

    authentication_classes = []
    permission_classes = [AllowAny]


    def post(self, request):
        print("========== CUSTOMER REGISTER HIT ==========")
        name = request.data.get("name")
        email = request.data.get("email")
        password = request.data.get("password")


        if not all([name, email, password]):

            return Response(
                {
                    "error":
                    "name, email and password are required"
                },
                status=status.HTTP_400_BAD_REQUEST
            )


        if Customer.objects.filter(email=email).exists():

            return Response(
                {
                    "error":
                    "Email already exists"
                },
                status=status.HTTP_400_BAD_REQUEST
            )


        customer = Customer.objects.create(

            name=name,

            email=email,

            password_hash=make_password(password),

            is_active=True

        )


        tokens = get_tokens_for_customer(customer)


        return Response(

            {
                "message":
                "Customer registered successfully",

                "customer_id":
                customer.id,

                "name":
                customer.name,

                "email":
                customer.email,

                "refresh":
                tokens["refresh"],

                "access":
                tokens["access"]

            },

            status=status.HTTP_201_CREATED

        )



class CustomerLoginView(APIView):

    authentication_classes = []
    permission_classes = [AllowAny]


    def post(self, request):

        email = request.data.get("email")
        password = request.data.get("password")


        if not all([email, password]):

            return Response(

                {
                    "error":
                    "email and password required"
                },

                status=status.HTTP_400_BAD_REQUEST
            )


        try:

            customer = Customer.objects.get(

                email=email,

                is_active=True

            )


        except Customer.DoesNotExist:


            return Response(

                {
                    "error":
                    "Invalid credentials"
                },

                status=status.HTTP_401_UNAUTHORIZED

            )



        if not check_password(

            password,

            customer.password_hash

        ):


            return Response(

                {
                    "error":
                    "Invalid credentials"
                },

                status=status.HTTP_401_UNAUTHORIZED

            )



        tokens = get_tokens_for_customer(customer)



        return Response(

            {
                "message":
                "Login successful",

                "customer_id":
                customer.id,

                "name":
                customer.name,

                "email":
                customer.email,

                "refresh":
                tokens["refresh"],

                "access":
                tokens["access"]

            }

        )


# ============================================================
# In orders/views.py:
# 1) DELETE the existing stub `class CustomerProfileView(APIView): ...`
#    and replace it with the version below.
# 2) ADD these imports near the top, with your other imports:
#    from rest_framework.permissions import IsAuthenticated
#    from rest_framework_simplejwt.tokens import RefreshToken
#    from .authentication import CustomerJWTAuthentication
# 3) APPEND CustomerLogoutView and CustomerChangePasswordView below.
# ============================================================


class CustomerProfileView(APIView):

    """
    GET   /api/customer-auth/profile/   — view own profile
    PATCH /api/customer-auth/profile/   — partial update
          Body: any subset of {name, contact_number, address}
    Requires customer JWT (Authorization: Bearer <customer_access_token>).
    """
    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        c = request.user  # CustomerJWTAuthentication resolves this to a Customer
        return Response({
            'customer_id': c.id,
            'name': c.name,
            'email': c.email,
            'contact_number': c.contact_number,
            'address': c.address,
        })

    def patch(self, request):
        c = request.user

        if 'name' in request.data:
            c.name = request.data['name']
        if 'contact_number' in request.data:
            c.contact_number = request.data['contact_number']
        if 'address' in request.data:
            c.address = request.data['address']
        c.save()

        return Response({
            'message': 'Profile updated',
            'name': c.name,
            'contact_number': c.contact_number,
            'address': c.address,
        })


class CustomerLogoutView(APIView):
    """
    POST /api/customer-auth/logout/
    Body: { "refresh": "<refresh_token>" }
    Blacklists the refresh token. Requires
    'rest_framework_simplejwt.token_blacklist' in INSTALLED_APPS.
    """
    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response({'error': 'refresh token is required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            RefreshToken(refresh_token).blacklist()
        except Exception:
            return Response({'error': 'Invalid or already-expired refresh token'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'message': 'Logged out successfully'})


class CustomerChangePasswordView(APIView):
    """
    POST /api/customer-auth/change-password/
    Body: { "old_password": "...", "new_password": "..." }
    """
    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        c = request.user
        old_password = request.data.get('old_password', '')
        new_password = request.data.get('new_password', '')

        if not old_password or not new_password:
            return Response({'error': 'old_password and new_password are required'}, status=status.HTTP_400_BAD_REQUEST)
        if len(new_password) < 6:
            return Response({'error': 'New password must be at least 6 characters'}, status=status.HTTP_400_BAD_REQUEST)
        if not check_password(old_password, c.password_hash):
            return Response({'error': 'old_password is incorrect'}, status=status.HTTP_400_BAD_REQUEST)

        c.password_hash = make_password(new_password)
        c.save()
        return Response({'message': 'Password changed successfully'})


class OrderListCreateView(APIView):

    authentication_classes = [
        LenientJWTAuthentication,
        CustomerJWTAuthentication,
    ]


    permission_classes = [

        IsAuthenticated

    ]


    def get(self, request):
        # New staff GET logic
        ...


    def post(self, request):
        # OLD OrderCreateView.post() BODY
        customer = request.user

        pickup_date = request.data.get("pickup_date")
        time_slot = request.data.get("time_slot")
        items = request.data.get("items")

        if not all([pickup_date, time_slot, items]):
            return Response(
                {"error": "Missing required fields"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if time_slot not in [
            "MORNING",
            "AFTERNOON",
            "EVENING"
        ]:
            return Response(
                {"error": "Invalid pickup time"},
                status=status.HTTP_400_BAD_REQUEST
            )

        order_reference = (
            f"ORD-2026-"
            f"{str(OnlineOrder.objects.count() + 1).zfill(5)}"
        )

        order = OnlineOrder.objects.create(
            customer=customer,
            pickup_date=pickup_date,
            pickup_time_slot=time_slot,
            order_reference=order_reference,
            status="PENDING"
        )

        total = 0

        for item in items:

            product_id = item.get("product_id")

            quantity = int(
                item.get("quantity")
            )

            try:
                product = Product.objects.get(
                    id=product_id
                )

            except Product.DoesNotExist:

                return Response(
                    {
                        "error":
                        f"Product {product_id} not found"
                    },
                    status=status.HTTP_404_NOT_FOUND
                )

            available_stock = get_available_stock(
                product_id
            )

            if quantity > available_stock:

                return Response(
                    {
                        "error":
                        f"Insufficient stock for {product.product_name}",
                        "requested_quantity":
                        quantity,
                        "available_stock":
                        available_stock
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            price = product.unit_price

            OnlineOrderItem.objects.create(
                order=order,
                product=product,
                quantity=quantity,
                unit_price=price
            )

            StockLedger.objects.create(
                product=product,
                transaction_type="SALE_SYNC",
                source="ONLINE_ORDER",
                quantity_change=-quantity,
                reference_id=order.id
            )

            total += price * quantity

        order.total_amount = total
        order.save()

        return Response(
            {
                "message":
                "Order created successfully",
                "order_reference":
                order.order_reference,
                "status":
                order.status,
                "total_amount":
                float(order.total_amount)
            },
            status=status.HTTP_201_CREATED
        )


class OrderListView(APIView):

    authentication_classes = [

        CustomerJWTAuthentication

    ]

    permission_classes = [

        IsAuthenticated

    ]


    def get(self, request):

        customer = request.user


        orders = OnlineOrder.objects.filter(

            customer=customer

        ).order_by("-id")



        response = []



        for order in orders:


            items = OnlineOrderItem.objects.filter(

                order=order

            )


            item_list = []



            for item in items:


                item_list.append(

                    {

                        "product":
                         item.product.product_name,

                        "quantity":
                        item.quantity,

                        "unit_price":
                        float(item.unit_price)

                    }

                )



            response.append(

                {

                    "order_reference":
                    order.order_reference,

                    "pickup_date":
                    order.pickup_date,

                    "pickup_time_slot":
                    order.pickup_time_slot,

                    "status":
                    order.status,

                    "payment_status":
                    order.payment_status,

                    "total_amount":
                    float(order.total_amount),

                    "items":
                    item_list

                }

            )


        return Response(response)


# ============================================================
# SECTION 2 — REPLACE `class OrderStatusUpdateView(APIView):`
# entirely with this.
#
# THE BUG: the current version authenticates with
# CustomerJWTAuthentication and never checks the order belongs to
# the requesting customer — any logged-in customer could PUT any
# order id and move it to CONFIRMED/READY/COMPLETED/CANCELLED.
#
# THE ACTUAL FIX: per the API Design Doc's Role Access Matrix
# (Section 23), PATCH /api/orders/{id}/status/ is STAFF-ONLY —
# customers were never supposed to hit this endpoint at all. So
# the fix isn't "add an ownership check to a customer-facing
# endpoint" — it's routing this correctly as staff-only. A
# customer cancelling their own order is a separate, still-
# unbuilt DELETE endpoint (flagged in the earlier branch review,
# not part of this patch).
#
# Also fixes: PUT -> PATCH (matches the v3.0 spec fix already
# applied everywhere else in the doc), and wires real Audit_Log
# entries via log_action() instead of the bare, useless
# `AuditLog.objects.create(action=...)` call that was there before
# (that was writing to core.models.AuditLog, which only has an
# `action` text field — not the real audit table users/models.py
# defines with user/table_name/old_value/new_value).
# ============================================================

class OrderStatusUpdateView(APIView):

    authentication_classes = [LenientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    ALLOWED_TRANSITIONS = {
        "PENDING": ["CONFIRMED", "CANCELLED"],
        "CONFIRMED": ["READY", "CANCELLED"],
        "READY": ["COMPLETED", "CANCELLED"],
        "COMPLETED": [],
        "CANCELLED": [],
    }

    def patch(self, request, pk):
        try:
            order = OnlineOrder.objects.get(id=pk)
        except OnlineOrder.DoesNotExist:
            return Response(
                {"error": "Order not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        new_status = request.data.get("status")
        if not new_status:
            return Response(
                {"error": "Status required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        new_status = new_status.upper()

        if new_status not in self.ALLOWED_TRANSITIONS.get(order.status, []):
            return Response(
                {"error": f"Cannot move order from {order.status} to {new_status}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_status = order.status
        order.status = new_status
        order.save()

        log_action(
            user=request.user,
            action="ORDER_STATUS_CHANGE",
            table_name="online_order",
            record_id=order.id,
            old_value={"status": old_status},
            new_value={"status": new_status},
            request=request,
        )

        return Response({
            "message": "Order updated successfully",
            "order_reference": order.order_reference,
            "status": order.status,
        })

    # Kept as an alias so nothing breaks if Chalani/Lavanya's
    # frontend is already calling PUT — remove once confirmed
    # everyone's switched to PATCH.
    def put(self, request, pk):
        return self.patch(request, pk)


# ============================================================
# orders/views.py — REPLACE the existing `def chatbot(request):`
# function (the last ~50 lines of the file, currently calling
# chatbot_response(message, customer_id) and returning it as-is)
# with this. Only change: logs every query to ChatbotLog, which
# GET /api/chatbot/logs/ and /logs/{session_id}/ need data for —
# those two endpoints from Section 18 also don't exist yet and
# aren't included in this patch (say the word if you want them too;
# they're a straightforward ListView + filtered detail view over
# ChatbotLog, maybe 20 minutes of work once this is in).
# ============================================================

@csrf_exempt
def chatbot(request):

    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "Invalid JSON format"}, status=400)

    message = body.get("message")
    customer_id = body.get("customer_id")
    session_id = body.get("session_id") or "anonymous"

    if not message:
        return JsonResponse({"error": "message field is required"}, status=400)

    result = chatbot_response(message, customer_id)

    intent = result["intent"]
    if intent not in dict(ChatbotLog.INTENT_CHOICES):
        intent = "UNKNOWN"

    ChatbotLog.objects.create(
        customer_id=customer_id if customer_id else None,
        session_id=session_id,
        user_message=message,
        bot_response=result["bot_response"],
        intent_detected=intent,
        query_success=result["query_success"],
    )

    return JsonResponse(result, safe=True)


# ============================================================
# SECTION 3 — APPEND to the bottom of orders/views.py.
# Ratings CRUD (F14) — Section 19 of the API Design Doc.
# Nothing here existed anywhere in the repo before this patch;
# models and serializers already existed as scaffolding, this
# wires the actual views.
# ============================================================

class RatingCreateView(APIView):
    """
    POST /api/ratings/ — customer submits a 1-5 rating.
    One rating per customer per product (DB unique_together
    already enforces this on ProductRating — we just turn the
    IntegrityError into a clean 400 instead of a 500).
    is_verified is set automatically: True only if this customer
    has a COMPLETED order containing this product.
    """
    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        customer = request.user

        serializer = RatingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        product = serializer.validated_data["product"]

        is_verified = OnlineOrderItem.objects.filter(
            order__customer=customer,
            product=product,
            order__status="COMPLETED",
        ).exists()

        try:
            rating = ProductRating.objects.create(
                product=product,
                customer=customer,
                rating=serializer.validated_data["rating"],
                feedback_text=serializer.validated_data.get("feedback_text", ""),
                is_verified=is_verified,
            )
        except IntegrityError:
            return Response(
                {"error": "You have already rated this product."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "message": "Rating submitted",
                "rating_id": rating.id,
                "is_verified": rating.is_verified,
            },
            status=status.HTTP_201_CREATED,
        )


class ProductRatingListView(APIView):
    """
    GET /api/ratings/product/{product_id}/ — public (No auth per
    Section 19.1), used on the product detail page.

    NOTE: Section 19's intro text says ratings are "internal only —
    not publicly visible", but its own endpoint table marks this GET
    as public for the product page. Those two statements conflict.
    This implementation follows the endpoint table (since that's
    what Chalani needs to build against) but deliberately omits any
    customer-identifying fields (name/email) from the response to
    stay in the spirit of "internal only" — flag this contradiction
    to Randika/the team rather than treating it as resolved.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, product_id):
        ratings = ProductRating.objects.filter(
            product_id=product_id, is_active=True
        ).order_by("-created_at")

        data = [
            {
                "rating": r.rating,
                "feedback_text": r.feedback_text,
                "is_verified": r.is_verified,
                "created_at": r.created_at,
            }
            for r in ratings
        ]
        return Response(data)


class RatingSummaryListView(APIView):
    """
    GET /api/ratings/summary/ — staff, Product_Rating_Summary per
    product (avg_rating, rating_count, verified_count, trend).
    """
    authentication_classes = [LenientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        summaries = ProductRatingSummary.objects.select_related("product").order_by(
            "-calculated_date"
        )
        data = [
            {
                "product_id": s.product_id,
                "product_name": s.product.product_name,
                "period": s.period,
                "avg_rating": float(s.avg_rating),
                "rating_count": s.rating_count,
                "verified_count": s.verified_count,
                "trend": s.trend,
                "calculated_date": s.calculated_date,
            }
            for s in summaries
        ]
        return Response(data)


class RatingSummaryCalculateView(APIView):
    """
    POST /api/ratings/summary/calculate/ — staff triggers the
    monthly summary recalculation. Trend rule per Section 19.2:
    IMPROVING if current avg > last period avg + 0.3,
    DECLINING if < last period avg - 0.3, STABLE otherwise.
    """
    authentication_classes = [LenientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        period = date.today().strftime("%Y-%m")

        product_ids = (
            ProductRating.objects.filter(is_active=True)
            .values_list("product_id", flat=True)
            .distinct()
        )

        updated = []
        for product_id in product_ids:
            ratings = ProductRating.objects.filter(
                product_id=product_id, is_active=True
            )
            agg = ratings.aggregate(avg=Avg("rating"), count=Count("id"))
            avg_rating = round(agg["avg"] or 0, 2)
            rating_count = agg["count"] or 0
            verified_count = ratings.filter(is_verified=True).count()

            previous = (
                ProductRatingSummary.objects.filter(product_id=product_id)
                .exclude(period=period)
                .order_by("-calculated_date")
                .first()
            )

            trend = "STABLE"
            if previous:
                diff = float(avg_rating) - float(previous.avg_rating)
                if diff > 0.3:
                    trend = "IMPROVING"
                elif diff < -0.3:
                    trend = "DECLINING"

            ProductRatingSummary.objects.update_or_create(
                product_id=product_id,
                period=period,
                defaults={
                    "avg_rating": avg_rating,
                    "rating_count": rating_count,
                    "verified_count": verified_count,
                    "trend": trend,
                },
            )
            updated.append(product_id)

        return Response({
            "message": f"Rating summaries recalculated for {len(updated)} product(s).",
            "period": period,
            "product_ids": updated,
        })


class RatingDeactivateView(APIView):
    """
    DELETE /api/ratings/{id}/ — admin deactivates an inappropriate
    rating (soft delete, is_active=False — consistent with every
    other deactivation pattern already used across this codebase).
    """
    authentication_classes = [LenientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        try:
            rating = ProductRating.objects.get(id=pk)
        except ProductRating.DoesNotExist:
            return Response(
                {"error": "Rating not found"}, status=status.HTTP_404_NOT_FOUND
            )

        rating.is_active = False
        rating.save()

        log_action(
            user=request.user,
            action="DEACTIVATE",
            table_name="product_rating",
            record_id=rating.id,
            old_value={"is_active": True},
            new_value={"is_active": False},
            request=request,
        )

        return Response({"message": "Rating deactivated"})






def _release_order_stock(order, reason):
    """
    Reverses the stock deduction OrderListCreateView.post() makes at
    order creation. Uses MANUAL_ADJUSTMENT as the transaction_type
    (the closest existing category — StockLedger.TRANSACTION_TYPES
    has no dedicated ORDER_CANCELLED/ORDER_EXPIRED type yet) with a
    descriptive `source` so it's traceable in
    GET /api/inventory/ledger/. If you'd rather add proper choices,
    it's a one-line model change plus a migration — flagging it as
    optional rather than doing it here, since a stray migration file
    landing in a "local safety net only" branch could cause a
    conflict if it's ever merged after Randika's own migrations move.
    """
    items = OnlineOrderItem.objects.filter(order=order)
    for item in items:
        StockLedger.objects.create(
            product=item.product,
            transaction_type="MANUAL_ADJUSTMENT",
            source=reason,
            quantity_change=item.quantity,
            reference_id=order.id,
        )




class OrderMyOrdersView(APIView):
    """
    GET /api/orders/my-orders/ — Customer token, own order history.
    This is the logic that used to live (mis-scoped) in the old
    OrderListView at orders/list/ — pulled out into its own endpoint
    at the correct URL per Section 17.1.
    """
    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        customer = request.user
        orders = OnlineOrder.objects.filter(customer=customer).order_by("-id")

        response = []
        for order in orders:
            items = OnlineOrderItem.objects.filter(order=order)
            response.append({
                "order_reference": order.order_reference,
                "pickup_date": order.pickup_date,
                "pickup_time_slot": order.pickup_time_slot,
                "status": order.status,
                "payment_status": order.payment_status,
                "total_amount": float(order.total_amount),
                "items": [
                    {
                        "product": item.product.product_name,
                        "quantity": item.quantity,
                        "unit_price": float(item.unit_price),
                    }
                    for item in items
                ],
            })
        return Response(response)



class OrderCancelView(APIView):
    """
    DELETE /api/orders/{id}/ — per Section 17.1: "Customer cancels
    their own order (Customer token) or Staff cancels (Yes token).
    Releases stock reservations. Customer can only cancel their own
    orders — server enforces this."

    Same dual-authenticator trick as OrderListCreateView: try staff
    auth first, fall through to customer auth.
    """
    authentication_classes = [LenientJWTAuthentication, CustomerJWTAuthentication]
    permission_classes = [IsAuthenticated]

    CANCELLABLE_STATUSES = ["PENDING", "CONFIRMED", "READY"]

    def delete(self, request, pk):
        try:
            order = OnlineOrder.objects.get(id=pk)
        except OnlineOrder.DoesNotExist:
            return Response(
                {"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND
            )

        is_customer = isinstance(request.user, Customer)

        if is_customer and order.customer_id != request.user.id:
            return Response(
                {"error": "You can only cancel your own orders."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if order.status not in self.CANCELLABLE_STATUSES:
            return Response(
                {"error": f"Cannot cancel an order in {order.status} status."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_status = order.status
        order.status = "CANCELLED"
        order.cancel_reason = request.data.get("reason", "")
        order.cancelled_by = "CUSTOMER" if is_customer else "STAFF"
        order.save()

        _release_order_stock(order, "ORDER_CANCELLED")

        # log_action's `user` param expects a Django auth_user — a
        # Customer instance isn't compatible with that FK, so we
        # pass None for customer-initiated cancellations. The
        # cancelled_by field on the order itself already records
        # who did it.
        log_action(
            user=None if is_customer else request.user,
            action="ORDER_CANCELLED",
            table_name="online_order",
            record_id=order.id,
            old_value={"status": old_status},
            new_value={"status": "CANCELLED"},
            request=request,
        )

        return Response({
            "message": "Order cancelled",
            "order_reference": order.order_reference,
        })





class OrderReferenceLookupView(APIView):
    """
    GET /api/orders/reference/{ref}/?email=...  (or ?phone=...)
    No auth — but now requires the email or phone used on the order,
    not just the reference number.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, ref):
        contact_email = request.query_params.get("email", "").strip().lower()
        contact_phone = request.query_params.get("phone", "").strip()

        if not contact_email and not contact_phone:
            return Response(
                {"error": "Provide the email or phone number used when placing the order."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            order = OnlineOrder.objects.get(order_reference=ref)
        except OnlineOrder.DoesNotExist:
            # Same response as a contact mismatch below — see note above.
            return Response(
                {"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND
            )

        matches = False
        if contact_email and order.customer.email.strip().lower() == contact_email:
            matches = True
        if contact_phone and order.customer.contact_number.strip() == contact_phone:
            matches = True

        if not matches:
            return Response(
                {"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND
            )

        items = OnlineOrderItem.objects.filter(order=order)
        return Response({
            "order_reference": order.order_reference,
            "status": order.status,
            "pickup_date": order.pickup_date,
            "pickup_time_slot": order.pickup_time_slot,
            "total_amount": float(order.total_amount),
            "items": [
                {"product": item.product.product_name, "quantity": item.quantity}
                for item in items
            ],
        })



# ------------------------------------------------------------
# FIX 2 — orders/overdue/ split: GET is now read-only (list only),
# mutation (auto-expire + stock release + audit log) moved to
# POST /orders/overdue/process/. Same pattern as the
# /api/losses/auto-detect/ fix already in the doc.
#
# _release_order_stock() from patch2 is unchanged and reused here —
# don't duplicate it.
# ------------------------------------------------------------

class OrderOverdueView(APIView):
    """
    GET /api/orders/overdue/ — staff, READ-ONLY. Lists orders in
    READY status past collection_deadline. Does not change anything.
    """
    authentication_classes = [LenientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = date.today()
        overdue_orders = OnlineOrder.objects.filter(
            status="READY",
            collection_deadline__lt=today,
        ).order_by("collection_deadline")

        data = [
            {
                "id": order.id,
                "order_reference": order.order_reference,
                "customer_name": order.customer.name,
                "collection_deadline": order.collection_deadline,
                "days_overdue": (today - order.collection_deadline).days,
            }
            for order in overdue_orders
        ]
        return Response(data)


class OrderOverdueProcessView(APIView):
    """
    POST /api/orders/overdue/process/ — staff. Actually performs the
    auto-expire: moves each overdue READY order to EXPIRED, releases
    its stock, and writes an Audit_Log entry per order. Kept as a
    deliberate, explicit action rather than something that fires on
    every dashboard page load — matches the reasoning already used
    for /api/losses/auto-detect/.
    """
    authentication_classes = [LenientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        today = date.today()
        overdue_orders = OnlineOrder.objects.filter(
            status="READY",
            collection_deadline__lt=today,
        )

        expired_refs = []
        for order in overdue_orders:
            old_status = order.status
            order.status = "EXPIRED"
            order.save()

            _release_order_stock(order, "ORDER_EXPIRED")

            log_action(
                user=request.user,
                action="ORDER_AUTO_EXPIRED",
                table_name="online_order",
                record_id=order.id,
                old_value={"status": old_status},
                new_value={"status": "EXPIRED"},
                request=request,
            )
            expired_refs.append(order.order_reference)

        return Response({
            "message": f"{len(expired_refs)} order(s) auto-expired and stock released.",
            "expired_orders": expired_refs,
        })

