"""
POST /api/sales/upload/cash-flow/

Connects the existing Daily Cash Flow parser (sales/services/cash_flow_pipeline.py
-> _parse_bills, already used by the sync_products_from_cash_flow command) to the
live pipeline so the report can be uploaded from the Sales Upload page.

What one upload does (mirrors ItemLedgerPDFUploadView so every downstream
feature -- dashboard charts, profit analytics, lifecycle, health score,
reorder, FEFO stock -- sees cash-flow sales exactly like item-ledger sales):

  1. Parse the PDF into bills + item lines.
  2. Daily_Bill_Summary: one row per bill (CASH), skipping any
     (bill_no, sale_date) that already exists.
  3. Item_Sales_Record: item lines are aggregated per (product, sale_date).
     A (product, date) that already has an Item_Sales_Record is SKIPPED --
     same rule as the item-ledger upload -- so re-uploading the same file, or
     a date already loaded from an Item Ledger, can never double-count sales
     or double-deduct stock.
  4. Online-pickup reconciliation, then FEFO stock deduction, per
     (product, date), inside one transaction (same as item-ledger).

Product matching: exact normalised name first, then fuzzy (rapidfuzz WRatio)
at the locked MATCHED threshold (90) from cash_flow_pipeline. Anything below
that is NOT guessed -- it is skipped and reported so it can be fixed with
`python manage.py sync_products_from_cash_flow <file>` and re-uploaded
(re-upload is safe: already-loaded product/dates are skipped).

Quantities: Item_Sales_Record.quantity_sold is an integer, but loose items
are sold fractionally (e.g. 0.38 kg). Fractions are summed per product per
day and rounded half-up; a day total that rounds to 0 is skipped and
counted in the response.
"""

import io
import json
import re
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from rapidfuzz import fuzz, process
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from inventory.models import PurchaseBatch
from inventory.services.fefo import deduct_stock_fefo
from inventory.services.notifications import create_notification
from inventory.services.online_reconciliation import reconcile_item_ledger_deduction
from products.models import Product
from purchases.models import Purchase
from users.audit import log_action
from users.models import SystemConfig

from .models import DailyBillSummary, ItemSalesRecord, UploadLog
from .services.cash_flow_pipeline import _MATCHED_THRESHOLD, _parse_bills
from products.management.commands.sync_products_from_cash_flow import _looks_truncated

TWO_DP = Decimal("0.01")
LOCAL_TZ = ZoneInfo("Asia/Colombo")
STOCK_BUFFER = 10   # same default buffer as top_up_opening_stock


def _norm(name: str) -> str:
    """Same normalisation rule as sync_products_from_cash_flow."""
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


# The report footer prints "CASH <grand total> 0.00" right after the last bill.
# The parser reads it as an item line (name CASH, qty = the whole period's
# cash total, price 0.00) and attaches it to the last bill, so it must never
# reach products or stock. Real item lines always have a name, a positive
# quantity and a positive unit price.
_NOISE_NAMES = {"CASH", "CASH OUT", "CASH IN", "TOTAL", "BALANCE", "SUB TOTAL", "SUBTOTAL"}
_MAX_LINE_QTY = Decimal("100000")
_MIN_FUZZY_NAME_LEN = 6      # shorter names are matched exactly only


_DATE_LIKE = re.compile(r"^\d{4}/\d{2}/\d{2}$")   # daily-total rows: "2026/09/03 195,667.78 0.00"


def _valid_items(bill):
    for raw_name, qty, price in bill["items"]:
        name = (raw_name or "").strip()
        if not name or name.upper() in _NOISE_NAMES or _DATE_LIKE.match(name):
            continue
        if qty <= 0 or price <= 0 or qty > _MAX_LINE_QTY:
            continue
        yield name, qty, price


