"""
This parser handles the item ledger PDF format
from easyAcc — NOT an Excel file. The plan originally assumed Excel but the
actual easyAcc export for item-level sales is PDF (one file per product).

"""

import re
import pdfplumber
import pandas as pd
from datetime import datetime
from decimal import Decimal, InvalidOperation


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC ENTRY POINT  (called by sales/views.py upload endpoint)
# ─────────────────────────────────────────────────────────────────────────────

def parse_item_sales_pdf(file_obj):
    """
    Parse an easyAcc Item Ledger PDF and return structured sale records.

    Args:
        file_obj: An open file-like object (Django InMemoryUploadedFile or
                  a file opened with open(..., 'rb')).

    Returns:
        {
            'sku_code':     str | None,   # extracted from PDF header
            'product_name': str | None,   # extracted from PDF header
            'records': [                  # list of parsed sale rows
                {
                    'sale_date':      date,
                    'quantity_sold':  int,
                    'unit_price':     Decimal,
                    'total_amount':   Decimal,
                },
                ...
            ],
            'skipped_rows': int,          # rows that could not be parsed
            'errors':       [str],        # human-readable parse warnings
        }
    """
    records      = []
    errors       = []
    skipped_rows = 0
    sku_code     = None
    product_name = None

    with pdfplumber.open(file_obj) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):

            # ── 1. Try to extract SKU + product name from page text ───────────
            raw_text = page.extract_text() or ''
            if not sku_code:
                sku_code, product_name = _extract_header_info(raw_text)

            # ── 2. Extract all tables on this page ────────────────────────────
            tables = page.extract_tables() or []
            if not tables:
                # Fallback: try to read rows from raw text lines
                tables = _lines_to_table(raw_text)

            for table in tables:
                header_row_idx = _find_header_row(table)
                col_map        = _map_columns(table, header_row_idx)

                for row_idx, row in enumerate(table):
                    if row_idx <= header_row_idx:
                        continue   # skip header
                    if _is_empty_or_total_row(row):
                        continue   # skip totals / blank lines

                    record, err = _parse_data_row(row, col_map)
                    if record:
                        records.append(record)
                    else:
                        skipped_rows += 1
                        if err:
                            errors.append(
                                f"Page {page_num}, row {row_idx + 1}: {err}"
                            )

    return {
        'sku_code':     sku_code,
        'product_name': product_name,
        'records':      records,
        'skipped_rows': skipped_rows,
        'errors':       errors,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  HEADER EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

# easyAcc header patterns:
#   "Item : 6713  KIRI HATTI 400g"
#   "Item No. : 6713   Item Name : KIRI HATTI 400g"
#   "6713 - KIRI HATTI 400g"
_HEADER_PATTERNS = [
    r'Item\s*(?:No\.?)?\s*[:\-]\s*(\w+)\s+(?:Item\s+Name\s*[:\-]\s*)?(.+)',
    r'(\d{3,6})\s*[\-–]\s*(.+)',
    r'Code\s*[:\-]\s*(\w+)\s+Name\s*[:\-]\s*(.+)',
]

def _extract_header_info(text):
    """Return (sku_code, product_name) from the PDF's header text, or (None, None)."""
    for line in text.splitlines():
        line = line.strip()
        for pattern in _HEADER_PATTERNS:
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                sku  = m.group(1).strip()
                name = m.group(2).strip()
                # Sanity check: SKU should be numeric or alphanumeric ≤ 10 chars
                if len(sku) <= 10:
                    return sku, name
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
#  TABLE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _find_header_row(table):
    """Return index of the row that contains column headers, default 0."""
    header_keywords = {'date', 'qty', 'quantity', 'amount', 'rate',
                       'price', 'bill', 'invoice', 'no', 'particulars',
                       'outward', 'inward', 'sales', 'total'}
    for idx, row in enumerate(table or []):
        row_text = ' '.join(str(c).lower() for c in row if c)
        hits = sum(1 for kw in header_keywords if kw in row_text)
        if hits >= 2:
            return idx
    return 0


def _map_columns(table, header_row_idx):
    """
    Return a dict mapping semantic names to column indices:
      {'date': 0, 'qty': 2, 'price': 3, 'amount': 4}
    Returns empty dict if header not found.
    """
    if not table or header_row_idx >= len(table):
        return {}

    headers = [str(c).lower().strip() if c else '' for c in table[header_row_idx]]
    col_map = {}

    date_kw    = {'date', 'dt', 'txn date', 'transaction date'}
    qty_kw     = {'qty', 'quantity', 'outward', 'sold', 'sale qty', 'units'}
    price_kw   = {'rate', 'price', 'unit price', 'mrp', 'selling price'}
    amount_kw  = {'amount', 'total', 'value', 'net amount', 'sales amount'}

    for i, h in enumerate(headers):
        if any(k in h for k in date_kw)   and 'date'   not in col_map:
            col_map['date']   = i
        if any(k in h for k in qty_kw)    and 'qty'    not in col_map:
            col_map['qty']    = i
        if any(k in h for k in price_kw)  and 'price'  not in col_map:
            col_map['price']  = i
        if any(k in h for k in amount_kw) and 'amount' not in col_map:
            col_map['amount'] = i

    return col_map


def _is_empty_or_total_row(row):
    """Skip blank rows and rows labelled Total / Grand Total / Balance."""
    if not row or all(c is None or str(c).strip() == '' for c in row):
        return True
    row_text = ' '.join(str(c) for c in row if c).lower()
    return any(t in row_text for t in ('total', 'grand', 'balance', 'opening', 'closing'))


def _lines_to_table(text):
    """
    Fallback: if pdfplumber finds no tables, convert raw text lines into
    a pseudo-table (list of lists of whitespace-split tokens).
    """
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(line.split())
    return [rows] if rows else []


# ─────────────────────────────────────────────────────────────────────────────
#  ROW PARSING
# ─────────────────────────────────────────────────────────────────────────────

_DATE_FORMATS = [
    '%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d',
    '%d/%m/%y', '%d-%m-%y', '%d.%m.%Y',
]

def _parse_date(text):
    text = str(text).strip().split()[0]   # take first token only
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(text):
    try:
        clean = re.sub(r'[^\d.]', '', str(text))
        if clean:
            return Decimal(clean)
    except InvalidOperation:
        pass
    return None


def _parse_data_row(row, col_map):
    """
    Parse one data row.
    Returns (record_dict, None) on success, or (None, error_string) on failure.
    """
    cells = [str(c).strip() if c is not None else '' for c in row]

    # ── Date ─────────────────────────────────────────────────────────────────
    if 'date' in col_map:
        sale_date = _parse_date(cells[col_map['date']])
    else:
        # No explicit date column — scan all cells
        sale_date = None
        for c in cells:
            sale_date = _parse_date(c)
            if sale_date:
                break

    if sale_date is None:
        return None, f"No date found in row: {cells}"

    # ── Quantity ──────────────────────────────────────────────────────────────
    if 'qty' in col_map:
        qty_dec = _parse_decimal(cells[col_map['qty']])
    else:
        qty_dec = None
        for c in cells:
            d = _parse_decimal(c)
            if d and 0 < d < 100_000:
                qty_dec = d
                break

    if qty_dec is None or qty_dec <= 0:
        return None, f"No valid quantity in row: {cells}"
    quantity_sold = int(qty_dec)

    # ── Unit price ────────────────────────────────────────────────────────────
    if 'price' in col_map:
        unit_price = _parse_decimal(cells[col_map['price']])
    else:
        unit_price = None

    # ── Total amount ──────────────────────────────────────────────────────────
    if 'amount' in col_map:
        total_amount = _parse_decimal(cells[col_map['amount']])
    else:
        total_amount = None

    # ── Fill in missing price / amount ────────────────────────────────────────
    if unit_price is None and total_amount and quantity_sold:
        unit_price = (total_amount / quantity_sold).quantize(Decimal('0.01'))
    if total_amount is None and unit_price and quantity_sold:
        total_amount = (unit_price * quantity_sold).quantize(Decimal('0.01'))

    if unit_price is None:
        unit_price = Decimal('0.00')     # price unknown — save row, flag it
        errors_note = "Unit price unknown"
    else:
        errors_note = None

    if total_amount is None:
        total_amount = Decimal('0.00')

    record = {
        'sale_date':     sale_date,
        'quantity_sold': quantity_sold,
        'unit_price':    unit_price,
        'total_amount':  total_amount,
    }
    return record, errors_note




def parse_item_master(file):
    """
    Parse the easyAcc Item Master Excel file (Book1.xlsx format).

    File structure — NO header row:
      Col A (index 0) — seq_number   : ignored
      Col B (index 1) — product_name : mandatory match key
      Col C (index 2) — sinhala_name : ignored
      Col D (index 3) — sku_code     : sparse, only ~10/495 rows
      Col E (index 4) — qty_on_hand  : ignored
      Col F (index 5) — unit_price   : mandatory, must be > 0

    Returns:
        {
            'rows': [
                {
                    'product_name': str,
                    'sku_code':     str | None,
                    'unit_price':   float,
                    'row_num':      int,
                }
            ],
            'skipped': int,
            'errors':  [str],
        }
    """
    try:
        df = pd.read_excel(file, header=None)
    except Exception as e:
        return {'rows': [], 'skipped': 0, 'errors': [f'Could not read Excel file: {str(e)}']}

    rows   = []
    errors = []
    skipped = 0

    seen_skus   = {}
    seen_names  = {}

    for index, row in df.iterrows():
        row_num = index + 1

        if len(row) < 6:
            skipped += 1
            errors.append(f'Row {row_num}: Only {len(row)} columns — expected 6. Skipped.')
            continue

        raw_name     = row.iloc[1] if not pd.isna(row.iloc[1]) else ''
        product_name = str(raw_name).strip()

        if product_name == 'DEFAULT ITEM':
            skipped += 1
            continue

        if not product_name:
            skipped += 1
            errors.append(f'Row {row_num}: Empty product name — skipped')
            continue

        raw_sku  = row.iloc[3] if not pd.isna(row.iloc[3]) else None
        sku_code = str(raw_sku).strip() if raw_sku is not None else None
        if not sku_code or sku_code.lower() in ('nan', 'none', ''):
            sku_code = None

        try:
            unit_price = float(row.iloc[5]) if not pd.isna(row.iloc[5]) else 0.0
        except (ValueError, TypeError):
            unit_price = 0.0

        if unit_price <= 0:
            skipped += 1
            errors.append(f'Row {row_num}: "{product_name}" price={unit_price} — skipped')
            continue

        if sku_code:
            if sku_code in seen_skus:
                skipped += 1
                errors.append(f'Row {row_num}: Duplicate SKU "{sku_code}" (first seen row {seen_skus[sku_code]}) — skipped')
                continue
            seen_skus[sku_code] = row_num

        if not sku_code:
            normalized_name = product_name.lower()
            if normalized_name in seen_names:
                skipped += 1
                errors.append(f'Row {row_num}: Duplicate name "{product_name}" (first seen row {seen_names[normalized_name]}) — skipped')
                continue
            seen_names[normalized_name] = row_num

        rows.append({
            'product_name': product_name,
            'sku_code':     sku_code,
            'unit_price':   unit_price,
            'row_num':      row_num,
        })

    return {
        'rows':    rows,
        'skipped': skipped,
        'errors':  errors,
    }