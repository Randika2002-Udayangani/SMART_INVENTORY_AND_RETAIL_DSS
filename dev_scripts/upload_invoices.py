"""
Batch upload script for purchase invoice PDFs.
Uploads all invoices in a folder, logs successes/failures, saves results to JSON.

USAGE:
    1. Edit INVOICE_FOLDER and TOKEN below.
    2. Run: python upload_invoices.py
    3. If a token expires mid-run, re-login, update TOKEN, set START_INDEX
       to resume where it stopped.
"""

import requests
import os
import json

# ── EDIT THESE ────────────────────────────────────────────────────────────
BASE_URL        = "http://localhost:8000/api/purchases/upload/invoice/"
TOKEN           = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxNzg0NDc5MTg2LCJpYXQiOjE3ODQ0NTAzODYsImp0aSI6ImYwNWMwZWNmNmJiNTQ2MWM4YjU3YzI5NmI4MWNkMjEyIiwidXNlcl9pZCI6IjEifQ.frA0tkv3LjLoobS2Psx4NLW9X31CWJiwuyCF3gM-gpQ"
INVOICE_FOLDER  = r"C:\Users\HP\Desktop\3rd Year\2nd SEM\CSC311S3 - Machine Learning\suppliers"
START_INDEX     = 0
# ─────────────────────────────────────────────────────────────────────────

headers = {"Authorization": f"Bearer {TOKEN}"}

all_pdfs = sorted(
    f for f in os.listdir(INVOICE_FOLDER) if f.lower().endswith('.pdf')
)
print(f"Found {len(all_pdfs)} invoice PDFs total.")

batch = all_pdfs[START_INDEX:]
print(f"Uploading from index {START_INDEX} ({len(batch)} files)\n")

results  = []
failures = []
total_batches_created = 0
total_lines_skipped    = 0

for i, filename in enumerate(batch, start=START_INDEX):
    filepath = os.path.join(INVOICE_FOLDER, filename)
    try:
        with open(filepath, 'rb') as f:
            resp = requests.post(BASE_URL, headers=headers, files={'file': f})

        if resp.status_code == 201:
            data = resp.json()
            results.append({'index': i, 'file': filename, 'data': data})
            total_batches_created += data.get('batches_created', 0)
            total_lines_skipped   += data.get('lines_skipped', 0)
            print(f"[{i}] OK: {filename} -> supplier='{data.get('supplier')}', "
                  f"invoice={data.get('invoice_number')}, "
                  f"batches_created={data.get('batches_created')}, "
                  f"lines_skipped={data.get('lines_skipped')}, "
                  f"total=Rs.{data.get('total_amount')}")
            if data.get('skipped'):
                for s in data['skipped']:
                    print(f"      SKIPPED LINE: {s['item_code']} '{s['description']}' - {s['reason']}")

        else:
            try:
                err = resp.json()
            except Exception:
                err = resp.text
            failures.append({'index': i, 'file': filename, 'status_code': resp.status_code, 'error': err})
            print(f"[{i}] FAIL ({resp.status_code}): {filename} -> {err}")

            # Token expired mid-run -- stop here so you can refresh and resume
            if resp.status_code == 401 or 'token' in str(err).lower():
                print(f"\n*** Token issue detected. Stop, get a fresh token, "
                      f"set START_INDEX = {i}, and re-run. ***\n")
                break

    except Exception as e:
        failures.append({'index': i, 'file': filename, 'error': str(e)})
        print(f"[{i}] EXCEPTION: {filename} -> {e}")

print(f"\n{'='*60}")
print(f"SUMMARY: {len(results)} succeeded, {len(failures)} failed")
print(f"Total batches created: {total_batches_created}")
print(f"Total lines skipped (product name mismatches): {total_lines_skipped}")
print(f"{'='*60}\n")

if failures:
    print("Failures:")
    for f in failures:
        print(f"  [{f['index']}] {f['file']}: {f.get('error')}")

with open('invoice_upload_results.json', 'w') as f:
    json.dump({'succeeded': results, 'failed': failures}, f, indent=2, default=str)

print(f"\nFull results saved to invoice_upload_results.json")