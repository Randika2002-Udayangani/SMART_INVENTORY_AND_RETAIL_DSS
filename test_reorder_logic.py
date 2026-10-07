"""
test_reorder_logic.py
=====================
Tests for the PURE functions in inventory/services/reorder_logic.py.
No Django, no database, no server needed — run anywhere.

USAGE:
    python test_reorder_logic.py

All tests print PASS or FAIL with the reason.
The mandatory test from the task plan is marked  ← REQUIRED.
"""

import sys
from decimal import Decimal

# ── Import the two pure functions directly ────────────────────────────────────
# Since these functions have no Django imports, we can test them in isolation.
# Copy-paste just the two functions here OR adjust the sys.path if running
# from inside your Django project.

def get_urgency(days_of_stock: float) -> str:
    """Paste from reorder_logic.py — pure function, no DB needed."""
    if days_of_stock <= 3:
        return 'CRITICAL'
    elif days_of_stock <= 7:
        return 'HIGH'
    elif days_of_stock <= 14:
        return 'MEDIUM'
    else:
        return 'LOW'


def calc_suggested_qty(avg_daily_sales, current_stock, lead_time_days):
    """Paste from reorder_logic.py — pure function, no DB needed."""
    TARGET_STOCK_DAYS = 30
    safety_stock  = int(avg_daily_sales * lead_time_days)
    raw_suggested = (avg_daily_sales * TARGET_STOCK_DAYS) + safety_stock - current_stock
    suggested_qty = max(0, int(raw_suggested))
    return {'safety_stock': safety_stock, 'suggested_quantity': suggested_qty}


# ── Test runner ───────────────────────────────────────────────────────────────
passed = 0
failed = 0

def check(label, got, expected, required=False):
    global passed, failed
    tag = '← REQUIRED' if required else ''
    if got == expected:
        print(f'  PASS  {label}  {tag}')
        passed += 1
    else:
        print(f'  FAIL  {label}  {tag}')
        print(f'        expected: {expected!r}')
        print(f'        got:      {got!r}')
        failed += 1


print()
print('=' * 58)
print('  reorder_logic.py — unit tests')
print('=' * 58)

# ─────────────────────────────────────────────────────────────────────────────
print()
print('get_urgency() — boundary tests')
# ─────────────────────────────────────────────────────────────────────────────

check('get_urgency(2) == CRITICAL',    get_urgency(2),    'CRITICAL', required=True)
check('get_urgency(0) == CRITICAL',    get_urgency(0),    'CRITICAL')
check('get_urgency(3) == CRITICAL',    get_urgency(3),    'CRITICAL')  # boundary
check('get_urgency(3.0) == CRITICAL',  get_urgency(3.0),  'CRITICAL')  # float boundary
check('get_urgency(4) == HIGH',        get_urgency(4),    'HIGH')      # first HIGH
check('get_urgency(5) == HIGH',        get_urgency(5),    'HIGH')
check('get_urgency(7) == HIGH',        get_urgency(7),    'HIGH')      # last HIGH
check('get_urgency(8) == MEDIUM',      get_urgency(8),    'MEDIUM')    # first MEDIUM
check('get_urgency(10) == MEDIUM',     get_urgency(10),   'MEDIUM')
check('get_urgency(14) == MEDIUM',     get_urgency(14),   'MEDIUM')    # last MEDIUM
check('get_urgency(15) == LOW',        get_urgency(15),   'LOW')       # first LOW
check('get_urgency(30) == LOW',        get_urgency(30),   'LOW')
check('get_urgency(100) == LOW',       get_urgency(100),  'LOW')

# ─────────────────────────────────────────────────────────────────────────────
print()
print('calc_suggested_qty() — formula verification')
# ─────────────────────────────────────────────────────────────────────────────

# Example 1: Normal reorder needed
# avg=10, lead=7, stock=50
# safety = 10×7 = 70
# raw    = (10×30)+70−50 = 320
# suggested = 320
r1 = calc_suggested_qty(Decimal('10'), 50, 7)
check('safety_stock  (avg=10,lead=7)',    r1['safety_stock'],       70)
check('suggested_qty (avg=10,stock=50,lead=7)', r1['suggested_quantity'], 320)

# Example 2: Overstocked — suggested must be 0
# avg=5, lead=7, stock=200
# safety = 5×7 = 35
# raw    = (5×30)+35−200 = −15  →  MAX(0,−15) = 0
r2 = calc_suggested_qty(Decimal('5'), 200, 7)
check('suggested_qty == 0 when overstocked',   r2['suggested_quantity'], 0)
check('safety_stock  (avg=5,lead=7)',           r2['safety_stock'],       35)

# Example 3: Zero stock — full reorder
# avg=8, lead=7, stock=0
# safety = 8×7 = 56
# raw    = (8×30)+56−0 = 296
r3 = calc_suggested_qty(Decimal('8'), 0, 7)
check('suggested_qty when stock=0',  r3['suggested_quantity'], 296)
check('safety_stock (avg=8,lead=7)', r3['safety_stock'],        56)

# Example 4: Longer lead time (lead=14)
# avg=5, lead=14, stock=30
# safety = 5×14 = 70
# raw    = (5×30)+70−30 = 190
r4 = calc_suggested_qty(Decimal('5'), 30, 14)
check('suggested_qty (lead=14)',     r4['suggested_quantity'], 190)
check('safety_stock  (lead=14)',     r4['safety_stock'],        70)

# Example 5: Float avg_daily (common from division)
# avg=3.33, lead=7, stock=10
# safety = int(3.33×7) = int(23.31) = 23
# raw    = (3.33×30)+23−10 ≈ 112.9  →  int = 112
r5 = calc_suggested_qty(Decimal('3.33'), 10, 7)
check('safety_stock  (avg=3.33, lead=7)', r5['safety_stock'],        23)
check('suggested_qty (avg=3.33)',         r5['suggested_quantity'],  112)

# ─────────────────────────────────────────────────────────────────────────────
print()
print('=' * 58)
print(f'  {passed} passed   {failed} failed')
print('=' * 58)

if failed == 0:
    print()
    print('  All tests passed.')
    print('  You are ready to wire check_reorder_needs() into the view.')
    print()
else:
    print()
    print('  Fix failing tests before running check_reorder_needs().')
    print()
    sys.exit(1)