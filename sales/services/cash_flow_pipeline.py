"""
F(new) — Pipeline 3: Daily Cash Flow Bill + Item Ingestion

Parses the client's "Daily Cash Flow" report (JasperReports Rep_CashInOut)
and populates DailyBillSummary + BillLineItem. This is Section 27 / 27.7 of
the API Design Document (proposed, pending Randika's sign-off before merge
into dev1 — see 27.6 open items).

STATUS NOTE (11 Aug 2026): the client's report generator is confirmed to
accept an arbitrary date range and always renders the same template. This
module is written to run against a single-day export (date_from == date_to).
The single-day STRUCTURE ITSELF — specifically what a section with zero
rows for that day renders as (see _SECTION_MARKERS handling below)

DOES NOT TOUCH Stock_Ledger OR avg_cost_price. Those remain sourced
exclusively from Pipeline 2 (Item Ledger, exact sku_code match — corrected
in v3.2 26.1 to be PDF-based, not Excel). This module's product matches are
fuzzy and analytics-only, per 27.4/27.5.

THREE PIECES, IN ORDER:
    1. _parse_bills()          -- extract (bill, [items]) structure, Section 27.2
    2. _upsert_bills()         -- idempotent save against (bill_no, date), Section 27.7
    3. _assign_match_status()  -- fuzzy-match items to Product, Section 27.4

Called by: POST /api/sales/upload/cash-flow/ (not yet built — proposed
           endpoint, Section 27.3)
"""

import re
from datetime import datetime
from decimal import Decimal

import pdfplumber
from rapidfuzz import fuzz, process

# ── Section 27.2 — row classification ──────────────────────────────────────

# DATE  7-digit BILL_NO  REASON  CASH_IN  CASH_OUT
_BILL_RE = re.compile(
    r'^(\d{4}/\d{2}/\d{2})\s+(\d{7})\s+(.*?)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$'
)
# PRODUCT_NAME  QTY  UNIT_PRICE  (only ever appears directly under a bill row)
_ITEM_RE = re.compile(r'^(.*?)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$')

# Structural / non-sales markers that must never be parsed as bill or item
# rows. Confirmed against the full July file: matching these plus the
# CASH_IN > 0 rule below correctly separated 5,091 sales bills from 475
# non-sales/structural rows (Section 27.2).
#
# ASSUMPTION FLAGGED: on a real single-day export, the "Cash Out" / "Sup
# Pay" section may not print at all if that day has zero cash-out activity
# (template group-suppression on empty data is common in JasperReports).
# This skip-list only needs to match when the marker IS present — if a
# section is absent on a given day, there's simply nothing here to skip,
# which is harmless. What's NOT yet verified is whether an absent section
# has any other side effect (e.g. a differently-labelled totals row). Treat
# _parse_bills()'s output as needing a spot-check the first time it runs
# against a real single-day file.
_SECTION_MARKERS = {
    "CASH",
    "Cash Out",
    "Sup Pay",
    "DATE BILL NO REASON CASH IN CASH OUT",
    "Samanala Super Mart",
}


def _is_structural_line(line: str) -> bool:
    s = line.strip()
    if s in _SECTION_MARKERS:
        return True
    if s.startswith("Daily Cash Flow") or s.startswith("TOTAL") or s.startswith("BALANCE"):
        return True
    return False


