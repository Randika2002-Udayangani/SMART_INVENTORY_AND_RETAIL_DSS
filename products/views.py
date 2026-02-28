

# Create your views here.
from rest_framework import generics, permissions
from .models import Brand, Category, StoreZone, Product
from .serializers import (
    BrandSerializer, CategorySerializer,
    StoreZoneSerializer, ProductSerializer
)


class BrandListCreateView(generics.ListCreateAPIView):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer


class BrandDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer


class CategoryListCreateView(generics.ListCreateAPIView):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer


class CategoryDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer


class StoreZoneListCreateView(generics.ListCreateAPIView):
    queryset = StoreZone.objects.all()
    serializer_class = StoreZoneSerializer


class StoreZoneDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = StoreZone.objects.all()
    serializer_class = StoreZoneSerializer


class ProductListCreateView(generics.ListCreateAPIView):
    serializer_class = ProductSerializer
    permission_classes = [permissions.AllowAny]  # public — v3.0 fix

    def get_queryset(self):
        queryset = Product.objects.filter(is_active=True)
        category = self.request.query_params.get('category')
        brand = self.request.query_params.get('brand')
        search = self.request.query_params.get('search')
        if category:
            queryset = queryset.filter(category__id=category)
        if brand:
            queryset = queryset.filter(brand__id=brand)
        if search:
            queryset = queryset.filter(product_name__icontains=search)
        return queryset


class ProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [permissions.AllowAny]  # public — v3.0 fix