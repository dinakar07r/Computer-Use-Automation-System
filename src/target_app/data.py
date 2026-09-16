"""
In-memory 'core banking' data store for the mock legacy back-office app.

This stands in for the real bank/credit-union servicing system the assignment
describes. It intentionally has no API — the only way in is the rendered UI.
"""
import itertools
import threading
import time

_lock = threading.Lock()
_next_subaccount_seq = itertools.count(1001)

MEMBERS = {
    "12345": {
        "id": "12345",
        "name": "Dinakar Rao",
        "status": "active",
        "savings_balance": 4820.55,
        "checking_balance": 1120.10,
        "subaccounts": [],
    },
    "40000": {
        "id": "40000",
        "name": "Frozen Account Member",
        "status": "frozen",  # triggers a permission-denial business outcome
        "savings_balance": 300.00,
        "checking_balance": 0.00,
        "subaccounts": [],
    },
    "55555": {
        "id": "55555",
        "name": "Priya Nair",
        "status": "active",
        "savings_balance": 15230.00,
        "checking_balance": 900.00,
        "subaccounts": [],
    },
}

# Request counter used to deterministically inject a one-off "transient slow /
# interstitial" condition, so replay evidence can show recoverable-condition
# handling without relying on real network flakiness.
_request_count = itertools.count(1)


def next_request_is_slow() -> bool:
    """Every 5th request to the app simulates a transient slow load."""
    n = next(_request_count)
    return n % 5 == 0


def get_member(member_id: str):
    return MEMBERS.get(member_id)


def create_subaccount(member_id: str, account_type: str, initial_deposit: float):
    with _lock:
        member = MEMBERS.get(member_id)
        if member is None:
            raise KeyError("member not found")
        if member["status"] == "frozen":
            raise PermissionError("member account is frozen")
        if initial_deposit < 25.00:
            raise ValueError("initial deposit below minimum ($25.00)")
        acct_no = f"SUB-{next(_next_subaccount_seq)}"
        record = {
            "account_number": acct_no,
            "type": account_type,
            "balance": initial_deposit,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        member["subaccounts"].append(record)
        return record
