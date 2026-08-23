"""
Management command: sync_products_from_cash_flow

Adds genuinely new products discovered in a Cash Flow PDF (Section 27.2
parser) to the real Product table. Safe to re-run -- idempotent via
normalized-name matching, never creates a duplicate, never overwrites an
existing product.

Run with:
    python manage.py sync_products_from_cash_flow /path/to/cash_flow.pdf
    python manage.py sync_products_from_cash_flow /path/to/cash_flow.pdf --dry-run

WHAT GETS SET, AND WHY (read this before running against a real DB):

    product_name    = raw name as printed on the report (preserved, not
                       normalized -- normalization is only used for
                       matching, per the "preserve the original product
                       name when creating a new record" requirement)
    sku_code        = None. Deliberately never fabricated. A synthetic SKU
                       here would collide with the real easyAcc-issued SKU
                       for the same product whenever it eventually shows up
                       via Pipeline 2 (Item Ledger), producing a duplicate
                       instead of a clean match. Leaving it null is the
                       correct "unknown" state, not a bug to fix later.
    unit_price      = the MOST RECENTLY observed selling price for that
                       product across the file (by bill date). Prices do
                       vary bill-to-bill for the same raw name in this
                       data -- most recent is the best available proxy for
                       "current" retail price.
    cost_price      = 0.00. THIS IS A KNOWN, FLAGGED PLACEHOLDER, not a
                       real value -- there is no purchase/GRN record for
                       these products, so there is no real cost to use.
                       Same precedent already accepted elsewhere in this
                       project for the identical reason (WAC/profit_engine
                       cost_price=0 gap, documented as a data gap rather
                       than a code bug). Every product inserted this way
                       MUST get a real cost_price once an actual Purchase
                       is recorded for it -- until then, any profit/WAC/
                       health-score/discount calculation involving these
                       products will be wrong, not just imprecise.
    avg_cost_price  = same as cost_price on first insert, matching the
                       existing convention documented in API Design
                       Document Section 5.4 ("Sets avg_cost_price =
                       cost_price on first insert").
    category, brand = left null. Both are nullable FKs on the real model
                       (confirmed against dev1 products/models.py) -- no
                       need to fabricate either. A human can assign these
                       later; guessing them here risks silently
                       miscategorizing a product no one has looked at yet.
    reorder_threshold = left at the model default (0). Not overridden.
    introduced_date = today (the date it's actually entering the catalog,
                       not the date it happened to first appear in a
                       sales file).
    is_active       = True.

MATCHING / IDEMPOTENCY: identical normalization rule to
product_coverage_report.py -- strip, collapse whitespace, casefold. A
normalized exact match against an existing Product.product_name is
treated as "already exists," no new row created, safe to re-run against
the same or a different file indefinitely. This deliberately does NOT use
fuzzy matching to decide existence -- per the explicit instruction to
avoid aggressive fuzzy auto-matching, only an exact normalized match
counts. Anything less than that is a new product, not a guessed one.

SCOPE NOTE: this creates rows in the real Product table, which is outside
sales/inventory service ownership. Per this project's established
file-ownership + branch-per-fix + PR-review workflow, this should go
through the same review process as everything else that touches shared
schema -- run with --dry-run first, review the report, then get sign-off
before running for real against dev1's database.
"""

import csv
import re
import sys
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from products.models import Product
from sales.services.cash_flow_pipeline import _parse_bills  # Section 27.2 parser


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).casefold()


def _looks_truncated(raw_name: str) -> bool:
    """
    Cheap, deliberately over-inclusive signal for a wrapped/cut-off product
    name -- catches cases reconciliation-based detection structurally
    cannot: when a PDF line wraps but the qty/price stay correctly attached
    to the first line (e.g. "CALIN TEEN SOAP WITH" / "ROSEHIP 75G" on the
    next line), the bill still reconciles exactly because the arithmetic is
    fine -- only the name text is broken. Confirmed against real July data:
    "CALIN TEEN SOAP WITH" reconciles perfectly in multiple bills and was
    NOT caught by the reconciliation filter alone.

    This WILL flag some genuinely complete names as false positives (e.g.
    "GILLETTE BLUE 2", "RANI SANDALWOOD 5 IN 1" -- "N in 1" is a real
    marketing pattern, a trailing digit can be a real model number). That's
    an accepted, deliberate tradeoff: a false positive here just means a
    human spends a few seconds confirming a fine name in the review file.
    A false NEGATIVE means a broken name gets permanently written to the
    database. The asymmetry is why this stays over-inclusive on purpose.
    """
    words = raw_name.strip().split()
    if not words:
        return False
    last = words[-1].strip(".,").lower()
    if last in {"with", "and", "&", "of", "for", "to", "in", "or", "the", "a", "an", "on", "-"}:
        return True
    if re.fullmatch(r"\d{1,2}", words[-1]) and (len(words) < 2 or words[-2].lower() != "in"):
        return True  # bare trailing digit NOT part of an "N IN <digit>" pattern
    return False


