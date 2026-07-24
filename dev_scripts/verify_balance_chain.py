"""
Balance verification script.

Checks whether every CASH SALE row in an item ledger PDF is a real, distinct
inventory movement by validating: previous_balance - OUT == current_balance,
in sequence, for every row -- including rows that share a bill number with
another row on the same date.

If this holds true for 100% of rows, it proves the ledger treats every row
as a real movement regardless of bill number repetition -- meaning bill-number
dedup should be removed (Option A), not tightened (Option B).

USAGE:
    python verify_balance_chain.py path/to/ledger.pdf
"""

import sys
import pdfplumber
import datetime


def parse_rows(pdf_path):
    """
    Extract EVERY ledger row (sales, purchases, opening stock -- anything with
    IN/OUT/Balance numbers), in PDF order. Needed so the balance chain can be
    verified across ALL movements, not just sales -- otherwise purchase/restock
    rows look like "gaps" in the chain.
    """
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            for line in text.split('\n'):
                parts = line.strip().split()
                if len(parts) < 5:
                    continue
                try:
                    sale_date = datetime.datetime.strptime(parts[0], '%Y/%m/%d').date()
                except ValueError:
                    continue

                is_sale = 'CASH' in line and 'SALE' in line

                numeric_parts = []
                for p in parts:
                    try:
                        numeric_parts.append(float(p))
                    except ValueError:
                        continue
                if len(numeric_parts) < 3:
                    continue  # need IN, OUT, Balance

                in_qty   = numeric_parts[-3]
                out_qty  = numeric_parts[-2]
                balance  = numeric_parts[-1]

                bill_no = parts[1]

                rows.append({
                    'date': sale_date,
                    'bill_no': bill_no,
                    'in': in_qty,
                    'out': out_qty,
                    'balance': balance,
                    'is_sale': is_sale,
                    'raw': line.strip(),
                })
    return rows


def verify_chain(rows):
    """
    Walk ALL rows (sales + purchases + opening stock) IN PDF ORDER and check
    that each row's balance correctly follows: previous_balance + IN - OUT ==
    current_balance. This accounts for restocks, not just sales.

    Duplicate-bill-number tracking is restricted to SALE rows only, since
    that's what the dedup logic in the view actually affects.

    Returns (all_passed, mismatches, duplicate_bill_report).
    """
    mismatches = []
    prev_balance = None

    from collections import defaultdict
    bill_occurrences = defaultdict(list)

    for i, row in enumerate(rows):
        if row['is_sale']:
            key = (row['date'], row['bill_no'])
            bill_occurrences[key].append(i)

        if prev_balance is not None:
            expected = round(prev_balance + row['in'] - row['out'], 3)
            actual   = round(row['balance'], 3)
            if abs(expected - actual) > 0.001:
                mismatches.append({
                    'row_index': i,
                    'expected_balance': expected,
                    'actual_balance': actual,
                    'raw': row['raw'],
                })
        prev_balance = row['balance']

    duplicate_bills = {k: v for k, v in bill_occurrences.items() if len(v) > 1}

    return len(mismatches) == 0, mismatches, duplicate_bills


def main():
    if len(sys.argv) < 2:
        print("Usage: python verify_balance_chain.py path/to/ledger.pdf")
        sys.exit(1)

    pdf_path = sys.argv[1]
    print(f"Parsing {pdf_path} ...")
    rows = parse_rows(pdf_path)
    print(f"Found {len(rows)} CASH SALE rows.\n")

    all_passed, mismatches, duplicate_bills = verify_chain(rows)

    print(f"{'='*60}")
    print(f"BALANCE CHAIN VERIFICATION: {'PASSED' if all_passed else 'FAILED'}")
    print(f"{'='*60}\n")

    if all_passed:
        print("Every row's balance correctly follows from the previous row's")
        print("balance minus that row's OUT quantity, in sequence.")
        print("This confirms every row -- including repeated-bill-number rows --")
        print("represents a real, distinct inventory movement.\n")
    else:
        print(f"{len(mismatches)} row(s) broke the balance chain:\n")
        for m in mismatches[:10]:
            print(f"  Row {m['row_index']}: expected balance {m['expected_balance']}, "
                  f"got {m['actual_balance']}")
            print(f"    Raw: {m['raw']}\n")

    print(f"{'='*60}")
    print(f"Bill numbers that repeat on the same date: {len(duplicate_bills)}")
    print(f"{'='*60}\n")

    for (date, bill_no), indices in list(duplicate_bills.items())[:10]:
        print(f"  {date} / bill {bill_no}: appears at row indices {indices}")
        for idx in indices:
            print(f"      {rows[idx]['raw']}")
        print()

    if all_passed and duplicate_bills:
        print("CONCLUSION: repeated bill numbers are real, separate transactions.")
        print("Recommend Option A -- remove bill-number dedup entirely, since the")
        print("Balance column itself proves every row is a distinct movement.")
    elif not all_passed:
        print("CONCLUSION: balance chain has gaps -- investigate mismatches above")
        print("before deciding on dedup logic; something else may be going on")
        print("(e.g. rows spanning multiple pages/products, OPENING STOCK resets).")


if __name__ == '__main__':
    main()