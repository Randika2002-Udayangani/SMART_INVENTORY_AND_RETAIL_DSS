from datetime import date, timedelta, datetime

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from sales.services.profit_engine import (
    slow_moving as _slow_moving,
    sales_trend as _sales_trend,
    category_performance as _category_performance,
    store_revenue as _store_revenue,
)


# ═══════════════════════════════════════════════════════════════════════════════
# F05-D..G: Slow-Moving / Sales Trend / Category Performance / Store Revenue
# (API Design Doc §10 — analytics/slow-moving/, sales-trend/,
#  category-performance/, store-revenue/)
#
# Deliberately kept in analytics/views.py rather than sales/views.py —
# these 4 endpoints are new analytics-app routes, not additions to
# Randika's existing sales pipeline views. Keeps the diff scoped to
# files M2 has permission on: profit_engine.py + analytics/ app.
#
# Date-range views (all except sales_trend) follow the same
# ?date_from=&date_to= convention as sales.views.profit_summary(),
# defaulting to the last 30 days when omitted. sales_trend() uses
# ?period=&months= instead, per its own spec in §10.
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_date_range(request, default_days=30):
    """
    Shared date_from/date_to parsing, mirroring the validation
    sales.views.profit_summary() already does — kept local to this
    file so it doesn't require importing from sales/views.py.
    """
    raw_to   = request.query_params.get('date_to')
    raw_from = request.query_params.get('date_from')

    date_to = datetime.strptime(raw_to, '%Y-%m-%d').date() if raw_to else date.today()
    date_from = datetime.strptime(raw_from, '%Y-%m-%d').date() if raw_from else date_to - timedelta(days=default_days)

    if date_from > date_to:
        raise ValueError('date_from must be before date_to.')

    return date_from, date_to


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def slow_moving(request):
    """
    GET /api/analytics/slow-moving/?date_from=&date_to=

    Products whose margin/volume diverge from their category average.
    See profit_engine.slow_moving() for the flagging logic.
    """
    try:
        date_from, date_to = _parse_date_range(request)
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    flagged = _slow_moving(date_from, date_to)

    return Response({
        'period': {'date_from': str(date_from), 'date_to': str(date_to)},
        'flagged_products': flagged,
        'count': len(flagged),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def sales_trend(request):
    """
    GET /api/analytics/sales-trend/?period=daily|weekly|monthly&months=6

    Chart-ready sales trend, bucketed by the requested period.
    """
    period = request.query_params.get('period', 'daily')
    if period not in ('daily', 'weekly', 'monthly'):
        return Response(
            {'error': "period must be one of: daily, weekly, monthly."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        months = int(request.query_params.get('months', 6))
    except (TypeError, ValueError):
        return Response({'error': 'months must be an integer.'}, status=status.HTTP_400_BAD_REQUEST)

    trend = _sales_trend(period=period, months=months)

    return Response({
        'period': period,
        'months': months,
        'trend': trend,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def category_performance(request):
    """
    GET /api/analytics/category-performance/?date_from=&date_to=

    Sales and profit grouped by category, ranked by margin_pct descending.
    """
    try:
        date_from, date_to = _parse_date_range(request)
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    ranked = _category_performance(date_from, date_to)

    return Response({
        'period': {'date_from': str(date_from), 'date_to': str(date_to)},
        'categories': ranked,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def store_revenue(request):
    """
    GET /api/analytics/store-revenue/?date_from=&date_to=

    Store-level KPIs from DailyBillSummary — total revenue, discount,
    net revenue, and payment-type breakdown.
    """
    try:
        date_from, date_to = _parse_date_range(request)
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    data = _store_revenue(date_from, date_to)

    return Response(data)