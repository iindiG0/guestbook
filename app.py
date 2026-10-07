# Guestbook: a small message board with register, login, logout and PostgreSQL.
#
# Everything runs on the server (Python + Flask). The server builds each page
# from a template and sends finished HTML to the browser. No JavaScript.
#
# All settings come from environment variables (see README.md), so the same
# code runs anywhere: a VM, Docker, Kubernetes, a PaaS.
#
# Database: PostgreSQL managed by CloudNativePG (CNPG). CNPG creates a Secret
# "<cluster>-app" with keys uri, host, port, dbname, user, password. Give the
# app EITHER the uri as DATABASE_URL, OR the separate keys as DB_HOST, DB_PORT,
# DB_NAME, DB_USER, DB_PASSWORD.

import os
import secrets
import socket
import time

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash


def database_conninfo():
    """Build the connection string from DATABASE_URL or from the separate DB_* variables."""
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url
    required = ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"Database settings missing: set DATABASE_URL, or {', '.join(missing)}")
    return make_conninfo(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        sslmode=os.environ.get("DB_SSLMODE", "prefer"),
    )


DATABASE_URL = database_conninfo()
SECRET_KEY = os.environ.get("SECRET_KEY", "")
APP_TITLE = os.environ.get("APP_TITLE", "Guestbook")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

if not SECRET_KEY:
    raise SystemExit("SECRET_KEY must be set: a long random string, e.g. `openssl rand -hex 32`")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,          # signs the session cookie so users can't fake it
    SESSION_COOKIE_HTTPONLY=True,   # page scripts can't read the cookie
    SESSION_COOKIE_SAMESITE="Lax",  # cookie isn't sent on form posts from other sites
    SESSION_COOKIE_SECURE=COOKIE_SECURE,  # true = cookie only sent over HTTPS
)

# When running behind a reverse proxy / load balancer / ingress, trust its
# X-Forwarded-* headers so the app knows the real client IP and http vs https.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

# A pool keeps a few connections open and reuses them (faster than reconnecting).
# It connects in the background and keeps retrying if the database isn't up yet.
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, kwargs={"row_factory": dict_row})


def init_db():
    """Create the tables if they don't exist. Retries while the database is starting."""
    for attempt in range(1, 21):
        try:
            with pool.connection(timeout=10) as conn:
                # gunicorn starts 2 workers at the same time and both run this.
                # The lock makes the second one wait until the first has finished.
                conn.execute("SELECT pg_advisory_xact_lock(4242)")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id            SERIAL PRIMARY KEY,
                        username      TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
                    )""")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id         SERIAL PRIMARY KEY,
                        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        body       TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )""")
            print("Connected to PostgreSQL, tables are ready", flush=True)
            return
        except Exception as err:  # database still starting, DNS not ready, ...
            print(f"Database not ready (attempt {attempt}): {err}", flush=True)
            time.sleep(3)
    raise SystemExit("Could not connect to the database")


def query(sql, params=(), one=False):
    """Run SQL. Values go in separately (%s placeholders), which blocks SQL injection."""
    with pool.connection() as conn:
        cur = conn.execute(sql, params)
        if cur.description is None:  # INSERT/UPDATE/DELETE without RETURNING
            return None
        return cur.fetchone() if one else cur.fetchall()


init_db()

# ---------------------------------------------------------------------------
# Login helpers
# ---------------------------------------------------------------------------


@app.before_request
def load_user():
    """Before every request: who is logged in? (stored in the signed session cookie)"""
    g.user = None
    if "user_id" in session:
        g.user = query("SELECT id, username FROM users WHERE id = %s", (session["user_id"],), one=True)
        if g.user is None:  # user was deleted
            session.clear()


def login_required():
    if g.user is None:
        flash("Please log in first.")
        return redirect(url_for("login"))
    return None


def csrf_token():
    """A random token per session, put in every form. Blocks fake form posts from other sites."""
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST":
        expected = session.get("csrf", "")
        sent = request.form.get("csrf", "")
        # No token in the session = this browser never loaded our form, so reject too
        if not expected or not secrets.compare_digest(sent, expected):
            abort(400, "Invalid form token. Reload the page and try again.")


@app.context_processor
def template_globals():
    """Values every template can use."""
    return {"title": APP_TITLE, "pod": socket.gethostname(), "csrf_token": csrf_token}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    messages = query("""
        SELECT m.id, m.body, m.created_at, m.user_id, u.username
          FROM messages m JOIN users u ON u.id = m.user_id
         ORDER BY m.created_at DESC
         LIMIT 100""")
    return render_template("index.html", messages=messages)


@app.post("/messages")
def post_message():
    if (r := login_required()):
        return r
    body = request.form.get("body", "").strip()
    if not body:
        flash("Message can't be empty.")
    elif len(body) > 500:
        flash("Message is too long (max 500 characters).")
    else:
        query("INSERT INTO messages (user_id, body) VALUES (%s, %s)", (g.user["id"], body))
        flash("Message posted.")
    return redirect(url_for("index"))


@app.post("/messages/<int:message_id>/delete")
def delete_message(message_id):
    if (r := login_required()):
        return r
    # "AND user_id = ..." means you can only delete your own messages
    deleted = query("DELETE FROM messages WHERE id = %s AND user_id = %s RETURNING id",
                    (message_id, g.user["id"]), one=True)
    flash("Message deleted." if deleted else "You can only delete your own messages.")
    return redirect(url_for("index"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if not (3 <= len(username) <= 30 and username.isalnum()):
            flash("Username: 3-30 letters or numbers.")
        elif len(password) < 8:
            flash("Password: at least 8 characters.")
        elif query("SELECT 1 FROM users WHERE username = %s", (username,), one=True):
            flash("That username is taken.")
        else:
            # Store only a one-way hash of the password, never the password itself
            user = query("INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id",
                         (username, generate_password_hash(password)), one=True)
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome, {username}!")
            return redirect(url_for("index"))
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = query("SELECT id, password_hash FROM users WHERE username = %s", (username,), one=True)
        if user and check_password_hash(user["password_hash"], password):
            session.clear()  # fresh session after login
            session["user_id"] = user["id"]
            flash(f"Logged in as {username}.")
            return redirect(url_for("index"))
        # Same message for "no such user" and "wrong password": don't reveal which usernames exist
        flash("Wrong username or password.")
    return render_template("auth.html", mode="login")


@app.post("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("index"))


# Health checks for whatever runs the app (load balancer, Kubernetes, monitoring)

@app.get("/healthz")
def healthz():
    """Liveness: the process is up and answering."""
    return "ok"


@app.get("/readyz")
def readyz():
    """Readiness: the app can reach the database, so it can serve real pages."""
    try:
        query("SELECT 1", one=True)
        return "ready"
    except Exception as err:
        return f"database not reachable: {err.__class__.__name__}", 503
