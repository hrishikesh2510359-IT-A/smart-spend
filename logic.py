from datetime import datetime, timedelta
import calendar
from models import db, User, Category, Transaction


# ---------------------------------------------------------------------------
# Helper — compute period_end from a period_start date
# Rule: last day of the month AFTER period_start's month
# e.g.  period_start = 2026-03-25  →  period_end = 2026-04-30
#       period_start = 2026-12-01  →  period_end = 2027-01-31
# ---------------------------------------------------------------------------
def compute_period_end(period_start: datetime) -> datetime:
    # Move to the month after period_start
    year  = period_start.year
    month = period_start.month + 1
    if month > 12:
        month = 1
        year += 1
    last_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, last_day, 23, 59, 59)


# ---------------------------------------------------------------------------
# Get the active income transaction for a user
# "Active" = the most recent income whose period_end is in the future
# If none found, fall back to the most recent income transaction overall
# ---------------------------------------------------------------------------
def get_active_income_tx(user_id: int):
    now = datetime.utcnow()
    # Try to find a period that hasn't ended yet
    active = (
        Transaction.query
        .filter_by(user_id=user_id, type='income')
        .filter(Transaction.period_end >= now)
        .order_by(Transaction.date.desc())
        .first()
    )
    if active:
        return active
    # Fallback: most recent income transaction
    return (
        Transaction.query
        .filter_by(user_id=user_id, type='income')
        .order_by(Transaction.date.desc())
        .first()
    )


# ---------------------------------------------------------------------------
# Get the date range for the current active income period
# Returns (period_start, period_end) datetimes
# Falls back to current calendar month if no income transaction exists
# ---------------------------------------------------------------------------
def get_active_period_range(user_id: int):
    tx = get_active_income_tx(user_id)
    if tx and tx.period_start and tx.period_end:
        return tx.period_start, tx.period_end
    # Fallback: current calendar month
    now = datetime.utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day = calendar.monthrange(now.year, now.month)[1]
    end = now.replace(day=last_day, hour=23, minute=59, second=59, microsecond=0)
    return start, end


# ---------------------------------------------------------------------------
# Calculate remaining balance within the active period
# remaining = income_for_period + extra_income_txns - total_expenses_in_period
# ---------------------------------------------------------------------------
def calculate_remaining_balance(user_id: int) -> float:
    period_start, period_end = get_active_period_range(user_id)

    # All income transactions in this period
    income_txns = (
        Transaction.query
        .filter_by(user_id=user_id, type='income')
        .filter(Transaction.date >= period_start, Transaction.date <= period_end)
        .all()
    )
    total_income = sum(t.amount for t in income_txns)

    # All expense transactions in this period
    expense_txns = (
        Transaction.query
        .filter_by(user_id=user_id, type='expense')
        .filter(Transaction.date >= period_start, Transaction.date <= period_end)
        .all()
    )
    total_expenses = sum(t.amount for t in expense_txns)

    # Fixed reservations — categories marked is_fixed=True have their full
    # budget reserved. Only the UNSPENT portion is deducted to avoid
    # double-counting when the user later logs the actual expense.
    categories = Category.query.filter_by(user_id=user_id).all()
    fixed_categories = {c.name: c.budget for c in categories if c.is_fixed}

    already_spent_on_fixed = sum(
        txn.amount for txn in expense_txns if txn.category in fixed_categories
    )
    total_fixed_reserved = sum(fixed_categories.values())
    unspent_fixed = max(0.0, total_fixed_reserved - already_spent_on_fixed)

    remaining = total_income - total_expenses - unspent_fixed
    return round(remaining, 2)


# ---------------------------------------------------------------------------
# Calculate daily allowance
# = remaining_balance / days left from TODAY to period_end (inclusive)
# ---------------------------------------------------------------------------
def calculate_daily_allowance(user_id: int) -> float:
    _, period_end = get_active_period_range(user_id)
    remaining = calculate_remaining_balance(user_id)

    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = period_end.replace(hour=0, minute=0, second=0, microsecond=0)

    days_left = (end_date - today).days + 1  # inclusive of today
    days_left = max(days_left, 1)            # avoid division by zero

    return round(remaining / days_left, 2)


