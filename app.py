import os
import sqlite3
from functools import wraps

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-commitlearn-secret")

DATABASE = "database.db"
MAX_ACTIVE_GOALS = 3


def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db_connection()
    goals_cols = conn.execute("PRAGMA table_info(goals)").fetchall()
    col_names = {col[1] for col in goals_cols}
    # Drop legacy schema (no users / no user_id) so dummy goals cannot leak in.
    if goals_cols and "user_id" not in col_names:
        conn.executescript("DROP TABLE IF EXISTS goals; DROP TABLE IF EXISTS users;")
    with open("schema.sql", "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


@app.before_request
def load_logged_in_user():
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
        return

    conn = get_db_connection()
    g.user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()


def count_active_goals(conn, user_id):
    return conn.execute(
        "SELECT COUNT(*) FROM goals WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchone()[0]


def promote_oldest_backlog(conn, user_id):
    """Move the oldest backlog goal into an active slot when one is free."""
    if count_active_goals(conn, user_id) >= MAX_ACTIVE_GOALS:
        return

    oldest = conn.execute(
        """
        SELECT id FROM goals
        WHERE user_id = ? AND status = 'backlog'
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()

    if oldest:
        conn.execute(
            "UPDATE goals SET status = 'active', last_update_at = CURRENT_TIMESTAMP WHERE id = ?",
            (oldest["id"],),
        )


def get_user_goal(conn, goal_id, user_id):
    return conn.execute(
        "SELECT * FROM goals WHERE id = ? AND user_id = ?",
        (goal_id, user_id),
    ).fetchone()


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        error = None

        if not username:
            error = "Username is required."
        elif not email:
            error = "Email is required."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm:
            error = "Passwords do not match."

        if error is None:
            conn = get_db_connection()
            try:
                conn.execute(
                    "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                    (username, email, generate_password_hash(password)),
                )
                conn.commit()
                user = conn.execute(
                    "SELECT id FROM users WHERE username = ?", (username,)
                ).fetchone()
                session.clear()
                session["user_id"] = user["id"]
            except sqlite3.IntegrityError:
                error = "That username or email is already registered."
            finally:
                conn.close()

        if error is None:
            flash("Welcome to CommitLearn. Your dashboard is empty — add your first commitment.", "success")
            return redirect(url_for("index"))

        flash(error, "error")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        error = None

        conn = get_db_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? OR email = ?",
            (identifier, identifier.lower()),
        ).fetchone()
        conn.close()

        if user is None or not check_password_hash(user["password_hash"], password):
            error = "Invalid username/email or password."

        if error is None:
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("index"))

        flash(error, "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    conn = get_db_connection()
    active_goals = conn.execute(
        """
        SELECT * FROM goals
        WHERE user_id = ? AND status = 'active'
        ORDER BY created_at ASC, id ASC
        """,
        (g.user["id"],),
    ).fetchall()
    backlog_goals = conn.execute(
        """
        SELECT * FROM goals
        WHERE user_id = ? AND status = 'backlog'
        ORDER BY created_at ASC, id ASC
        """,
        (g.user["id"],),
    ).fetchall()
    conn.close()
    return render_template(
        "index.html",
        active_goals=active_goals,
        backlog_goals=backlog_goals,
        max_active=MAX_ACTIVE_GOALS,
    )


@app.route("/create", methods=["GET", "POST"])
@login_required
def create_goal():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        target_days = request.form.get("target_days", "").strip()
        checkpoint_total = request.form.get("checkpoint_total", "").strip()
        error = None

        if not title:
            error = "Title is required."
        else:
            try:
                target_days_val = int(target_days)
                checkpoint_total_val = int(checkpoint_total)
                if target_days_val < 1 or checkpoint_total_val < 1:
                    error = "Target days and checkpoints must be at least 1."
            except ValueError:
                error = "Target days and checkpoints must be numbers."

        if error:
            flash(error, "error")
            return render_template("new_goal.html")

        conn = get_db_connection()
        status = (
            "active"
            if count_active_goals(conn, g.user["id"]) < MAX_ACTIVE_GOALS
            else "backlog"
        )
        conn.execute(
            """
            INSERT INTO goals (user_id, title, target_days, checkpoint_total, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (g.user["id"], title, target_days_val, checkpoint_total_val, status),
        )
        conn.commit()
        conn.close()

        if status == "backlog":
            flash(
                "You already have 3 active goals. This commitment was added to the backlog.",
                "info",
            )
        else:
            flash("Commitment created.", "success")
        return redirect(url_for("index"))

    return render_template("new_goal.html")


@app.route("/goals/<int:goal_id>/complete", methods=["POST"])
@login_required
def complete_goal(goal_id):
    conn = get_db_connection()
    goal = get_user_goal(conn, goal_id, g.user["id"])
    if goal is None:
        conn.close()
        flash("Goal not found.", "error")
        return redirect(url_for("index"))

    was_active = goal["status"] == "active"
    conn.execute(
        """
        UPDATE goals
        SET status = 'completed', checkpoint_done = checkpoint_total, last_update_at = CURRENT_TIMESTAMP
        WHERE id = ? AND user_id = ?
        """,
        (goal_id, g.user["id"]),
    )
    if was_active:
        promote_oldest_backlog(conn, g.user["id"])
    conn.commit()
    conn.close()
    flash("Goal marked complete.", "success")
    return redirect(url_for("index"))


@app.route("/goals/<int:goal_id>/delete", methods=["POST"])
@login_required
def delete_goal(goal_id):
    conn = get_db_connection()
    goal = get_user_goal(conn, goal_id, g.user["id"])
    if goal is None:
        conn.close()
        flash("Goal not found.", "error")
        return redirect(url_for("index"))

    was_active = goal["status"] == "active"
    conn.execute(
        "DELETE FROM goals WHERE id = ? AND user_id = ?",
        (goal_id, g.user["id"]),
    )
    if was_active:
        promote_oldest_backlog(conn, g.user["id"])
    conn.commit()
    conn.close()
    flash("Goal removed.", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
