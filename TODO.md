# Randika — Dashboard Widget and Admin-Page Work

This branch contains Randika's dashboard widget and administration-page work.
The checklist below is retained as historical context for the frontend tasks
that were coordinated with Lavanya; it is not a continuation of Lavanya's
personal task list.

## ✅ Completed
- [x] 5. `dashboard/views.py` — Added view functions for all new pages
- [x] 6. `dashboard/urls.py` — Added URL patterns for all new pages

## Tier 1 — Fix Existing Pages
- [ ] 1. `home.html` — Fix sidebar links + KPI card hardcoded values + health score filter default
- [ ] 2. `loss_analysis.html` — Fix raw IDs in table, fix polling bug (autoDetect re-triggers on redirect)
- [ ] 3. `sales_report.html` — Verify already correct (sessionStorage, product_name object)
- [ ] 4. `lifecycle.html` — Verify already correct (re-fetch after POST)

## Tier 2 — New Pages (Templates)
- [ ] 8. `templates/dashboard/health_score.html` — Build full health score page
- [ ] 9. `templates/dashboard/reorder.html` — Build reorder suggestions page
- [ ] 10. `templates/dashboard/inventory.html` — Build stock management page
- [ ] 11. `templates/dashboard/purchases.html` — Build purchases & batches page
- [ ] 12. `templates/dashboard/suppliers.html` — Build suppliers CRUD page
- [ ] 13. `templates/dashboard/products.html` — Build product management page

## Final
- [ ] 14. Push branch and verify everything works

