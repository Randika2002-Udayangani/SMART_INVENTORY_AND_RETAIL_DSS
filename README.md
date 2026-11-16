Smart Inventory and Retail DSS

A Django web application for retail inventory operations and decision support. It combines a customer storefront with a staff dashboard for products, stock, purchasing, sales uploads, reporting, and operational alerts.

Live system: https://smart-inventory-dss.onrender.com

Features
Staff dashboard
Product and supplier management — maintain the product catalogue, suppliers, and store zones.
Purchases and batches — record supplier deliveries, invoice details, received quantities, costs, and expiry dates.
Inventory — review stock levels, low/out-of-stock items, stock history, and manual adjustments.
Sales uploads — upload daily bill PDFs and item ledger PDFs, and review any records that need attention. Upload status and source filenames appear in the upload summaries.
Decision support — view reorder recommendations, product lifecycle stages, inventory health scores, discounts, losses, and zone recommendations.
Reports and analytics — review operational and sales data and export supported reports.
User and access management — manage staff accounts and roles, including Admin, Manager, and Staff access.
Audit log and system configuration — review recorded account and data changes and configure supported application settings.
Notifications — view operational alerts in the dashboard.
Customer storefront
Browse products and discounts, use the shopping cart, and place pickup orders.
Register, sign in, manage a profile and orders, and use customer password-recovery pages.
Ask the in-app assistant questions about supported store information.

Staff and customer authentication are separate. Staff use the private dashboard login route; customers use the storefront login.

Technology
Python 3.11 and Django 4.2
Django REST Framework and Simple JWT
PostgreSQL
HTML, CSS, JavaScript (Bootstrap, Chart.js), and Django templates
Pandas, OpenPyXL, PDFMiner/pdfplumber, and ReportLab for data processing and document workflows
Optional Google Gemini integration for the customer assistant
Gunicorn and WhiteNoise for production serving

Pinned Python dependencies are listed in requirements.txt.

Project layout
text
.
├── analytics/       # Analytics and decision-support endpoints
├── core/            # Shared API endpoints and utilities
├── customer/        # Customer storefront and customer pages
├── dashboard/       # Staff dashboard pages
├── inventory/       # Stock, lifecycle, health, reorder, and notifications
├── orders/          # Customer authentication, orders, ratings, and assistant APIs
├── products/        # Products, categories, brands, and zones
├── purchases/       # Purchases, batches, and supplier deliveries
├── sales/           # Sales records, PDF uploads, and report exports
├── suppliers/       # Supplier management
├── users/           # Staff authentication, roles, audit log, and configuration
├── smart_inventory/ # Django settings and URL configuration
├── templates/       # Staff dashboard templates
├── manage.py
└── requirements.txt
Local setup
Requirements
Python 3.11 (the pinned pandas 2.0.3 does not support Python 3.12 or later)
PostgreSQL 15
Git
1. Get the code and create a virtual environment
powershell
git clone https://github.com/Randika2002-Udayangani/SMART_INVENTORY_AND_RETAIL_DSS.git
cd SMART_INVENTORY_AND_RETAIL_DSS
git checkout main
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1

On macOS or Linux, activate the environment with:

bash
source venv/bin/activate
2. Install dependencies
bash
python -m pip install --upgrade pip
pip install -r requirements.txt
3. Configure environment variables

Create the PostgreSQL database and user first, for example in psql:

sql
CREATE DATABASE smart_inventory;
CREATE USER smart_user WITH PASSWORD 'choose-a-password';
GRANT ALL PRIVILEGES ON DATABASE smart_inventory TO smart_user;
ALTER USER smart_user CREATEDB;

Copy the safe example file to a local .env file:

powershell
Copy-Item .env.example .env

The Django settings read environment variables from the repository-root .env and from smart_inventory/.env. Variables already set on the machine take priority over both files. Add the database and Django settings below to the local .env. Do not commit .env files or real credentials.

dotenv
SECRET_KEY=replace-with-a-long-random-secret
DEBUG=True
STAFF_LOGIN_PATH=choose-a-private-path-segment
DATABASE_NAME=smart_inventory
DATABASE_USER=smart_user
DATABASE_PASSWORD=your_postgresql_password
DATABASE_HOST=localhost
DATABASE_PORT=5432

With DEBUG=True and no STAFF_LOGIN_PATH, the staff login falls back to dev-staff-entry. When DEBUG is off, STAFF_LOGIN_PATH is required and the app will not start without it.