def _create_missing_products(bills, overrides=None):
    """
    Same rules as `manage.py sync_products_from_cash_flow`, run automatically
    on every cash-flow upload so new products exist before sales are matched.

      * only an exact normalised-name match counts as "already exists"
      * names that look truncated/wrapped, or that only appear in bills that
        do not reconcile, are NOT created (returned for manual review)
      * sku_code None, cost_price 0.00 (placeholder), unit_price = latest
        price seen in the file, no category/brand, is_active True
    Idempotent: a second run creates nothing.

    `overrides` ({printed name: corrected name}) are names a person reviewed
    and approved on the page: they are created under the corrected name even
    if they would otherwise be held.
    Returns (created_names, held_names).
    """
    overrides = overrides or {}
    approved = {_norm(v) for v in overrides.values() if v and v.strip()}
    by_norm = {}
    for bill in bills:
        for printed_name, qty, unit_price in _valid_items(bill):
            raw_name = overrides.get(printed_name, printed_name).strip()
            key = _norm(raw_name)
            entry = by_norm.setdefault(key, {
                "raw_name": raw_name, "latest_price": unit_price,
                "latest_date": bill["date"], "any_reconciled": False,
            })
            if bill["date"] >= entry["latest_date"]:
                entry["latest_price"] = unit_price
                entry["latest_date"] = bill["date"]
            if bill["reconciled"]:
                entry["any_reconciled"] = True

    existing = {_norm(n) for n in Product.objects.values_list("product_name", flat=True)}
    to_create, held = [], []
    for key, e in by_norm.items():
        if key in existing:
            continue
        if key in approved or (e["any_reconciled"] and not _looks_truncated(e["raw_name"])):
            to_create.append(e)
        else:
            held.append(e["raw_name"])

    with transaction.atomic():
        for e in to_create:
            Product.objects.create(
                product_name=e["raw_name"][:150],
                sku_code=None,
                unit_price=e["latest_price"],
                cost_price=Decimal("0.00"),
                avg_cost_price=Decimal("0.00"),
                category=None, brand=None,
                reorder_threshold=0,
                introduced_date=timezone.localdate(),
                is_active=True,
            )
    return [e["raw_name"] for e in to_create], held


# ── Review storage (files under MEDIA_ROOT/cash_flow_reviews, no migration) ───
REVIEW_DIR = Path(settings.MEDIA_ROOT) / "cash_flow_reviews"


def _review_paths(rid):
    return REVIEW_DIR / f"{int(rid)}.pdf", REVIEW_DIR / f"{int(rid)}.json"


def _save_review(rid, file_name, pdf_bytes, items, count):
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path, json_path = _review_paths(rid)
    pdf_path.write_bytes(pdf_bytes)
    json_path.write_text(json.dumps({
        "upload_id": int(rid), "file_name": file_name,
        "created_at": timezone.now().isoformat(),
        "review_count": count, "review_items": items,
    }))


def _load_review(rid):
    _, json_path = _review_paths(rid)
    if not json_path.exists() or not _review_paths(rid)[0].exists():
        return None
    return json.loads(json_path.read_text())


def _clear_review(rid):
    for path in _review_paths(rid):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _list_reviews():
    if not REVIEW_DIR.exists():
        return []
    out = []
    for jp in REVIEW_DIR.glob("*.json"):
        try:
            data = json.loads(jp.read_text())
        except ValueError:
            continue
        if _review_paths(data["upload_id"])[0].exists():
            out.append(data)
    return sorted(out, key=lambda d: d["created_at"], reverse=True)


def _read_overrides(request):
    """
    Optional form field `name_overrides`: JSON object {printed name: corrected
    name} for names a person reviewed and approved on the Sales Upload page.
    Anything malformed or empty is ignored (nothing is guessed).
    """
    raw = request.data.get("name_overrides")
    if not raw:
        return {}
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for printed, corrected in data.items():
        if not isinstance(printed, str) or not isinstance(corrected, str):
            continue
        corrected = re.sub(r"\s+", " ", corrected.strip())[:150]
        if (not corrected or corrected.upper() in _NOISE_NAMES
                or _DATE_LIKE.match(corrected)):
            continue
        out[printed] = corrected
    return out