def _parse_bills(pdf_path: str) -> list[dict]:
    """
    Section 27.2. Extracts every qualifying sales bill and its item lines
    from a Cash Flow PDF (single day or bulk range — the parser doesn't
    care, it just walks rows).

    A row is a sales bill iff it matches _BILL_RE AND cash_in > 0. This one
    condition is sufficient (verified against the full July file) — no
    reason-text special-casing needed. It naturally excludes:
      - Sup Pay entries        (no bill_no at all -- never matches _BILL_RE)
      - subtotal/running-total rows (same -- no bill_no)
      - the RET CARD reversal row   (bill_no present, but cash_in = 0.00)

    Returns a list of dicts:
        {
            'bill_no': str, 'date': date, 'reason': str,
            'cash_in': Decimal,
            'items': [ (raw_name, qty, unit_price), ... ],
            'reconciled': bool,   -- Section 27.2/27.7: EXACT match only,
                                      zero tolerance. Sum(qty*price) == cash_in
                                      to the cent, or this is False. No
                                      rounding allowance has been agreed —
                                      see the open note in 27.2.
        }
    """
    lines: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(l for l in text.split("\n") if l.strip())

    bills: list[dict] = []
    current: dict | None = None

    for line in lines:
        if _is_structural_line(line):
            continue

        bill_match = _BILL_RE.match(line)
        if bill_match:
            if current:
                bills.append(current)
            date_str, bill_no, reason, cash_in_str, _cash_out_str = bill_match.groups()
            cash_in = Decimal(cash_in_str.replace(",", ""))
            if cash_in <= 0:
                # RET CARD-type reversal rows -- no items follow, not a sale
                current = None
                continue
            current = {
                "bill_no": bill_no,
                "date": datetime.strptime(date_str, "%Y/%m/%d").date(),
                "reason": reason,
                "cash_in": cash_in,
                "items": [],
            }
            continue

        item_match = _ITEM_RE.match(line)
        if item_match and current is not None:
            name, qty_str, price_str = item_match.groups()
            qty = Decimal(qty_str.replace(",", ""))
            price = Decimal(price_str.replace(",", ""))
            current["items"].append((name.strip(), qty, price))
        # else: either a wrapped product-name continuation line under a
        # bill we're skipping (current is None), or a genuine wrap that
        # swallowed an item's numbers -- both are logged, not silently
        # dropped, by the caller inspecting 'reconciled' below.

    if current:
        bills.append(current)

    for bill in bills:
        line_sum = sum((qty * price for _, qty, price in bill["items"]), Decimal("0"))
        bill["reconciled"] = (line_sum == bill["cash_in"])

    return bills


# ── Section 27.7 — idempotent upsert ────────────────────────────────────────

def _upsert_bills(parsed_bills: list[dict], DailyBillSummary, BillLineItem, UploadLog, upload_log_entry) -> dict:
    """
    Section 27.7. Uniqueness key: (bill_no, date) composite -- NOT bill_no
    alone. bill_no showed zero collisions across all 5,091 sales bills in
    the July file, but that's one month of evidence; whether easyAcc resets
    numbering at a month/year boundary is unverified, so the composite key
    is used defensively.

    Behavior on a re-upload of the same (bill_no, date):
      - identical parsed content  -> no-op (true idempotency, 0 new rows)
      - different parsed content  -> UPDATE the DailyBillSummary row,
        delete-and-reinsert its BillLineItem children, and write an
        UploadLog entry flagged as a correction. Never silently rejected --
        every write is logged, per the mandatory-audit-log principle
        already established in the design doc (Section 25.3 / v3.2).

    Django model classes are passed in rather than imported directly,
    since DailyBillSummary / BillLineItem / UploadLog don't exist as
    migrated models yet (Section 27.3/27.4 -- proposed schema, pending
    Randika's review of the migration before this can run for real).

    Returns: {'inserted': int, 'updated': int, 'unchanged': int}
    """
    inserted = updated = unchanged = 0

    for bill in parsed_bills:
        existing = DailyBillSummary.objects.filter(
            bill_no=bill["bill_no"], date=bill["date"]
        ).first()

        if existing is None:
            record = DailyBillSummary.objects.create(
                bill_no=bill["bill_no"],
                date=bill["date"],
                reason=bill["reason"],
                cash_in=bill["cash_in"],
                reconciled=bill["reconciled"],
            )
            for name, qty, price in bill["items"]:
                BillLineItem.objects.create(
                    bill=record,
                    raw_product_name=name,
                    quantity=qty,
                    unit_price=price,
                    line_amount=qty * price,
                    product=None,
                    match_confidence=None,
                    match_status=None,
                )
            inserted += 1
            continue

        # Compare content to decide no-op vs. correction. Comparing cash_in
        # + item count + item line_amount sum is sufficient to detect any
        # meaningful change without a field-by-field diff.
        existing_items = list(existing.bill_line_items.all())
        existing_sum = sum((i.line_amount for i in existing_items), Decimal("0"))
        new_sum = sum((qty * price for _, qty, price in bill["items"]), Decimal("0"))

        content_unchanged = (
            existing.cash_in == bill["cash_in"]
            and len(existing_items) == len(bill["items"])
            and existing_sum == new_sum
        )

        if content_unchanged:
            unchanged += 1
            continue

        # Content differs -- UPSERT, log as correction, never silent
        existing.cash_in = bill["cash_in"]
        existing.reason = bill["reason"]
        existing.reconciled = bill["reconciled"]
        existing.save()

        existing.bill_line_items.all().delete()
        for name, qty, price in bill["items"]:
            BillLineItem.objects.create(
                bill=existing,
                raw_product_name=name,
                quantity=qty,
                unit_price=price,
                line_amount=qty * price,
                product=None,
                match_confidence=None,
                match_status=None,
            )

        UploadLog.objects.create(
            upload=upload_log_entry,
            action="CASH_FLOW_CORRECTION",
            reference=f"{bill['bill_no']}/{bill['date']}",
            note="Bill content changed on re-upload; existing row updated, not duplicated.",
        )
        updated += 1

    return {"inserted": inserted, "updated": updated, "unchanged": unchanged}


