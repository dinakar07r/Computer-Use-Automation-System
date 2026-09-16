"""
Mock legacy bank/credit-union back-office application.

Deliberately legacy in style: server-rendered HTML, table-based layout,
minimal CSS, no data-testid / no stable id attributes on most interactive
elements. This is the stand-in "one concrete surface" the assignment asks
for (Section 4: "a local sample app you build or mock").

Run:
    python -m src.target_app.app
Then browse to http://127.0.0.1:5055/login  (operator / operator123)
"""
from flask import Flask, render_template, request, redirect, url_for, session

from . import data

app = Flask(__name__)
app.secret_key = "dev-only-not-a-real-secret"  # local demo app only

OPERATOR_USER = "operator"
OPERATOR_PASS = "operator123"


def _require_login():
    return session.get("logged_in") is True


@app.route("/")
def index():
    if _require_login():
        return redirect(url_for("search"))
    return redirect(url_for("login"))


@app.route("/__test__/force_interstitial")
def force_interstitial():
    """Test-only helper (not part of the app's real UI): deterministically
    forces the transient interstitial on the next /members/search request in
    this browser session. Used by `replay --inject-interstitial-before-step`
    to produce reproducible RECOVERABLE-path evidence rather than relying on
    the organic every-Nth-request trigger, which is realistic but not
    reproducible across separate test runs. Mirrors the existing
    --inject-session-timeout-before-step fault-injection pattern."""
    session["force_interstitial"] = True
    return ("ok", 200)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == OPERATOR_USER and p == OPERATOR_PASS:
            session["logged_in"] = True
            return redirect(url_for("search"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/members/search", methods=["GET", "POST"])
def search():
    if not _require_login():
        return redirect(url_for("login"))

    if session.pop("force_interstitial", False) or data.next_request_is_slow():
        # Simulates a transient slow / interstitial screen a real legacy app
        # occasionally shows (e.g. "processing, please wait"). Triggered
        # either organically (every Nth request -- realistic, unpredictable)
        # or deterministically via /__test__/force_interstitial (used by
        # `replay --inject-interstitial-before-step` to produce reproducible
        # RECOVERABLE-path evidence on demand).
        next_url = request.url
        if request.method == "POST":
            next_url = url_for("search")
        return render_template("interstitial.html", next_url=next_url)

    error = None
    if request.method == "POST":
        member_id = request.form.get("member_id", "").strip()
        member = data.get_member(member_id)
        if member is None:
            error = f"No member found with ID {member_id!r}."
        else:
            return redirect(url_for("member_detail", member_id=member_id))
    return render_template("search.html", error=error)


@app.route("/members/<member_id>")
def member_detail(member_id):
    if not _require_login():
        return redirect(url_for("login"))
    member = data.get_member(member_id)
    if member is None:
        return render_template("search.html", error=f"No member found with ID {member_id!r}."), 404
    return render_template("member.html", member=member)


@app.route("/members/<member_id>/subaccounts/new", methods=["GET", "POST"])
def subaccount_new(member_id):
    if not _require_login():
        return redirect(url_for("login"))
    member = data.get_member(member_id)
    if member is None:
        return render_template("search.html", error=f"No member found with ID {member_id!r}."), 404

    error = None
    if request.method == "POST":
        account_type = request.form.get("account_type", "")
        try:
            deposit = float(request.form.get("initial_deposit", "0") or "0")
        except ValueError:
            deposit = -1

        if member["status"] == "frozen":
            error = "PERMISSION_DENIED: this member's account is frozen and cannot be modified."
        elif deposit < 25.00:
            error = "VALIDATION_ERROR: initial deposit must be at least $25.00."
        else:
            return render_template(
                "subaccount_confirm.html",
                member=member,
                account_type=account_type,
                deposit=deposit,
            )
    return render_template("subaccount_new.html", member=member, error=error)


@app.route("/members/<member_id>/subaccounts/confirm", methods=["POST"])
def subaccount_confirm(member_id):
    if not _require_login():
        return redirect(url_for("login"))
    account_type = request.form.get("account_type")
    deposit = float(request.form.get("deposit"))
    try:
        record = data.create_subaccount(member_id, account_type, deposit)
    except KeyError:
        return render_template("search.html", error=f"No member found with ID {member_id!r}."), 404
    except PermissionError:
        member = data.get_member(member_id)
        return render_template(
            "subaccount_new.html",
            member=member,
            error="PERMISSION_DENIED: this member's account is frozen and cannot be modified.",
        )
    except ValueError as e:
        member = data.get_member(member_id)
        return render_template(
            "subaccount_new.html",
            member=member,
            error=f"VALIDATION_ERROR: {e}",
        )
    member = data.get_member(member_id)
    return render_template("subaccount_result.html", member=member, record=record)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
