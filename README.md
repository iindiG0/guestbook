# Guestbook

A small message board: **register, log in, write messages, delete your own, log out**.

- Python 3.10+ / Flask, served by gunicorn
- Pages are built on the server (Jinja templates): no frontend JavaScript, no build step
- Data in PostgreSQL, intended to be a **CloudNativePG (CNPG)** cluster
- All settings come from environment variables, so it runs the same anywhere

## Project files

| File | What it is |
|---|---|
| `app.py` | The application: settings, database, login, pages |
| `templates/` | Page templates: `base.html` (layout), `index.html` (messages), `auth.html` (login/register) |
| `requirements.txt` | Python dependencies (pinned versions) |
| `gunicorn.conf.py` | Web server settings: listens on `0.0.0.0:$PORT` (default 8080), 2 workers, logs to stdout |
| `schema.sql` | Database tables, for review. The app creates them itself on startup |

## How to run

```bash
pip install -r requirements.txt
gunicorn app:app
```

That's the only command. `gunicorn.conf.py` is picked up automatically.

## Configuration (environment variables)

### Database (CNPG)

CNPG creates a Secret named `<cluster-name>-app` for the application user. Provide the app
**one** of these two options from that Secret:

**Option A: one connection string**

| Variable | CNPG Secret key |
|---|---|
| `DATABASE_URL` | `uri` |

**Option B: separate values**

| Variable | CNPG Secret key | Default |
|---|---|---|
| `DB_HOST` | `host` (e.g. `<cluster>-rw`, the read-write Service) | — |
| `DB_PORT` | `port` | `5432` |
| `DB_NAME` | `dbname` | — |
| `DB_USER` | `user` | — |
| `DB_PASSWORD` | `password` | — |
| `DB_SSLMODE` | — | `prefer` |

Connect to the **`-rw`** Service (the primary). The app writes on most pages.

### Application

| Variable | Required | Default | What it is |
|---|---|---|---|
| `SECRET_KEY` | **yes** | — | Signs the login cookie. Long random value, e.g. `openssl rand -hex 32`. Keep it secret, and the same on every instance (otherwise users get logged out when they hit another instance) |
| `PORT` | no | `8080` | Port to listen on |
| `COOKIE_SECURE` | no | `false` | Set `true` when the site is served over HTTPS (cookie is then never sent over plain HTTP) |
| `APP_TITLE` | no | `Guestbook` | Title shown on the page |

The app refuses to start if the database settings or `SECRET_KEY` are missing, and says which.

## Database

- On startup the app runs `CREATE TABLE IF NOT EXISTS` for `users` and `messages` (see `schema.sql`).
  The database user therefore needs permission to create tables in its database. The CNPG
  application user (the database owner) has this by default.
- Several instances can start at the same time: a PostgreSQL advisory lock makes them take turns.
- If the database isn't reachable yet, the app retries for about a minute before giving up.

## Health checks

| Path | Meaning | Use for |
|---|---|---|
| `/healthz` | Process is up (200 `ok`) | Liveness: restart if it fails |
| `/readyz` | Database is reachable (200 `ready`, or 503) | Readiness: only send traffic when it passes |

## Running more than one instance

The app keeps no state in memory or on local disk: logins are in signed cookies and data is in
PostgreSQL. You can run as many instances as you like behind a load balancer, as long as they share
the same `SECRET_KEY` and database.

It trusts one level of `X-Forwarded-For/Proto/Host` headers, as set by a reverse proxy, load
balancer or ingress in front of it.

## Logs

Access and error logs go to stdout/stderr (no log files).

## Security built in

- Passwords stored only as one-way hashes (scrypt)
- Login in a signed, HttpOnly cookie; `SameSite=Lax`; `Secure` when `COOKIE_SECURE=true`
- CSRF token in every form; forged posts are rejected with 400
- SQL uses parameters only (no string building), which blocks SQL injection
- Users can only delete their own messages (enforced in the SQL)
- User text is escaped, never run as HTML
- Same error for "unknown user" and "wrong password"
