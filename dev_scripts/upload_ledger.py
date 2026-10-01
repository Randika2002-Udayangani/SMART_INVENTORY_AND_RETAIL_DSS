"""
Staged batch upload script for item ledger PDFs.
Uploads a batch, logs successes/failures, saves failure list to JSON.

USAGE:
    Option A (quick, manual token):
        1. Get a fresh access token.
        2. Set it as an environment variable before running:
               PowerShell: $env:LEDGER_UPLOAD_TOKEN = "<paste token>"
        3. Edit LEDGER_FOLDER, BATCH_SIZE, START_INDEX below.
        4. Run: python upload_ledgers.py

    Option B (recommended, auto-login):
        Set LEDGER_UPLOAD_USERNAME and LEDGER_UPLOAD_PASSWORD env vars instead,
        and the script will log in and fetch its own token automatically.
"""

import requests
import os
import json

# ── EDIT THESE ────────────────────────────────────────────────────────────
BASE_URL      = "http://localhost:8000/api/sales/upload/item-ledger/"
LOGIN_URL     = "http://localhost:8000/api/auth/login/"
LEDGER_FOLDER = r"C:\Users\HP\Desktop\3rd Year\2nd SEM\CSC311S3 - Machine Learning\Item_Ledgers"  # folder containing all 400 PDFs
BATCH_SIZE    = 20                          # how many to upload this run
START_INDEX   = 480                         # change this to resume later batches
# ─────────────────────────────────────────────────────────────────────────


def get_token():
    """Get an access token from env var, or log in fresh if credentials are set."""
    token = os.environ.get("LEDGER_UPLOAD_TOKEN")
    if token:
        return token

    username = os.environ.get("LEDGER_UPLOAD_USERNAME")
    password = os.environ.get("LEDGER_UPLOAD_PASSWORD")
    if username and password:
        resp = requests.post(LOGIN_URL, json={"username": username, "password": password})
        resp.raise_for_status()
        return resp.json()["access"]

    raise RuntimeError(
        "No token found. Either set LEDGER_UPLOAD_TOKEN directly, or set "
        "LEDGER_UPLOAD_USERNAME and LEDGER_UPLOAD_PASSWORD so the script can log in itself.\n"
        'PowerShell example: $env:LEDGER_UPLOAD_TOKEN = "<paste fresh token>"'
    )


TOKEN = get_token()
headers = {"Authorization": f"Bearer {TOKEN}"}

all_pdfs = sorted(
    f for f in os.listdir(LEDGER_FOLDER) if f.lower().endswith('.pdf')
)
print(f"Found {len(all_pdfs)} PDF files total in folder.")

batch = all_pdfs[START_INDEX:START_INDEX + BATCH_SIZE]
print(f"Uploading batch: files {START_INDEX} to {START_INDEX + len(batch) - 1} ({len(batch)} files)\n")

results  = []
failures = []

for i, filename in enumerate(batch, start=START_INDEX):
    filepath = os.path.join(LEDGER_FOLDER, filename)
    try:
        with open(filepath, 'rb') as f:
            resp = requests.post(BASE_URL, headers=headers, files={'file': f})

        if resp.status_code == 201:
            data = resp.json()
            results.append({'index': i, 'file': filename, 'data': data})
            print(f"[{i}] OK: {filename} -> product='{data.get('product')}', "
                  f"dates_inserted={data.get('dates_inserted')}, "
                  f"dates_skipped={data.get('dates_skipped')}")
        else:
            try:
                err = resp.json()
            except Exception:
                err = resp.text
            failures.append({'index': i, 'file': filename, 'status_code': resp.status_code, 'error': err})
            print(f"[{i}] FAIL ({resp.status_code}): {filename} -> {err}")

    except Exception as e:
        failures.append({'index': i, 'file': filename, 'error': str(e)})
        print(f"[{i}] EXCEPTION: {filename} -> {e}")

print(f"\n{'='*60}")
print(f"SUMMARY: {len(results)} succeeded, {len(failures)} failed (out of {len(batch)} attempted)")
print(f"{'='*60}\n")

if failures:
    print("Failures:")
    for f in failures:
        print(f"  [{f['index']}] {f['file']}: {f.get('error')}")

with open('upload_results.json', 'w') as f:
    json.dump({'succeeded': results, 'failed': failures}, f, indent=2, default=str)

print(f"\nFull results saved to upload_results.json")
print(f"Next run: set START_INDEX = {START_INDEX + len(batch)} to continue with the next batch.")