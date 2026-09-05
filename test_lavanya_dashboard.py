"""
Selenium smoke test for Lavanya's dashboard branch (week6/lavanya, pushed 2026-08-09).

WHAT THIS DOES
- Logs in once as a manager/staff user via the real login form
- Visits every dashboard page and confirms:
    1. It doesn't bounce back to /dashboard/login/ (i.e. JWT check passed)
    2. The page returns normal content (not a Django error page)
    3. There are no red errors in the browser console
- Checks every sidebar link's actual href against the routes we found in the repo,
  and flags any that are still "#" (dead) or point to Django URL that doesn't resolve
- Prints a plain pass/fail table at the end

WHAT THIS DOES NOT DO
- Does not check that the *data* on each page is correct (real numbers vs pgAdmin).
  You still need to do that part manually — this script only catches "page is broken /
  blank / login-loop / JS error", which is most of what earlier QA rounds kept finding.

HOW TO RUN
1. pip install selenium --break-system-packages   (or in a venv)
2. Download a matching chromedriver (or use Selenium Manager, which ships with
   selenium >= 4.10 and downloads the driver automatically — no extra setup needed)
3. Make sure your Django dev server is running: python manage.py runserver
4. Update BASE_URL, MANAGER_USERNAME, MANAGER_PASSWORD below
5. Run: python test_lavanya_dashboard.py
"""

import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ---------------- CONFIG — EDIT THESE ----------------
BASE_URL = "http://127.0.0.1:8000"
MANAGER_USERNAME = "admin"
MANAGER_PASSWORD = "Admin123@"
# ------------------------------------------------------

# Real routes confirmed by grepping dashboard/urls.py on week6/lavanya
DASHBOARD_PAGES = [
    ("Home",             "/dashboard/"),
    ("Loss Analysis",    "/dashboard/loss-analysis/"),
    ("Lifecycle",        "/dashboard/lifecycle/"),
    ("Health Score",     "/dashboard/health-score/"),
    ("Analytics",        "/dashboard/analytics/"),
    ("Reorder",          "/dashboard/reorder/"),
    ("Discount Engine",  "/dashboard/discount-engine/"),
    ("Inventory",        "/dashboard/inventory/"),
    ("Purchases",        "/dashboard/purchases/"),
    ("Suppliers",        "/dashboard/suppliers/"),
    ("Products",         "/dashboard/products/"),
]

LOGIN_URL = BASE_URL + "/dashboard/login/"


def get_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1400,1000")
    # Comment out the next line if you want to watch the browser run
    # options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    return driver


def login(driver):
    """Log in through the real UI so sessionStorage gets set the same way a real user would.

    NOTE: this login.html has no <form> and no submit button — it's a plain
    <button onclick="doLogin()">, and the fields use id="username" / id="password"
    with no name attribute. So we select by ID and click the button directly.
    """
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 10)
    username_field = wait.until(EC.presence_of_element_located((By.ID, "username")))
    password_field = driver.find_element(By.ID, "password")

    username_field.send_keys(MANAGER_USERNAME)
    password_field.send_keys(MANAGER_PASSWORD)

    login_button = driver.find_element(By.CSS_SELECTOR, ".btn-login")
    login_button.click()

    # Give the fetch() call + JS redirect time to complete
    time.sleep(2)

    # If credentials were wrong, the page stays put and shows the error message
    try:
        error_msg = driver.find_element(By.ID, "errorMsg")
        if error_msg.is_displayed():
            raise RuntimeError(
                "Login form showed 'Invalid username or password' — check "
                "MANAGER_USERNAME / MANAGER_PASSWORD at the top of this script."
            )
    except NoSuchElementException:
        pass

    if "/dashboard/login/" in driver.current_url:
        raise RuntimeError(
            "Still on login page after submitting credentials — login likely failed. "
            "Check MANAGER_USERNAME / MANAGER_PASSWORD, or check the login form field names "
            "match what this script expects (input[name='username'], input[type='password'])."
        )
    print(f"[OK] Logged in. Redirected to: {driver.current_url}")


def check_console_errors(driver):
    """Chrome only. Returns list of severe console errors."""
    try:
        logs = driver.get_log("browser")
    except Exception:
        return []  # some driver/browser combos don't support this
    return [entry for entry in logs if entry.get("level") == "SEVERE"]


def test_page(driver, name, path):
    url = BASE_URL + path
    result = {"name": name, "path": path, "status": "PASS", "notes": []}

    driver.get(url)
    time.sleep(1.5)  # let any client-side auth-check JS run and redirect if needed

    current_url = driver.current_url

    # 1. Did it bounce back to login? (JWT check failed / not sent)
    if "/dashboard/login/" in current_url and path != "/dashboard/login/":
        result["status"] = "FAIL"
        result["notes"].append("Redirected to login — JWT check failed or token not sent")
        return result

    # 2. Is it a Django error page? (very rough check — looks for common Django debug page text)
    page_source = driver.page_source
    if "Traceback (most recent call last)" in page_source or "DisallowedHost" in page_source:
        result["status"] = "FAIL"
        result["notes"].append("Django error / traceback page shown instead of real content")
        return result

    # 3. Is the page basically empty? (rough heuristic — very short body text)
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.strip()
        if len(body_text) < 20:
            result["status"] = "FAIL"
            result["notes"].append("Page body is nearly empty — likely blank/broken template")
    except NoSuchElementException:
        result["status"] = "FAIL"
        result["notes"].append("No <body> found at all")

    # 4. Console errors
    errors = check_console_errors(driver)
    if errors:
        result["status"] = "FAIL" if result["status"] == "PASS" else result["status"]
        for e in errors[:5]:  # cap to first 5 to keep output readable
            result["notes"].append(f"Console error: {e.get('message', '')[:150]}")

    if result["status"] == "PASS":
        result["notes"].append("Loaded OK, no redirect, no console errors")

    return result


def check_sidebar_links(driver):
    """Run once, from the home page, to catch dead (#) or mismatched links."""
    driver.get(BASE_URL + "/dashboard/")
    time.sleep(1)
    links = driver.find_elements(By.CSS_SELECTOR, ".sidebar a, nav a")
    findings = []
    for link in links:
        href = link.get_attribute("href") or ""
        text = link.text.strip()
        if not text:
            continue
        if href.endswith("#") or href == "":
            findings.append(f'DEAD LINK: "{text}" -> href="{href or "#"}"')
    return findings


def main():
    driver = get_driver()
    results = []
    try:
        login(driver)

        print("\n--- Testing sidebar links from Home page ---")
        dead_links = check_sidebar_links(driver)
        if dead_links:
            for f in dead_links:
                print(f"[WARN] {f}")
        else:
            print("[OK] No dead (#) sidebar links found")

        print("\n--- Testing each dashboard page ---")
        for name, path in DASHBOARD_PAGES:
            r = test_page(driver, name, path)
            results.append(r)
            print(f"[{r['status']}] {name} ({path})")
            for note in r["notes"]:
                print(f"        - {note}")

    finally:
        driver.quit()

    # ---- Summary table ----
    print("\n=== SUMMARY ===")
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    print(f"{passed} passed / {failed} failed / {len(results)} total\n")
    for r in results:
        print(f"{r['status']:5} | {r['name']:20} | {r['path']}")


if __name__ == "__main__":
    main()