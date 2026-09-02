# ============================================================
# orders/overdue/process/, for the POST split (Fix 2).
# OrderReferenceLookupView's URL pattern is unchanged (still
# orders/reference/<str:ref>/) — Fix 1 only changed what happens
# inside that view, not its route.
# ============================================================

from django.urls import path

from .views import (
    CustomerRegisterView,
    CustomerLoginView,
    CustomerLogoutView,
    CustomerProfileView,
    CustomerChangePasswordView,
    OrderListCreateView,
    OrderStatusUpdateView,
    OrderOverdueView,
    OrderOverdueProcessView,
    OrderMyOrdersView,
    OrderReferenceLookupView,
    OrderCancelView,
    RatingCreateView,
    ProductRatingListView,
    RatingSummaryListView,
    RatingSummaryCalculateView,
    RatingDeactivateView,
    chatbot,
)

urlpatterns = [
    path("customer-auth/register/", CustomerRegisterView.as_view(), name="customer-register"),
    path("customer-auth/login/", CustomerLoginView.as_view(), name="customer-login"),
    path("customer-auth/logout/", CustomerLogoutView.as_view(), name="customer-logout"),
    path("customer-auth/profile/", CustomerProfileView.as_view(), name="customer-profile"),
    path("customer-auth/change-password/", CustomerChangePasswordView.as_view(), name="customer-change-password"),

    # ── Orders (F12) ───────────────────────────────────────
    # Static sub-paths before any <int:pk>/ or <str:ref>/ pattern.
    path("orders/overdue/process/", OrderOverdueProcessView.as_view(), name="order-overdue-process"),
    path("orders/overdue/", OrderOverdueView.as_view(), name="order-overdue"),
    path("orders/my-orders/", OrderMyOrdersView.as_view(), name="order-my-orders"),
    path("orders/reference/<str:ref>/", OrderReferenceLookupView.as_view(), name="order-reference"),

    path("orders/", OrderListCreateView.as_view(), name="order-list-create"),
    path("orders/<int:pk>/status/", OrderStatusUpdateView.as_view(), name="order-status"),
    path("orders/<int:pk>/", OrderCancelView.as_view(), name="order-cancel"),

    # ── Chatbot (F13) ──────────────────────────────────────
    path("chatbot/query/", chatbot, name="chatbot"),

    # ── Ratings (F14) ──────────────────────────────────────
    path("ratings/product/<int:product_id>/", ProductRatingListView.as_view(), name="ratings-by-product"),
    path("ratings/summary/calculate/", RatingSummaryCalculateView.as_view(), name="ratings-summary-calculate"),
    path("ratings/summary/", RatingSummaryListView.as_view(), name="ratings-summary"),
    path("ratings/", RatingCreateView.as_view(), name="ratings-create"),
    path("ratings/<int:pk>/", RatingDeactivateView.as_view(), name="ratings-deactivate"),
]
