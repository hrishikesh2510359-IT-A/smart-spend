from flask import Blueprint, request, jsonify, render_template, redirect, url_for
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

from models import db, User, Category, Transaction
from logic import (
    calculate_remaining_balance,
    calculate_daily_allowance,
    get_budget_vs_spent,
    get_days_remaining,
    get_active_period_range,
    get_previous_period_leftover,
    compute_period_end,
    trigger_curveball,
)

routes = Blueprint('routes', __name__)


# ---------------------------------------------------------------------------
# Page Routes
# ---------------------------------------------------------------------------

@routes.route('/')
def index():
    return render_template('login.html')


@routes.route('/onboarding')
def onboarding_page():
    return render_template('onboarding.html')


@routes.route('/dashboard')
def dashboard_page():
    return render_template('dashboard.html')


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@routes.route('/register', methods=['POST'])
def register():
    data     = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Username already exists'}), 409

    user = User(
        username=username,
        password=generate_password_hash(password),
    )
    db.session.add(user)
    db.session.commit()
    login_user(user)
    return jsonify({'user_id': user.id, 'username': user.username}), 201


@routes.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return jsonify({'message': 'Send POST with username + password'}), 200

    data     = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    user = User.query.filter_by(username=username).first()
    if not user or not check_password_hash(user.password, password):
        return jsonify({'error': 'Invalid credentials'}), 401

    login_user(user)
    return jsonify({'user_id': user.id, 'username': user.username}), 200


@routes.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('routes.index'))


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------

@routes.route('/setup-income', methods=['POST'])
def setup_income():
    data           = request.get_json()
    user_id        = data.get('user_id')
    monthly_income = data.get('monthly_income')

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    user.monthly_income = monthly_income
    db.session.commit()
    return jsonify({'message': 'Income set', 'monthly_income': monthly_income}), 200


@routes.route('/add-category', methods=['POST'])
def add_category():
    data     = request.get_json()
    user_id  = data.get('user_id')
    name     = data.get('name', '').strip()
    budget   = data.get('budget', 0.0)
    is_fixed = data.get('is_fixed', False)

    if not name:
        return jsonify({'error': 'Category name required'}), 400

    category = Category(
        user_id=user_id,
        name=name,
        budget=budget,
        is_fixed=is_fixed,
    )
    db.session.add(category)
    db.session.commit()
    return jsonify({
        'message':     'Category added',
        'category_id': category.id,
        'name':        category.name,
        'budget':      category.budget,
        'is_fixed':    category.is_fixed,
    }), 201


@routes.route('/get-categories', methods=['GET'])
def get_categories():
    user_id    = request.args.get('user_id', type=int)
    categories = Category.query.filter_by(user_id=user_id).all()
    return jsonify([{
        'id':       c.id,
        'name':     c.name,
        'budget':   c.budget,
        'is_fixed': c.is_fixed,
    } for c in categories]), 200


# ---------------------------------------------------------------------------
# Edit Category
# Renames the category and updates budget / fixed_amount.
# All transactions under the old name are renamed automatically.
# ---------------------------------------------------------------------------

@routes.route('/edit-category/<int:category_id>', methods=['PUT'])
def edit_category(category_id):
    data       = request.get_json()
    new_name   = data.get('name', '').strip()
    new_budget = data.get('budget')
    is_fixed   = data.get('is_fixed')

    category = Category.query.get(category_id)
    if not category:
        return jsonify({'error': 'Category not found'}), 404

    old_name = category.name

    if new_name and new_name != old_name:
        Transaction.query.filter_by(
            user_id=category.user_id,
            category=old_name,
        ).update({'category': new_name})
        category.name = new_name

    if new_budget is not None:
        category.budget = new_budget

    if is_fixed is not None:
        category.is_fixed = is_fixed

    db.session.commit()
    return jsonify({
        'message':  'Category updated',
        'id':       category.id,
        'name':     category.name,
        'budget':   category.budget,
        'is_fixed': category.is_fixed,
    }), 200