Optional settings:

GEMINI_API_KEY — enables the assistant's Gemini integration. Keep the key server-side.
GEMINI_MODEL — model override; the application has a default.
CHATBOT_RATE_LIMIT and CHATBOT_RATE_WINDOW_SECONDS — per-user assistant request limits (default 20 requests per 300 seconds).
ALLOWED_HOSTS — comma-separated hostnames for non-local environments.
CORS_ALLOWED_ORIGINS — comma-separated browser origins allowed to call the API.
SITE_URL — public base URL used when constructing customer-facing links.
AUDIT_PROXY_COUNT — number of trusted proxies in front of Django, so the audit log records the real client address.
EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, and DEFAULT_FROM_EMAIL — configure SMTP email (Gmail, with an app password). In development, Django uses the console email backend by default.

For deployment, set DEBUG=False, use a strong private SECRET_KEY, set STAFF_LOGIN_PATH, configure ALLOWED_HOSTS, and supply production database and email settings through the hosting platform's secret/environment configuration.

4. Apply migrations, create an initial admin, and load default data
powershell
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_discount_rules
python manage.py seed_store_zones

The initial superuser has full Admin rights in the staff dashboard. Use the application's User Management page to create and assign staff roles. The two seed_ commands load the default discount tiers and special store zones; without the discount tiers the Discount Engine page stays empty.

5. Start the development server
powershell
python manage.py runserver
Customer storefront: http://127.0.0.1:8000/
Staff login: http://127.0.0.1:8000/<STAFF_LOGIN_PATH>/
Staff dashboard (after login): http://127.0.0.1:8000/dashboard/

Replace <STAFF_LOGIN_PATH> with the value configured in the local environment.

6. Load business data

Through the staff dashboard, load data in this order: the item master Excel file, then purchases, then the item ledger and daily bill PDFs. Sales rows are matched to products by SKU code, so the products must exist first.

API overview

Application APIs are mounted under /api/. Main endpoint groups include:

/api/auth/ and /api/users/ — staff login-related actions, profile, password changes, user management, and audit log.
/api/products/, /api/suppliers/, and /api/purchases/ — catalogue and purchasing operations.
/api/inventory/, /api/lifecycle/, /api/health-scores/, /api/reorder/, and /api/discounts/ — stock and decision-support operations.
/api/sales/ and /api/reports/ — sales uploads, upload logs, and report exports.
/api/customer-auth/, /api/orders/, /api/ratings/, and /api/chatbot/ — customer account, order, feedback, and assistant APIs.
/api/status/ — health check used by the hosting platform.

Most staff APIs require a JWT access token. Customer endpoints use the customer authentication flow. Consult each app's urls.py, views, and serializers for endpoint-specific request formats and permissions.

Roles
Admin — staff account administration, configuration, and Admin-authorized operations.
Manager — operational management and decision-support actions granted to Managers and Admins.
Staff — day-to-day inventory, purchase, sales upload, and other permitted operations.

Exact permissions are enforced by the API as well as by dashboard controls; hiding a control in the browser is not the security boundary.

Tests and maintenance

Run the full Django test suite (the database user needs the CREATEDB permission, because Django creates a temporary test database):

powershell
python manage.py test

Run tests for a specific app or module:

powershell
python manage.py test users
python manage.py test products.test_reorder_threshold

Useful Django commands:

powershell
python manage.py check
python manage.py makemigrations
python manage.py migrate
python manage.py collectstatic
Deployment

The live system runs on Render (web service) with a Neon PostgreSQL database, both in the Singapore region. Render builds and deploys the main branch.

Build command: pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate --noinput
Start command: gunicorn smart_inventory.wsgi
Health check path: /api/status/
Python version: 3.11
Settings and secrets are Render environment variables, not code.

Updates follow one path: feature branch, pull request into dev1, tests, then a pull request into main. Render redeploys automatically.

Security
Never commit .env, database credentials, email passwords, API keys, or production secrets.
Do not use development settings or example credentials in production.
Keep DEBUG=False in production and configure ALLOWED_HOSTS, HTTPS, database, and email settings for the deployment.
Use a unique, private STAFF_LOGIN_PATH; it supplements authentication and does not replace it.
Team

This repository is the Team Software Project for STACK5. Project contributors are listed in the project submission materials.