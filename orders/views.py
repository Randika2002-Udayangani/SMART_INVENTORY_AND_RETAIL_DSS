import json

from django.contrib.auth.hashers import make_password, check_password
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from core.models import AuditLog
from products.models import Product

from inventory.services.stock import get_available_stock
from inventory.models import StockLedger

from .models import Customer, OnlineOrder, OnlineOrderItem
from .tokens import get_tokens_for_customer
from .chatbot import chatbot_response
from .jwt_authentication import CustomerJWTAuthentication


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


class CustomerProfileView(APIView):

    authentication_classes = [

        CustomerJWTAuthentication

    ]


    def get(self, request):

        customer = request.user


        return Response(

            {
                "id":
                customer.id,

                "name":
                customer.name,

                "email":
                customer.email,

                "contact_number":
                customer.contact_number,

                "address":
                customer.address

            }

        )


class OrderCreateView(APIView):

    authentication_classes = [

        CustomerJWTAuthentication

    ]


    def post(self, request):

        customer = request.user


        pickup_date = request.data.get(
            "pickup_date"
        )

        time_slot = request.data.get(
            "time_slot"
        )

        items = request.data.get(
            "items"
        )



        if not all(

            [
                pickup_date,
                time_slot,
                items

            ]

        ):


            return Response(

                {
                    "error":
                    "Missing required fields"
                },

                status=status.HTTP_400_BAD_REQUEST

            )



        if time_slot not in [

            "MORNING",
            "AFTERNOON",
            "EVENING"

        ]:


            return Response(

                {
                    "error":
                    "Invalid pickup time"
                },

                status=status.HTTP_400_BAD_REQUEST

            )


        order = OnlineOrder.objects.create(

          customer=customer,

          pickup_date=pickup_date,

          pickup_time_slot=time_slot,

          status="PENDING"

        )

        order.order_reference = f"ORD-2026-{order.id:05d}"

        order.save()

        total = 0



        for item in items:


            product_id = item.get(
                "product_id"
            )


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


class OrderStatusUpdateView(APIView):

    authentication_classes = [
        CustomerJWTAuthentication
    ]

    def put(self, request, pk):

        try:

            order = OnlineOrder.objects.get(

                id=pk

            )


        except OnlineOrder.DoesNotExist:


            return Response(

                {
                    "error":
                    "Order not found"
                },

                status=status.HTTP_404_NOT_FOUND

            )



        new_status = request.data.get(

            "status"

        )


        if not new_status:


            return Response(

                {
                    "error":
                    "Status required"
                },

                status=status.HTTP_400_BAD_REQUEST

            )



        new_status = new_status.upper()



        allowed = {

            "PENDING":
            [
                "CONFIRMED",
                "CANCELLED"
            ],

            "CONFIRMED":
            [
                "READY",
                "CANCELLED"
            ],

            "READY":
            [
                "COMPLETED",
                "CANCELLED"
            ],

            "COMPLETED":
            [],

            "CANCELLED":
            []

        }



        if new_status not in allowed.get(

            order.status,

            []

        ):


            return Response(

                {
                    "error":
                    "Invalid status transition"
                },

                status=status.HTTP_400_BAD_REQUEST

            )



        order.status = new_status

        order.save()



        AuditLog.objects.create(

            action=

            f"{order.order_reference} changed to {new_status}"

        )



        return Response(

            {
                "message":
                "Order updated successfully",

                "status":
                order.status

            }

        )



@csrf_exempt
def chatbot(request):

    if request.method != "POST":

        return JsonResponse(
            {
                "error": "POST required"
            },
            status=405
        )


    try:

        body = json.loads(
            request.body
        )

    except Exception:

        return JsonResponse(
            {
                "error": "Invalid JSON format"
            },
            status=400
        )

    message = body.get("message")

    customer_id = body.get(
      "customer_id"
    )


    if not message:

       return JsonResponse(
        {
            "error": "message field is required"
        },
        status=400
       )


    result = chatbot_response(
       message,
       customer_id
    )

    return JsonResponse(
    result,
    safe=True
)