@routes.route('/delete-category/<int:category_id>', methods=['DELETE'])
def delete_category(category_id):
    category = Category.query.get(category_id)
    if not category:
        return jsonify({'error': 'Category not found'}), 404

    # Transactions are kept; their category field will just be an orphan name
    db.session.delete(category)
    db.session.commit()
    return jsonify({'message': 'Category deleted'}), 200


# ---------------------------------------------------------------------------
# Transactions — Add
# ---------------------------------------------------------------------------

@routes.route('/add-expense', methods=['POST'])
def add_expense():
    data        = request.get_json()
    user_id     = data.get('user_id', 1)
    amount      = data.get('amount')
    category    = data.get('category', 'Uncategorized')
    description = data.get('description', '')
    date_str    = data.get('date')  # optional, ISO string

    if date_str:
        try:
            tx_date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            tx_date = datetime.utcnow()
    else:
        tx_date = datetime.utcnow()

    transaction = Transaction(
        user_id=user_id,
        type='expense',
        category=category,
        description=description,
        amount=amount,
        date=tx_date,
    )
    db.session.add(transaction)
    db.session.commit()
    return jsonify({
        'message':        'Expense added',
        'transaction_id': transaction.id,
        'daily_allowance': calculate_daily_allowance(user_id),
    }), 201


@routes.route('/add-income', methods=['POST'])
def add_income():
    data         = request.get_json()
    user_id      = data.get('user_id', 1)
    amount       = data.get('amount')
    description  = data.get('description', '')
    period_start_str = data.get('period_start')  # 'YYYY-MM-DD'

    if period_start_str:
        try:
            period_start = datetime.strptime(period_start_str, '%Y-%m-%d')
        except ValueError:
            period_start = datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
    else:
        period_start = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    period_end = compute_period_end(period_start)

    transaction = Transaction(
        user_id=user_id,
        type='income',
        category='Income',
        description=description,
        amount=amount,
        date=period_start,        # transaction date = period start
        period_start=period_start,
        period_end=period_end,
    )
    db.session.add(transaction)
    db.session.commit()

    return jsonify({
        'message':        'Income added',
        'transaction_id': transaction.id,
        'period_start':   period_start.strftime('%Y-%m-%d'),
        'period_end':     period_end.strftime('%Y-%m-%d'),
        'daily_allowance': calculate_daily_allowance(user_id),
    }), 201


# ---------------------------------------------------------------------------
# Transactions — Edit & Delete
# ---------------------------------------------------------------------------

@routes.route('/edit-transaction/<int:transaction_id>', methods=['PUT'])
def edit_transaction(transaction_id):
    data = request.get_json()
    tx   = Transaction.query.get(transaction_id)

    if not tx:
        return jsonify({'error': 'Transaction not found'}), 404

    if 'amount' in data:
        tx.amount = data['amount']

    if 'description' in data:
        tx.description = data['description']

    if 'category' in data:
        tx.category = data['category']

    if 'type' in data and data['type'] in ('income', 'expense'):
        tx.type = data['type']

    if 'date' in data:
        try:
            tx.date = datetime.strptime(data['date'], '%Y-%m-%d')
        except ValueError:
            pass

    # If it's an income transaction and period_start is being changed,
    # recompute period_end automatically
    if tx.type == 'income' and 'period_start' in data:
        try:
            new_period_start = datetime.strptime(data['period_start'], '%Y-%m-%d')
            tx.period_start  = new_period_start
            tx.period_end    = compute_period_end(new_period_start)
            tx.date          = new_period_start
        except ValueError:
            pass

    db.session.commit()
    return jsonify({
        'message':        'Transaction updated',
        'transaction_id': tx.id,
        'daily_allowance': calculate_daily_allowance(tx.user_id),
    }), 200


@routes.route('/delete-transaction/<int:transaction_id>', methods=['DELETE'])
def delete_transaction(transaction_id):
    tx = Transaction.query.get(transaction_id)
    if not tx:
        return jsonify({'error': 'Transaction not found'}), 404

    user_id = tx.user_id
    db.session.delete(tx)
    db.session.commit()
    return jsonify({
        'message':        'Transaction deleted',
        'daily_allowance': calculate_daily_allowance(user_id),
    }), 200


# ---------------------------------------------------------------------------
# Get Transactions (active period)
# ---------------------------------------------------------------------------

