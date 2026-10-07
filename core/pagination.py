from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardResultsPagination(PageNumberPagination):
    """
    Shared pagination for list endpoints across the project.

    Query params:
        ?page=<n>       default 1
        ?page_size=<n>  default 25, capped at 100 (prevents a caller
                         requesting the entire table in one response)

    Response shape is deliberately NOT DRF's default
    {count, next, previous, results} — it matches the hand-rolled
    envelope already used by AuditLogListView (users/views.py) so
    every paginated endpoint in the project returns the same shape:

        {
            "results": [...],
            "count": <total matching rows, across all pages>,
            "page": <current page>,
            "page_size": <rows per page>,
            "total_pages": <total pages at this page_size>
        }

    Use by setting `pagination_class = StandardResultsPagination` on
    any generics.ListAPIView / ListCreateAPIView subclass.
    """
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100

    def get_paginated_response(self, data):
        return Response({
            'results': data,
            'count': self.page.paginator.count,
            'page': self.page.number,
            'page_size': self.get_page_size(self.request),
            'total_pages': self.page.paginator.num_pages,
        })