class Command(BaseCommand):
    help = "Idempotently sync new products discovered in a Cash Flow PDF into the Product table."

    def add_arguments(self, parser):
        parser.add_argument("pdf_path", type=str)
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be created without writing to the database."
        )

    def handle(self, *args, **options):
        pdf_path = options["pdf_path"]
        dry_run = options["dry_run"]

        bills = _parse_bills(pdf_path)

        # Collapse to one entry per normalized name, keeping the
        # most-recent observed price and first-seen display casing --
        # mirrors the WHITE SUGAR x4 -> 1 record example exactly.
        by_norm = {}
        for bill in bills:
            for raw_name, qty, unit_price in bill["items"]:
                key = _normalize(raw_name)
                entry = by_norm.setdefault(key, {
                    "raw_name": raw_name, "latest_price": unit_price,
                    "latest_date": bill["date"], "occurrences": 0,
                    "any_reconciled": False, "any_unreconciled": False,
                })
                entry["occurrences"] += 1
                if bill["date"] >= entry["latest_date"]:
                    entry["latest_price"] = unit_price
                    entry["latest_date"] = bill["date"]
                if bill["reconciled"]:
                    entry["any_reconciled"] = True
                else:
                    entry["any_unreconciled"] = True

        total_extracted = sum(e["occurrences"] for e in by_norm.values())

        existing_by_norm = {
            _normalize(p): p for p in Product.objects.values_list("product_name", flat=True)
        }

        # Split new candidates into "safe" vs "needs manual review" -- a name
        # that appears ONLY inside bills where item lines don't sum to
        # cash_in is a strong signal it's a wrapped/truncated product name
        # (e.g. "CALIN TEEN SOAP WITH" missing "ROSEHIP 75G" on the next
        # line), not a genuine complete product name. Confirmed empirically
        # against the real July file: ~22% of otherwise-new candidates fall
        # into this bucket, with visibly damaged names in the sample
        # ("DASH LIME FRESH SOAP 3 IN", "BATHI POOJA SRPARY HIT"). These are
        # NEVER auto-created -- writing a broken name permanently into the
        # Item Master is worse than leaving it out until a human corrects it.
        already_existing, to_create, needs_review = [], [], []
        for key, entry in by_norm.items():
            if key in existing_by_norm:
                already_existing.append(entry)
            elif entry["any_reconciled"] and not _looks_truncated(entry["raw_name"]):
                to_create.append(entry)
            else:
                needs_review.append(entry)

        created_records = []
        if not dry_run and to_create:
            with transaction.atomic():
                for entry in to_create:
                    product = Product.objects.create(
                        product_name=entry["raw_name"],
                        sku_code=None,
                        unit_price=entry["latest_price"],
                        cost_price=Decimal("0.00"),
                        avg_cost_price=Decimal("0.00"),
                        category=None,
                        brand=None,
                        reorder_threshold=0,
                        introduced_date=timezone.localdate(),
                        is_active=True,
                    )
                    created_records.append((product.id, entry["raw_name"], entry["latest_price"]))

        # ── Report ──────────────────────────────────────────────────────
        self.stdout.write("Monthly Sales Product Synchronization")
        self.stdout.write("--------------------------------------")
        self.stdout.write(f"Product lines extracted from PDF: {total_extracted}")
        self.stdout.write(f"Unique products found:            {len(by_norm)}")
        self.stdout.write(f"Already in Item Master:            {len(already_existing)}")
        label = "Would add (dry run)" if dry_run else "New products added"
        self.stdout.write(f"{label}:{' ' * (35 - len(label))}{len(to_create)}")
        self.stdout.write(f"Held for manual review (unreliable name):  {len(needs_review)}")
        self.stdout.write("")

        if needs_review:
            review_path = "products_needing_manual_review.csv"
            with open(review_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["raw_product_name", "last_observed_price", "occurrences", "reason"])
                for entry in sorted(needs_review, key=lambda e: -e["occurrences"]):
                    writer.writerow([
                        entry["raw_name"], entry["latest_price"], entry["occurrences"],
                        "Only appears in bills where item lines don't sum to cash_in -- "
                        "likely a wrapped/truncated product name, not a real complete name.",
                    ])
            self.stdout.write(
                self.style.WARNING(
                    f"{len(needs_review)} candidates withheld -- name reliability could not be "
                    f"confirmed. Written to {review_path} for manual correction before adding."
                )
            )
            self.stdout.write("")

        if not dry_run and created_records:
            self.stdout.write("New Products:")
            self.stdout.write(f"{'ID':<6} {'Product Name':<38} {'Unit Price':<12} cost_price")
            self.stdout.write("-" * 70)
            for pid, name, price in created_records:
                self.stdout.write(f"{pid:<6} {name:<38} {price:<12} 0.00 (PLACEHOLDER)")
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"{len(created_records)} products inserted with cost_price=0.00. "
                    "This is a known placeholder, not a real cost -- profit, WAC, health "
                    "score, and discount calculations for these products will be wrong "
                    "until a real Purchase record is entered for each one."
                )
            )
        elif dry_run and to_create:
            self.stdout.write(f"Would create {len(to_create)} products (dry run -- nothing written).")
            for entry in to_create[:20]:
                self.stdout.write(f"  {entry['raw_name']}  (last price: {entry['latest_price']}, seen {entry['occurrences']}x)")
            if len(to_create) > 20:
                self.stdout.write(f"  ... and {len(to_create) - 20} more")