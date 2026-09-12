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
    path('zones/recommendations/calculate/', views.ZoneRecommendationCalculateView.as_view(), name='zone-recommendations-calculate'),
    path('zones/recommendations/last-run/', views.ZoneCalculationRunLatestView.as_view(), name='zone-recommendations-last-run'),
    path('zones/recommendations/<int:pk>/status/', views.ZoneRecommendationStatusUpdateView.as_view(), name='zone-recommendation-status'),
    path('zones/recommendations/', views.ZoneRecommendationListView.as_view(), name='zone-recommendations'),
    path('zones/category-mapping/', views.CategoryZoneMappingView.as_view(), name='zone-category-mapping'),
    path('zones/overrides/<int:pk>/', views.ProductZoneOverrideDetailView.as_view(), name='zone-override-detail'),
    path('zones/overrides/', views.ProductZoneOverrideListCreateView.as_view(), name='zone-override-list'),
    path('zones/', views.StoreZoneListCreateView.as_view(), name='zone-list'),
    path('zones/<int:pk>/', views.StoreZoneDetailView.as_view(), name='zone-detail'),

    # Products
    path('products/', views.ProductListCreateView.as_view(), name='product-list'),
    path('products/import/', views.ItemMasterUploadView.as_view(), name='item-master-upload'),
    path('products/<int:pk>/recalculate-wac/', views.RecalculateWACView.as_view(), name='product-recalculate-wac'),
    path('products/<int:pk>/', views.ProductDetailView.as_view(), name='product-detail'),

    # Commented out — views not yet implemented
    # path('products/<int:pk>/availability/', views.ProductAvailabilityView.as_view(), name='product-availability'),
    # path('products/reclassify/', views.ReclassifyProductsView.as_view(), name='products-reclassify'),
]