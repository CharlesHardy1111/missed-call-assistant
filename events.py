"""Shared missed-call classification and event input handling."""
from flask import request

MISSED_STATUSES = frozenset({"no-answer", "busy", "failed", "canceled"})


def event_data():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else request.form
