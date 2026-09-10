from datetime import date, timedelta, datetime
from decimal import Decimal

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from sales.services.profit_engine import (
    slow_moving as _slow_moving,
    sales_trend as _sales_trend,
    category_performance as _category_performance,
    store_revenue as _store_revenue,
    calculate_sales_and_profit as _calculate_sales_and_profit,
    aggregate_by_brand_and_category as _aggregate_by_brand_and_category,
    get_top_products as _get_top_products,
)
from sales.models import ItemSalesRecord
from products.models import Product
from core.utils import get_last_sync_date
from django.db.models import Max


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
    GET /api/analytics/sales-trend/?date_from=&date_to=&granularity=daily|weekly|monthly
        (legacy: ?period=daily|weekly|monthly&months=6, still supported)

    Chart-ready sales trend, bucketed by the requested granularity.

    Fix: previously this endpoint only accepted ?period=&months=, which
    meant it could never be pinned to the same date_from/date_to every
    other analytics endpoint on the page uses — the manager could pick
    "Last Month" for the KPIs and get a totally different window on the
    trend chart. date_from/date_to now take priority when both are given;
    period/months (renamed granularity/months) remain as a fallback for
    any caller that just wants "N months back from today".
    """
    granularity = request.query_params.get('granularity') or request.query_params.get('period', 'daily')
    if granularity not in ('daily', 'weekly', 'monthly'):
        return Response(
            {'error': "granularity must be one of: daily, weekly, monthly."},
            status=status.HTTP_400_BAD_REQUEST
        )

    raw_from = request.query_params.get('date_from')
    raw_to = request.query_params.get('date_to')
    date_from = date_to = None
    if raw_from and raw_to:
        try:
            date_from = datetime.strptime(raw_from, '%Y-%m-%d').date()
            date_to = datetime.strptime(raw_to, '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': 'date_from/date_to must be in YYYY-MM-DD format.'}, status=status.HTTP_400_BAD_REQUEST)
        if date_from > date_to:
            return Response({'error': 'date_from must be before date_to.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        months = int(request.query_params.get('months', 6))
    except (TypeError, ValueError):
        return Response({'error': 'months must be an integer.'}, status=status.HTTP_400_BAD_REQUEST)

    trend = _sales_trend(period=granularity, months=months, start_date=date_from, end_date=date_to)

    return Response({
        'granularity': granularity,
        'period': {
            'date_from': str(date_from) if date_from else None,
            'date_to': str(date_to) if date_to else None,
        },
        'months': months if not (date_from and date_to) else None,
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


# ═══════════════════════════════════════════════════════════════════════════════
# Manager Analytics page — consolidated overview + product table
#
# calculate_sales_and_profit(), get_top_products() and
# aggregate_by_brand_and_category() were written and unit-tested in
# profit_engine.py (see F05-A/B/C above) but were never wired to a URL —
# API_Design_Document_v3.2 §26.3 flagged this. This is that wiring.
#
# calculate_sales_and_profit() is called ONCE per period here and its
# product_results are reused for top-products, brand/category aggregation
# and slow_moving flags — same "push the work into one grouped query,
# reuse the Python result" approach the rest of profit_engine.py already
# uses, so the whole overview costs ~6 DB queries total (3 per period,
# current + previous-for-comparison) rather than recomputing per section.
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_pct_change(current, previous):
    """
    (current - previous) / previous * 100, but never Infinity or NaN.
    Returns None when there's no meaningful previous-period baseline —
    the frontend renders None as "No comparable data" rather than a
    misleading 0% or a crashed Infinity%/NaN%.
    """
    if previous in (None, 0, 0.0):
        return None
    return round((current - previous) / previous * 100, 2)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def overview(request):
    """
    GET /api/analytics/overview/?date_from=&date_to=&brand=&category=

    Single consolidated payload for the Manager Analytics page: KPI
    summary, period-over-period comparison, top products, brand/category
    performance, business-rule flags (from slow_moving), dynamically
    generated insights, and data-quality counts — all computed from the
    SAME date range, so no widget on the page can drift onto a different
    period than another.
    """
    try:
        date_from, date_to = _parse_date_range(request)
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    # Equivalent-length immediately-preceding period, for comparison.
    period_days = (date_to - date_from).days + 1
    prev_date_to = date_from - timedelta(days=1)
    prev_date_from = prev_date_to - timedelta(days=period_days - 1)

    product_results, store_revenue_total, total_discount, consistency = \
        _calculate_sales_and_profit(date_from, date_to)
    prev_results, prev_store_revenue, _, _ = \
        _calculate_sales_and_profit(prev_date_from, prev_date_to)

    total_revenue = float(sum(r['total_revenue'] for r in product_results))
    total_profit = float(sum(r['total_profit'] for r in product_results))
    total_units = sum(r['total_qty'] for r in product_results)
    total_cost = total_revenue - total_profit
    margin_pct = round((total_profit / total_revenue * 100), 2) if total_revenue else 0.0

    prev_revenue = float(sum(r['total_revenue'] for r in prev_results))
    prev_profit = float(sum(r['total_profit'] for r in prev_results))
    prev_units = sum(r['total_qty'] for r in prev_results)
    prev_margin = round((prev_profit / prev_revenue * 100), 2) if prev_revenue else None

    changes = {
        'revenue_pct': _safe_pct_change(total_revenue, prev_revenue),
        'profit_pct': _safe_pct_change(total_profit, prev_profit),
        'units_pct': _safe_pct_change(total_units, prev_units),
        'margin_pct': _safe_pct_change(margin_pct, prev_margin) if prev_margin is not None else None,
    }

    top_profit = _get_top_products(date_from, date_to, rank_by='profit', limit=5, product_results=product_results)
    top_qty = _get_top_products(date_from, date_to, rank_by='qty', limit=5, product_results=product_results)
    brands, _unused_categories = _aggregate_by_brand_and_category(date_from, date_to, product_results=product_results)
    categories = _category_performance(date_from, date_to, product_results=product_results)
    flagged = _slow_moving(date_from, date_to, product_results=product_results)

    # Contribution % per brand/category, computed off the same total_revenue.
    for b in brands:
        b['contribution_pct'] = round(b['total_revenue'] / total_revenue * 100, 2) if total_revenue else 0.0
    for c in categories:
        c['contribution_pct'] = round(c['total_revenue'] / total_revenue * 100, 2) if total_revenue else 0.0
    # aggregate_by_brand_and_category sorts brands by profit — re-sort a
    # revenue-ranked copy for the "Revenue by Category"-style charts.
    categories_by_revenue = sorted(categories, key=lambda x: x['total_revenue'], reverse=True)

    high_volume_low_margin = [f for f in flagged if f['flag'] == 'HIGH_VOLUME_LOW_MARGIN']
    low_volume_high_margin = [f for f in flagged if f['flag'] == 'LOW_VOLUME_HIGH_MARGIN']
    rule_flags = [
        {
            'flag': 'HIGH_VOLUME_LOW_MARGIN',
            'count': len(high_volume_low_margin),
            'products': high_volume_low_margin,
        },
        {
            'flag': 'LOW_VOLUME_HIGH_MARGIN',
            'count': len(low_volume_high_margin),
            'products': low_volume_high_margin,
        },
    ]

    # ── Data quality — computed off product_ids actually in this period ──────
    product_ids = [r['product_id'] for r in product_results]
    products_missing_wac = Product.objects.filter(
        id__in=product_ids
    ).filter(
        avg_cost_price__isnull=True
    ).count() + Product.objects.filter(
        id__in=product_ids, avg_cost_price=0
    ).count()
    sales_record_count = ItemSalesRecord.objects.filter(
        sale_date__range=(date_from, date_to)
    ).count()
    latest_sales_date = ItemSalesRecord.objects.filter(
        sale_date__range=(date_from, date_to)
    ).aggregate(latest=Max('sale_date'))['latest']

    data_quality = {
        'sales_records': sales_record_count,
        'products_analysed': len(product_results),
        'products_missing_wac': products_missing_wac,
        'latest_sales_date': str(latest_sales_date) if latest_sales_date else None,
        'last_synchronized': get_last_sync_date(),
        'revenue_mismatch': consistency,
    }

    # ── Insights — generated from data already computed above, no extra queries ──
    insights = []
    if product_results:
        top_rev_product = max(product_results, key=lambda r: r['total_revenue'])
        insights.append({
            'title': 'Top Revenue Product',
            'text': f"{top_rev_product['product_name']} generated the most revenue this period "
                    f"(Rs {float(top_rev_product['total_revenue']):,.2f}).",
        })
    if top_profit:
        insights.append({
            'title': 'Top Profit Product',
            'text': f"{top_profit[0]['product_name']} was the most profitable product "
                    f"(Rs {float(top_profit[0]['total_profit']):,.2f} profit).",
        })
    if brands:
        insights.append({
            'title': 'Best Performing Brand',
            'text': f"{brands[0]['brand_name']} led all brands on profit "
                    f"(Rs {brands[0]['total_profit']:,.2f}).",
        })
    if categories:
        best_cat = max(categories, key=lambda c: c['total_profit'])
        insights.append({
            'title': 'Best Performing Category',
            'text': f"{best_cat['category_name']} was the strongest category by profit "
                    f"(Rs {best_cat['total_profit']:,.2f}, {best_cat['margin_pct']:.1f}% margin).",
        })
    if high_volume_low_margin:
        worst = min(high_volume_low_margin, key=lambda p: p['margin_pct'])
        insights.append({
            'title': 'Lowest Margin, High-Volume Product',
            'text': f"{worst['product_name']} sold {worst['total_qty']} units at only "
                    f"{worst['margin_pct']:.1f}% margin — a discount-engine or pricing review candidate.",
        })
    if low_volume_high_margin:
        best_opp = max(low_volume_high_margin, key=lambda p: p['margin_pct'])
        insights.append({
            'title': 'High-Margin, Low-Volume Opportunity',
            'text': f"{best_opp['product_name']} holds a {best_opp['margin_pct']:.1f}% margin but only "
                    f"{best_opp['total_qty']} units sold — a zone/promotion candidate rather than a discount one.",
        })
    if changes['revenue_pct'] is not None:
        direction = 'up' if changes['revenue_pct'] >= 0 else 'down'
        insights.append({
            'title': 'Revenue Trend',
            'text': f"Revenue is {direction} {abs(changes['revenue_pct']):.1f}% versus the previous period.",
        })
    if changes['profit_pct'] is not None:
        direction = 'up' if changes['profit_pct'] >= 0 else 'down'
        insights.append({
            'title': 'Profit Trend',
            'text': f"Profit is {direction} {abs(changes['profit_pct']):.1f}% versus the previous period.",
        })

    return Response({
        'period': {'date_from': str(date_from), 'date_to': str(date_to)},
        'previous_period': {'date_from': str(prev_date_from), 'date_to': str(prev_date_to)},
        'data_quality': data_quality,
        'summary': {
            'revenue': round(total_revenue, 2),
            'cost': round(total_cost, 2),
            'profit': round(total_profit, 2),
            'margin_pct': margin_pct,
            'units_sold': total_units,
            'discount': round(total_discount, 2),
        },
        'changes': changes,
        'top_products': {
            'profit': [_serialize_product_row(p) for p in top_profit],
            'qty': [_serialize_product_row(p) for p in top_qty],
        },
        'brands': brands,
        'categories': categories,
        'categories_by_revenue': categories_by_revenue,
        'rule_flags': rule_flags,
        'insights': insights,
        # Volume-vs-margin scatter: one point per product with sales this period.
        'portfolio': [
            {
                'product_name': r['product_name'],
                'units_sold': r['total_qty'],
                'revenue': float(r['total_revenue']),
                'profit': float(r['total_profit']),
                'margin_pct': float(r['margin_pct']),
            }
            for r in product_results
        ],
    })


def _serialize_product_row(r):
    return {
        'product_id': r['product_id'],
        'product_name': r['product_name'],
        'brand_name': r['brand_name'],
        'category_name': r['category_name'],
        'units_sold': r['total_qty'],
        'revenue': float(r['total_revenue']),
        'profit': float(r['total_profit']),
        'margin_pct': float(r['margin_pct']),
        'flags': r['flags'],
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def products(request):
    """
    GET /api/analytics/products/?date_from=&date_to=&search=&brand=&category=
        &sort=profit|revenue|qty|margin&page=1&page_size=25

    Server-side paginated/sortable/searchable product performance table —
    per-product WAC and cost fetched only for the current page's products,
    not the full result set.
    """
    try:
        date_from, date_to = _parse_date_range(request)
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    search = request.query_params.get('search', '').strip().lower()
    brand_filter = request.query_params.get('brand', '').strip().lower()
    category_filter = request.query_params.get('category', '').strip().lower()
    sort_key = request.query_params.get('sort', 'profit')
    sort_map = {
        'profit': lambda r: r['total_profit'],
        'revenue': lambda r: r['total_revenue'],
        'qty': lambda r: r['total_qty'],
        'margin': lambda r: r['margin_pct'],
    }
    if sort_key not in sort_map:
        return Response({'error': f"sort must be one of: {', '.join(sort_map)}."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        page = max(1, int(request.query_params.get('page', 1)))
        page_size = min(100, max(1, int(request.query_params.get('page_size', 25))))
    except (TypeError, ValueError):
        return Response({'error': 'page and page_size must be integers.'}, status=status.HTTP_400_BAD_REQUEST)

    product_results, *_ = _calculate_sales_and_profit(date_from, date_to)

    if search:
        product_results = [r for r in product_results if search in r['product_name'].lower()]
    if brand_filter:
        product_results = [r for r in product_results if brand_filter in r['brand_name'].lower()]
    if category_filter:
        product_results = [r for r in product_results if category_filter in r['category_name'].lower()]

    product_results.sort(key=sort_map[sort_key], reverse=True)
    count = len(product_results)
    start = (page - 1) * page_size
    page_rows = product_results[start:start + page_size]

    wac_map = {
        p['id']: p['avg_cost_price']
        for p in Product.objects.filter(
            id__in=[r['product_id'] for r in page_rows]
        ).values('id', 'avg_cost_price')
    }

    results = []
    for i, r in enumerate(page_rows, start=start + 1):
        wac = wac_map.get(r['product_id']) or Decimal('0.00')
        results.append({
            'rank': i,
            'product_id': r['product_id'],
            'product_name': r['product_name'],
            'brand_name': r['brand_name'],
            'category_name': r['category_name'],
            'units_sold': r['total_qty'],
            'revenue': float(r['total_revenue']),
            'wac': float(wac),
            'cost': float(r['total_qty'] * wac),
            'profit': float(r['total_profit']),
            'margin_pct': float(r['margin_pct']),
            'performance_flag': r['flags'][0] if r['flags'] else 'NORMAL',
        })

    return Response({
        'count': count,
        'page': page,
        'page_size': page_size,
        'results': results,
    })