@routes.route('/get-transactions', methods=['GET'])
def get_transactions():
    user_id      = request.args.get('user_id', type=int)
    period_start, period_end = get_active_period_range(user_id)

    txns = (
        Transaction.query
        .filter_by(user_id=user_id)
        .filter(Transaction.date >= period_start, Transaction.date <= period_end)
        .order_by(Transaction.date.desc())
        .all()
    )

    return jsonify([{
        'id':           t.id,
        'type':         t.type,
        'category':     t.category,
        'description':  t.description,
        'amount':       t.amount,
        'date':         t.date.strftime('%Y-%m-%d'),
        'period_start': t.period_start.strftime('%Y-%m-%d') if t.period_start else None,
        'period_end':   t.period_end.strftime('%Y-%m-%d')   if t.period_end   else None,
    } for t in txns]), 200


# ---------------------------------------------------------------------------
# Dashboard Data
# ---------------------------------------------------------------------------

@routes.route('/dashboard-data', methods=['GET'])
def dashboard_data():
    user_id = request.args.get('user_id', type=int)

    period_start, period_end = get_active_period_range(user_id)

    # Total income in active period
    income_txns = (
        Transaction.query
        .filter_by(user_id=user_id, type='income')
        .filter(Transaction.date >= period_start, Transaction.date <= period_end)
        .all()
    )
    total_income = sum(t.amount for t in income_txns)

    # Total expenses in active period
    expense_txns = (
        Transaction.query
        .filter_by(user_id=user_id, type='expense')
        .filter(Transaction.date >= period_start, Transaction.date <= period_end)
        .all()
    )
    total_expenses = sum(t.amount for t in expense_txns)

    # Recent transactions (last 5)
    recent = (
        Transaction.query
        .filter_by(user_id=user_id)
        .filter(Transaction.date >= period_start, Transaction.date <= period_end)
        .order_by(Transaction.date.desc())
        .limit(5)
        .all()
    )

    # Previous period leftover (display note)
    leftover = get_previous_period_leftover(user_id)

    # Fixed reservations summary
    categories  = Category.query.filter_by(user_id=user_id).all()
    total_fixed = sum(c.budget for c in categories if c.is_fixed)

    return jsonify({
        'total_income':         total_income,
        'total_expenses':       total_expenses,
        'remaining_balance':    calculate_remaining_balance(user_id),
        'daily_allowance':      calculate_daily_allowance(user_id),
        'days_remaining':       get_days_remaining(user_id),
        'period_start':         period_start.strftime('%Y-%m-%d'),
        'period_end':           period_end.strftime('%Y-%m-%d'),
        'total_fixed_reserved': total_fixed,
        'budget_vs_spent':      get_budget_vs_spent(user_id),
        'recent_transactions':  [{
            'id':          t.id,
            'type':        t.type,
            'category':    t.category,
            'description': t.description,
            'amount':      t.amount,
            'date':        t.date.strftime('%Y-%m-%d'),
        } for t in recent],
        'previous_period_leftover': leftover,
    }), 200


# ---------------------------------------------------------------------------
# Curveball
# ---------------------------------------------------------------------------

@routes.route('/trigger-curveball', methods=['POST'])
def trigger_curveball_route():
    data    = request.get_json()
    user_id = data.get('user_id', 1)
    result  = trigger_curveball(user_id)
    return jsonify(result), 200


# ---------------------------------------------------------------------------
# Toggle Fixed flag on a category
# ---------------------------------------------------------------------------

@routes.route('/toggle-fixed/<int:category_id>', methods=['PUT'])
def toggle_fixed(category_id):
    data     = request.get_json()
    user_id  = data.get('user_id')
    is_fixed = data.get('is_fixed')  # boolean

    cat = Category.query.filter_by(id=category_id, user_id=user_id).first()
    if not cat:
        return jsonify({'error': 'Category not found'}), 404

    cat.is_fixed = is_fixed
    db.session.commit()
    return jsonify({
        'message':         'Fixed flag updated',
        'id':              cat.id,
        'name':            cat.name,
        'is_fixed':        cat.is_fixed,
        'daily_allowance': calculate_daily_allowance(user_id),
    }), 200