# ---------------------------------------------------------------------------
# Days remaining in the active period (from today inclusive)
# ---------------------------------------------------------------------------
def get_days_remaining(user_id: int) -> int:
    _, period_end = get_active_period_range(user_id)
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = period_end.replace(hour=0, minute=0, second=0, microsecond=0)
    return max((end_date - today).days + 1, 0)


# ---------------------------------------------------------------------------
# Budget vs Spent per category (for the active period)
# ---------------------------------------------------------------------------
def get_budget_vs_spent(user_id: int) -> list:
    period_start, period_end = get_active_period_range(user_id)
    categories = Category.query.filter_by(user_id=user_id).all()

    result = []
    for cat in categories:
        spent = (
            db.session.query(db.func.sum(Transaction.amount))
            .filter_by(user_id=user_id, type='expense', category=cat.name)
            .filter(Transaction.date >= period_start, Transaction.date <= period_end)
            .scalar() or 0.0
        )
        result.append({
            'id':        cat.id,
            'category':  cat.name,
            'budget':    cat.budget,
            'is_fixed':  cat.is_fixed,
            'spent':     round(spent, 2),
            'remaining': round(cat.budget - spent, 2),
        })
    return result


# ---------------------------------------------------------------------------
# Leftover from the PREVIOUS period (shown as a display note)
# Returns None if there is no closed-out previous period
# ---------------------------------------------------------------------------
def get_previous_period_leftover(user_id: int):
    now = datetime.utcnow()
    # Find the most recently CLOSED income period
    closed_tx = (
        Transaction.query
        .filter_by(user_id=user_id, type='income')
        .filter(Transaction.period_end < now)
        .order_by(Transaction.period_end.desc())
        .first()
    )
    if not closed_tx or not closed_tx.period_start or not closed_tx.period_end:
        return None

    period_start = closed_tx.period_start
    period_end   = closed_tx.period_end

    income_txns = (
        Transaction.query
        .filter_by(user_id=user_id, type='income')
        .filter(Transaction.date >= period_start, Transaction.date <= period_end)
        .all()
    )
    total_income = sum(t.amount for t in income_txns)

    expense_txns = (
        Transaction.query
        .filter_by(user_id=user_id, type='expense')
        .filter(Transaction.date >= period_start, Transaction.date <= period_end)
        .all()
    )
    total_expenses = sum(t.amount for t in expense_txns)

    leftover = round(total_income - total_expenses, 2)
    return {
        'amount':       leftover,
        'period_start': period_start.strftime('%d %b %Y'),
        'period_end':   period_end.strftime('%d %b %Y'),
    }


# ---------------------------------------------------------------------------
# Curveball
# ---------------------------------------------------------------------------
CURVEBALLS = [
    {"event": "Phone screen cracked!",       "amount": 2000},
    {"event": "Sudden subscription renewal!", "amount": 500},
    {"event": "Medical emergency!",           "amount": 3000},
    {"event": "Car tyre puncture!",           "amount": 800},
    {"event": "Water bill spike!",            "amount": 600},
]

def trigger_curveball(user_id: int) -> dict:
    import random
    event = random.choice(CURVEBALLS)

    transaction = Transaction(
        user_id=user_id,
        type='expense',
        category='Curveball',
        description=event['event'],
        amount=event['amount'],
        date=datetime.utcnow(),
    )
    db.session.add(transaction)
    db.session.commit()

    old_allowance = calculate_daily_allowance(user_id)
    new_allowance = calculate_daily_allowance(user_id)
    pct_reduction = round(
        ((old_allowance - new_allowance) / old_allowance * 100) if old_allowance else 0,
        1
    )

    return {
        'event_name':          event['event'],
        'amount_deducted':     event['amount'],
        'new_daily_allowance': new_allowance,
        'percentage_reduction': pct_reduction,
    }
