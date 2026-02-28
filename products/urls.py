from django.urls import path
from . import views

urlpatterns = [
    # Brands
    path('brands/', views.BrandListCreateView.as_view(), name='brand-list'),
    path('brands/<int:pk>/', views.BrandDetailView.as_view(), name='brand-detail'),

    # Categories
    path('categories/', views.CategoryListCreateView.as_view(), name='category-list'),
    path('categories/<int:pk>/', views.CategoryDetailView.as_view(), name='category-detail'),

    # Store Zones
    path('zones/', views.StoreZoneListCreateView.as_view(), name='zone-list'),
    path('zones/<int:pk>/', views.StoreZoneDetailView.as_view(), name='zone-detail'),

    # Products — specific URLs FIRST, generic <int:pk> LAST
    path('products/', views.ProductListCreateView.as_view(), name='product-list'),
    path('products/<int:pk>/', views.ProductDetailView.as_view(), name='product-detail'),
]