def _process_cash_flow(request, pdf_bytes, file_name, overrides, review_id=None, notify=True):
    """Everything an upload does (see module docstring). Returns a Response."""
    upload_log = UploadLog.objects.create(
        file_name=file_name[:150],
        # ITEM_SALES: this upload creates Item_Sales_Records, and that
        # type already drives the last-sync stamp and the Sales Upload
        # page label. No model/choices change -> no migration needed.
        upload_type="ITEM_SALES",
        status="PARTIAL",
        error_message="",
        uploaded_by=request.user.id if request.user and request.user.is_authenticated else None,
    )

    try:
        bills = _parse_bills(io.BytesIO(pdf_bytes))
        if not bills:
            upload_log.status = "FAILED"
            upload_log.error_message = (
                "No sales bills found. Is this the Daily Cash Flow report?"
            )
            upload_log.save()
            return Response({"error": upload_log.error_message}, status=status.HTTP_400_BAD_REQUEST)

        warnings = []

        # ── 1. Daily_Bill_Summary (skip existing bill_no + date) ─────────
        dates = [b["date"] for b in bills]
        existing_bills = set(
            DailyBillSummary.objects
            .filter(sale_date__gte=min(dates), sale_date__lte=max(dates))
            .values_list("bill_no", "sale_date")
        )
        new_bill_rows = []
        for b in bills:
            key = (b["bill_no"], b["date"])
            if key in existing_bills:
                continue
            existing_bills.add(key)
            new_bill_rows.append(DailyBillSummary(
                sale_date=b["date"],
                bill_no=b["bill_no"],
                customer_name=(b["reason"] or "")[:150],
                gross_amount=b["cash_in"],
                discount=Decimal("0"),
                final_amount=b["cash_in"],
                payment_type="CASH",
                upload=upload_log,
            ))
        DailyBillSummary.objects.bulk_create(new_bill_rows, batch_size=1000)

        # ── 1b. Auto-add products that are in the PDF but not in the table ─
        created_names, held_names = _create_missing_products(bills, overrides)

        # ── 2. Match item names to products ──────────────────────────────
        products = list(Product.objects.all().only("id", "product_name", "unit_price", "cost_price", "avg_cost_price"))
        exact = {}
        for p in products:
            exact.setdefault(_norm(p.product_name), p)
        fuzzy_names = list(exact.keys())

        match_cache = {}          # raw name -> (Product | None, method)
        fuzzy_used = {}           # raw name -> matched product name (for review)

        def match(raw_name):
            raw_name = overrides.get(raw_name, raw_name)
            key = _norm(raw_name)
            if key in match_cache:
                return match_cache[key][0]
            if key in exact:
                match_cache[key] = (exact[key], "exact")
                return exact[key]
            best = (process.extractOne(key, fuzzy_names, scorer=fuzz.WRatio)
                    if len(key) >= _MIN_FUZZY_NAME_LEN else None)
            if best and best[1] >= _MATCHED_THRESHOLD:
                prod = exact[best[0]]
                match_cache[key] = (prod, "fuzzy")
                fuzzy_used[raw_name] = prod.product_name
                return prod
            match_cache[key] = (None, "none")
            return None

        # ── 3. Aggregate item lines per (product, date) ──────────────────
        agg = defaultdict(lambda: {"qty": Decimal("0"), "amount": Decimal("0")})
        unmatched = defaultdict(lambda: Decimal("0"))   # raw name -> revenue
        unmatched_info = {}       # raw name -> details for the review table
        for b in bills:
            for raw_name, qty, price in _valid_items(b):
                prod = match(raw_name)
                line_amount = qty * price
                if prod is None:
                    unmatched[raw_name] += line_amount
                    info = unmatched_info.setdefault(
                        raw_name, {"qty": Decimal("0"), "count": 0, "price": price, "date": b["date"]})
                    info["qty"] += qty
                    info["count"] += 1
                    if b["date"] >= info["date"]:
                        info["price"], info["date"] = price, b["date"]
                    continue
                slot = agg[(prod.id, b["date"])]
                slot["qty"] += qty
                slot["amount"] += line_amount

        # ── 3b. Auto stock top-up (same idea as top_up_opening_stock) ────
        # Historical sales in this dataset exceed recorded purchases, so
        # a sale can be larger than all sellable stock. Without this, FEFO
        # would deduct only what exists and silently drop the rest
        # (shortfall), leaving stock overstated after a later top-up.
        # Here the gap (+ buffer) is added FIRST under one
        # OPENING-STOCK-RECONCILE purchase, then the FULL sale is deducted.
        topup = {"purchase": None, "products": 0, "units": 0}

        def top_up_gap(product, needed):
            available = (
                PurchaseBatch.objects
                .filter(product_id=product.id,
                        status__in=["ACTIVE", "PENDING_EXPIRY"],
                        remaining_quantity__gt=0)
                .aggregate(s=Sum("remaining_quantity"))["s"] or 0
            )
            gap = needed - available
            if gap <= 0:
                return
            if topup["purchase"] is None:
                ref = (Purchase.objects
                       .filter(invoice_number__icontains="OPENING-STOCK")
                       .order_by("purchase_date").first())
                if ref is None:
                    return      # no opening-stock supplier to reuse -> leave as shortfall
                today = datetime.now(LOCAL_TZ).date()
                topup["purchase"] = Purchase.objects.create(
                    supplier=ref.supplier,
                    purchase_date=today,
                    invoice_number=f"OPENING-STOCK-RECONCILE-{today.isoformat()}",
                    total_amount=0,
                )
            qty = gap + STOCK_BUFFER
            cost = product.avg_cost_price or product.cost_price or Decimal("0")
            PurchaseBatch.objects.create(
                purchase=topup["purchase"], product=product,
                quantity_received=qty, remaining_quantity=qty,
                cost_price=cost, expiry_date=None, status="ACTIVE",
            )
            Purchase.objects.filter(pk=topup["purchase"].pk).update(
                total_amount=F("total_amount") + qty * cost)
            topup["products"] += 1
            topup["units"] += qty

        # ── 4. Per (product, date): record + reconcile + FEFO ────────────
        product_by_id = {p.id: p for p in products}
        inserted = skipped_existing = skipped_zero = 0
        shortfalls = 0
        double_deduction_risks = 0

        for (product_id, sale_date), slot in sorted(agg.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            qty = int(slot["qty"].quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if qty <= 0:
                skipped_zero += 1
                continue
            product = product_by_id[product_id]
            total_amount = slot["amount"].quantize(TWO_DP, rounding=ROUND_HALF_UP)
            unit_price = (total_amount / qty).quantize(TWO_DP, rounding=ROUND_HALF_UP)

            with transaction.atomic():
                # Serialise concurrent uploads for this product first.
                list(PurchaseBatch.objects.select_for_update().filter(product_id=product_id))

                if ItemSalesRecord.objects.filter(product_id=product_id, sale_date=sale_date).exists():
                    skipped_existing += 1
                    continue

                record = ItemSalesRecord.objects.create(
                    product=product,
                    sale_date=sale_date,
                    quantity_sold=qty,
                    unit_price=unit_price,
                    total_amount=total_amount,
                    upload=upload_log,
                )
                inserted += 1

                reconciliation = reconcile_item_ledger_deduction(product_id, sale_date, qty)
                if reconciliation["risk"]:
                    double_deduction_risks += 1
                    warnings.append(
                        f'{sale_date}: DOUBLE_DEDUCTION_RISK - "{product.product_name}" sold {qty} '
                        f'< confirmed online pickups {reconciliation["confirmed_online_quantity"]} '
                        f'(orders {reconciliation["order_ids"]}). No stock deducted for this date.'
                    )
                    continue

                to_deduct = reconciliation["remaining_to_deduct"]
                if to_deduct == 0:
                    continue

                top_up_gap(product, to_deduct)

                # source kept as SALE_SYNC_ITEM_LEDGER so reconcile_stock_fefo
                # and detect_double_deduction treat this like every other
                # live sale sync.
                fefo = deduct_stock_fefo(
                    product_id=product_id,
                    quantity=to_deduct,
                    source="SALE_SYNC_ITEM_LEDGER",
                    reference_id=record.id,
                )
                if fefo["shortfall"] > 0:
                    shortfalls += 1
                    warnings.append(
                        f'{sale_date}: "{product.product_name}" sold {to_deduct} but only '
                        f'{fefo["deducted"]} could be deducted (shortfall {fefo["shortfall"]}).'
                    )

        if unmatched:
            top = sorted(unmatched.items(), key=lambda kv: kv[1], reverse=True)[:10]
            warnings.append(
                f"{len(unmatched)} product name(s) not matched to the product table "
                f"(skipped, Rs. {sum(unmatched.values()).quantize(TWO_DP)} of line revenue). "
                f"Top: " + "; ".join(f"{n} (Rs. {v.quantize(TWO_DP)})" for n, v in top) +
                ". Review them on the page (fix the name, approve, apply)."
            )

        SystemConfig.objects.update_or_create(
            key="last_item_ledger_sync",
            defaults={
                "value": str(timezone.now().date()),
                "description": "Last item ledger PDF sync date",
            },
        )

        nothing_new = (not new_bill_rows) and inserted == 0
        if nothing_new and skipped_existing == 0 and skipped_zero == 0:
            upload_log.status = "FAILED"
        elif warnings:
            upload_log.status = "PARTIAL"
        else:
            upload_log.status = "SUCCESS"
        upload_log.error_message = "\n".join(warnings)[:2000]
        upload_log.save()

        if upload_log.status == "SUCCESS":
            log_action(
                user=request.user, action="CREATE", table_name="upload_log",
                record_id=upload_log.id, old_value=None,
                new_value={
                    "filename": upload_log.file_name,
                    "upload_type": "CASH_FLOW",
                    "status": upload_log.status,
                },
                request=request,
            )

        review_items = []
        for raw_name, revenue in sorted(unmatched.items(), key=lambda kv: kv[1], reverse=True)[:150]:
            info = unmatched_info[raw_name]
            best = process.extractOne(_norm(raw_name), fuzzy_names, scorer=fuzz.WRatio)
            suggestion = exact[best[0]].product_name if best and best[1] >= 70 else ""
            review_items.append({
                "raw_name": raw_name,
                "unit_price": str(info["price"]),
                "times_sold": info["count"],
                "units": str(info["qty"].quantize(TWO_DP)),
                "revenue": str(revenue.quantize(TWO_DP)),
                "suggestion": suggestion,
                "suggestion_score": int(best[1]) if suggestion else 0,
            })

        total_cash_in = sum((b["cash_in"] for b in bills), Decimal("0"))
        # ── Keep names that still need a person's approval ───────────────
        # The PDF and the review list are stored on the server, so the
        # reviewer can leave this page, come back later, or open it from
        # the notification, and approve without uploading the file again.
        rid = review_id or upload_log.id
        if review_items:
            _save_review(rid, file_name, pdf_bytes, review_items, len(unmatched))
            if notify:
                create_notification(
                    type="UPLOAD_REVIEW", priority="MEDIUM",
                    title="Cash flow upload needs your review",
                    message=(f"{file_name}: {len(unmatched)} product name(s) could not be "
                             f"matched. Open Sales Upload to approve or skip them."),
                    reference_table="upload_log", reference_id=rid,
                )
        else:
            _clear_review(rid)

        return Response({
            "message": "Daily Cash Flow upload complete",
            "date_from": str(min(dates)),
            "date_to": str(max(dates)),
            "bills_parsed": len(bills),
            "bills_total_cash_in": str(total_cash_in.quantize(TWO_DP)),
            "bills_inserted": len(new_bill_rows),
            "bills_already_present": len(bills) - len(new_bill_rows),
            "inserted": inserted,
            "skipped": skipped_existing + skipped_zero,
            "sales_records_inserted": inserted,
            "sales_records_skipped_existing": skipped_existing,
            "sales_records_skipped_zero_qty": skipped_zero,
            "products_fuzzy_matched": len(fuzzy_used),
            "products_unmatched": len(unmatched),
            "new_products_created": len(created_names),
            "new_product_names": created_names[:20],
            "names_held_for_review": len(held_names),
            "review_count": len(unmatched),
            "review_upload_id": rid if review_items else None,
            "review_items": review_items,
            "stock_shortfalls": shortfalls,
            "stock_topped_up_products": topup["products"],
            "stock_topped_up_units": topup["units"],
            "double_deduction_risks": double_deduction_risks,
            "upload_log_id": upload_log.id,
            "errors": warnings[:10],
        }, status=status.HTTP_201_CREATED)

    except Exception as exc:
        upload_log.status = "FAILED"
        upload_log.error_message = str(exc)[:2000]
        upload_log.save()
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class CashFlowUploadView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        file = request.FILES.get("file")
        if not file:
            return Response({"error": "No file uploaded"}, status=status.HTTP_400_BAD_REQUEST)
        if not file.name.lower().endswith(".pdf"):
            return Response({"error": "File must be a PDF"}, status=status.HTTP_400_BAD_REQUEST)
        return _process_cash_flow(request, file.read(), file.name, _read_overrides(request))


class CashFlowReviewListView(APIView):
    """GET /api/sales/upload/cash-flow/review/ - uploads still waiting for name approval."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"reviews": _list_reviews()})


class CashFlowReviewApplyView(APIView):
    """
    POST /api/sales/upload/cash-flow/review/<id>/apply/   {"name_overrides": {printed: corrected}}
    Re-runs the stored PDF with the approved names. Already-saved bills/sales are skipped.
    """
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    permission_classes = [IsAuthenticated]

    def post(self, request, upload_id):
        data = _load_review(upload_id)
        if data is None:
            return Response({"error": "This review no longer exists."}, status=status.HTTP_404_NOT_FOUND)
        overrides = _read_overrides(request)
        if not overrides:
            return Response({"error": "No approved names were sent."}, status=status.HTTP_400_BAD_REQUEST)
        pdf_bytes = _review_paths(upload_id)[0].read_bytes()
        return _process_cash_flow(request, pdf_bytes, data["file_name"], overrides,
                                  review_id=upload_id, notify=False)


class CashFlowReviewDismissView(APIView):
    """POST /api/sales/upload/cash-flow/review/<id>/dismiss/ - leave the remaining names out."""
    permission_classes = [IsAuthenticated]

    def post(self, request, upload_id):
        _clear_review(upload_id)
        return Response({"message": "Review dismissed."})