# ── Section 27.4 — fuzzy match-status assignment ────────────────────────────

# Locked thresholds (Section 27, reply to Randika 11 Aug 2026) -- changing
# these later means a migration + reprocessing every BillLineItem row, so
# these are not meant to be tuned casually.
_MATCHED_THRESHOLD = 90
_NEEDS_REVIEW_THRESHOLD = 70


def _classify_score(score: float) -> str:
    if score >= _MATCHED_THRESHOLD:
        return "MATCHED"
    if score >= _NEEDS_REVIEW_THRESHOLD:
        return "NEEDS_REVIEW"
    return "UNMATCHED"


def _assign_match_status(bill_line_items, product_queryset) -> dict:
    """
    Section 27.4. Fuzzy-matches each BillLineItem.raw_product_name against
    Product.product_name using rapidfuzz WRatio -- same scoring function
    used for the 43.7%-below-80 investigation, so the numbers stay
    comparable if this is re-run.

    IMPORTANT: this must run against the LIVE Product table, not the
    495-item Book1.xlsx proxy list used for the initial estimate. The
    proxy-list investigation found 85.2% of low-score cases had zero
    shared words with their best available match, and the file references
    3.76x more distinct product names than the proxy list contains --
    strong evidence the low match rate is a list-coverage artifact, not a
    real data-quality ceiling. This function doesn't resolve that on its
    own; it only produces correct numbers if given the real table.

    Never writes to Stock_Ledger or Product -- read-only against Product,
    write-only to BillLineItem.match_confidence / match_status / product.

    Returns: {'MATCHED': int, 'NEEDS_REVIEW': int, 'UNMATCHED': int}
    """
    product_names = list(product_queryset.values_list("product_name", flat=True))
    id_by_name = dict(product_queryset.values_list("product_name", "id"))

    counts = {"MATCHED": 0, "NEEDS_REVIEW": 0, "UNMATCHED": 0}

    for item in bill_line_items:
        best = process.extractOne(item.raw_product_name, product_names, scorer=fuzz.WRatio)
        score = best[1] if best else 0.0
        status = _classify_score(score)

        item.match_confidence = round(score, 2)
        item.match_status = status
        item.product_id = id_by_name.get(best[0]) if status == "MATCHED" else None
        item.save(update_fields=["match_confidence", "match_status", "product_id"])

        counts[status] += 1

    return counts


# ── Orchestration ────────────────────────────────────────────────────────

def process_daily_cash_flow(pdf_path: str, DailyBillSummary, BillLineItem, UploadLog,
                              upload_log_entry, product_queryset, run_matching: bool = True) -> dict:
    """
    Top-level entry point for POST /api/sales/upload/cash-flow/ (proposed).

    1. Parse the file (Section 27.2)
    2. Idempotent upsert (Section 27.7)
    3. Optionally fuzzy-match new/updated items (Section 27.4) -- can be
       deferred (run_matching=False) if match-status is meant to run as a
       separate batch step rather than inline on upload; not yet decided,
       flagging as an open question rather than assuming.

    Returns a summary dict suitable for the Upload_Log detail view, mirroring
    the shape used by calculate_discounts() in discount_engine.py.
    """
    parsed = _parse_bills(pdf_path)

    reconciled_count = sum(1 for b in parsed if b["reconciled"])
    zero_item_count = sum(1 for b in parsed if not b["items"])

    upsert_result = _upsert_bills(parsed, DailyBillSummary, BillLineItem, UploadLog, upload_log_entry)

    match_result = None
    if run_matching:
        new_items = BillLineItem.objects.filter(
            bill__bill_no__in=[b["bill_no"] for b in parsed],
            bill__date__in=[b["date"] for b in parsed],
            match_status__isnull=True,
        )
        match_result = _assign_match_status(new_items, product_queryset)

    return {
        "bills_parsed": len(parsed),
        "bills_reconciled_exact": reconciled_count,
        "bills_zero_items": zero_item_count,
        "upsert": upsert_result,
        "match_status": match_result,
    }