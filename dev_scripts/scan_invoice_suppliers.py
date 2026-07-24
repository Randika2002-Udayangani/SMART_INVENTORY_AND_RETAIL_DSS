"""
Scans a folder of purchase invoice PDFs and extracts the distinct supplier
names found in each one's header (the "Bill No : ... Supplier : ..." line).

Use this BEFORE running the invoice upload batch, so you can create all
missing Supplier records up front instead of hitting "Supplier not found"
errors one invoice at a time.

USAGE:
    python scan_invoice_suppliers.py
    (edit INVOICE_FOLDER below first)
"""

import os
import re
import pdfplumber

# ── EDIT THIS ────────────────────────────────────────────────────────────
INVOICE_FOLDER = r"C:\Users\HP\Desktop\3rd Year\2nd SEM\CSC311S3 - Machine Learning\suppliers"
# ─────────────────────────────────────────────────────────────────────────

_BILL_SUPPLIER_PATTERN = re.compile(r'Bill No\s*:\s*(\S+)\s+Supplier\s*:\s*(.+)')


def extract_supplier(pdf_path):
    """Return the supplier name found in a single invoice PDF, or None."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if not text:
                    continue
                for line in text.split('\n'):
                    m = _BILL_SUPPLIER_PATTERN.search(line.strip())
                    if m:
                        return m.group(2).strip()
    except Exception as e:
        return f"__ERROR__: {e}"
    return None


def main():
    all_pdfs = sorted(
        f for f in os.listdir(INVOICE_FOLDER) if f.lower().endswith('.pdf')
    )
    print(f"Found {len(all_pdfs)} invoice PDFs.\n")

    supplier_counts = {}
    unreadable = []

    for filename in all_pdfs:
        filepath = os.path.join(INVOICE_FOLDER, filename)
        supplier = extract_supplier(filepath)

        if supplier is None:
            unreadable.append(filename)
        elif supplier.startswith("__ERROR__"):
            unreadable.append(f"{filename} ({supplier})")
        else:
            supplier_counts[supplier] = supplier_counts.get(supplier, 0) + 1

    print(f"{'='*60}")
    print(f"DISTINCT SUPPLIERS FOUND: {len(supplier_counts)}")
    print(f"{'='*60}\n")

    for supplier, count in sorted(supplier_counts.items()):
        print(f"  {supplier:40s}  ({count} invoice{'s' if count != 1 else ''})")

    if unreadable:
        print(f"\n{'='*60}")
        print(f"COULD NOT EXTRACT SUPPLIER FROM {len(unreadable)} FILE(S):")
        print(f"{'='*60}")
        for f in unreadable:
            print(f"  {f}")

    print(f"\nNext step: create any suppliers above that don't already exist")
    print(f"via POST /api/suppliers/ or Django shell, before running the upload batch.")


if __name__ == '__main__':
    main()