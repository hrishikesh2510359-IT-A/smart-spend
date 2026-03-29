from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id              = db.Column(db.Integer, primary_key=True)
    username        = db.Column(db.String(150), unique=True, nullable=False)
    password        = db.Column(db.String(150), nullable=False)
    monthly_income  = db.Column(db.Float, default=0.0)

    categories   = db.relationship('Category',    backref='user', lazy=True)
    transactions = db.relationship('Transaction', backref='user', lazy=True)


class Category(db.Model):
    __tablename__ = 'categories'
    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name    = db.Column(db.String(100), nullable=False)
    budget  = db.Column(db.Float, default=0.0)

    # is_fixed: if True, the full budget amount is treated as a reservation
    # that reduces daily allowance automatically — no transaction is created.
    # The actual expense is logged manually when it occurs.
    is_fixed = db.Column(db.Boolean, default=False)


class Transaction(db.Model):
    __tablename__ = 'transactions'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # 'expense' or 'income'
    type        = db.Column(db.String(20), nullable=False)
    category    = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255), default='')
    amount      = db.Column(db.Float, nullable=False)
    date        = db.Column(db.DateTime, nullable=False)

    # Only populated for income transactions:
    # period_start → the date the user said their income period begins
    # period_end   → last day of the month after period_start's month
    #                e.g. period_start = 2026-03-25  →  period_end = 2026-04-30
    period_start = db.Column(db.DateTime, nullable=True)
    period_end   = db.Column(db.DateTime, nullable=True)
