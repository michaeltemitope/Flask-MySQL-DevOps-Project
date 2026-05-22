# =============================================================
# health.py
# Purpose  : Health and readiness check logic for the Flask app
# Location : app/health.py  (same folder as app.py)
# Used by  : app.py — imported and registered as a Blueprint
#
# WHY a separate file?
#   app.py is already responsible for routing, DB config, and
#   the main application logic. Health logic has its own job:
#   answering infrastructure questions ("is the app alive?",
#   "is the app ready to take traffic?"). Keeping it separate
#   means you can find, read, and update it instantly without
#   scrolling through unrelated code. This is the single
#   responsibility principle — one file, one job.
#
# HOW it plugs into app.py:
#   Flask Blueprints let you define routes in one file and
#   register them onto the main app object in another file.
#   Think of a Blueprint like a mini-app that gets attached
#   to the real app at startup. Two lines in app.py are all
#   you need (shown in the app.py diff section below).
# =============================================================

import time

from flask import Blueprint, jsonify, current_app

# Create a Blueprint named 'health'.
# The first argument is the blueprint's internal name (used by Flask).
# The second argument is always __name__ — it tells Flask where this
# file lives so it can find templates and static files if needed.
health_bp = Blueprint('health', __name__)


# =============================================================
# /health — Liveness Check
# =============================================================
#
# PURPOSE:
#   Answer one single question: "Is the Flask process alive?"
#   This endpoint does the absolute minimum — it returns a
#   fixed JSON response. No database. No external calls.
#   If this endpoint responds, the process is running.
#
# WHAT PROBLEM IT SOLVES:
#   Docker's healthcheck, AWS ALBs, and Kubernetes liveness
#   probes all need a fast, lightweight endpoint to hit every
#   few seconds. If you make them query the database, you add
#   unnecessary load and risk false negatives (e.g. DB is slow
#   but the Flask process is perfectly fine). Separating
#   liveness from readiness is a core production practice.
#
# HTTP STATUS CODES:
#   200 OK — Flask is alive and responding.
#
# =============================================================
@health_bp.route('/health')
def health():
    """
    Liveness check — confirms Flask is running.
    No database check. Intentionally lightweight.
    """
    return jsonify({
        "status": "healthy",
        "service": "flask-app",
    }), 200


# =============================================================
# /ready — Readiness Check
# =============================================================
#
# PURPOSE:
#   Answer a harder question: "Is the app ready to serve real
#   user traffic?" This means Flask is up AND the database
#   connection works AND the required table exists and is
#   queryable.
#
# WHAT PROBLEM IT SOLVES:
#   During startup, Flask boots in a few milliseconds but MySQL
#   can take 10–30 seconds to be ready. Without a readiness
#   check, a load balancer might route traffic to a container
#   whose DB connection isn't established yet, giving users
#   500 errors. The readiness check lets the infrastructure
#   hold traffic back until the app is truly ready.
#
#   This is also useful after a DB restart or network blip —
#   the readiness check will fail, the load balancer stops
#   sending traffic, and resumes only when the DB reconnects.
#
# HOW IT WORKS INTERNALLY (step by step):
#   1. Record the start time (for measuring response latency).
#   2. Grab the 'mysql' object from the Flask app context.
#      (current_app is Flask's way of safely accessing the app
#      object from inside a Blueprint without circular imports.)
#   3. Open a DictCursor — this is a cursor that returns rows
#      as Python dictionaries instead of plain tuples, making
#      them easier to work with.
#   4. Run "SELECT 1" — the simplest possible query. It doesn't
#      touch any table. It just asks MySQL "are you there?" and
#      MySQL replies with the number 1. If this fails, the DB
#      connection is broken.
#   5. Run "SELECT 1 FROM message LIMIT 1" — this confirms the
#      'message' table exists and is readable. LIMIT 1 means
#      MySQL stops after finding one row, so it's fast even if
#      the table has thousands of rows.
#   6. If both queries succeed, return 200 with a JSON body
#      showing what was checked and how long it took.
#   7. If anything fails (connection error, table missing,
#      query timeout), catch the exception, log it, and return
#      503 with a JSON body describing what failed. 503 means
#      "Service Unavailable" — the correct code for "I'm up
#      but not ready yet."
#
# HTTP STATUS CODES:
#   200 OK             — App is fully ready to serve traffic.
#   503 Service        — App is running but DB is unreachable
#       Unavailable      or the required table is missing.
#
# =============================================================
@health_bp.route('/ready')
def ready():
    """
    Readiness check — confirms Flask AND MySQL are operational.
    Returns 503 if the database is unreachable or not ready.
    """
    start_time = time.time()

    # Step 1: Grab the mysql extension that was initialised in app.py.
    # current_app is Flask's proxy to the real app object. We use it
    # here (instead of importing app directly) to avoid a circular
    # import: health.py would import app, and app.py imports health.py.
    mysql = current_app.extensions.get('mysql')

    # Step 2: Defensive check — if mysql wasn't configured on the app,
    # return 503 immediately with a clear message. This shouldn't happen
    # in production but catches misconfiguration early.
    if mysql is None:
        return jsonify({
            "status": "not ready",
            "reason": "MySQL extension is not configured on this app.",
            "checks": {
                "mysql_extension": "missing",
            },
        }), 503

    # Step 3: Try to connect and run two queries inside a try/except.
    # Any exception here means something is wrong with the DB layer.
    try:
        # Open a DictCursor — rows come back as dicts, not plain tuples.
        # This matches how the rest of the app uses MySQL (via flask_mysqldb).
        cursor = mysql.connection.cursor()

        # Query 1: "SELECT 1" — the most minimal connectivity test.
        # If MySQL is unreachable, this line raises an exception.
        cursor.execute("SELECT 1")
        cursor.fetchone()  # consume the result so the cursor is clean

        # Query 2: Confirm the 'message' table exists and is readable.
        # LIMIT 1 keeps this fast regardless of table size.
        # If the table doesn't exist, MySQL raises an OperationalError.
        cursor.execute("SELECT 1 FROM message LIMIT 1")
        cursor.fetchone()  # consume the result

        cursor.close()

        # Calculate how many milliseconds the DB check took.
        # This is useful for spotting slow DB responses over time.
        elapsed_ms = round((time.time() - start_time) * 1000, 2)

        # All checks passed — return 200.
        return jsonify({
            "status": "ready",
            "checks": {
                "mysql_connection": "ok",
                "message_table": "ok",
            },
            "response_time_ms": elapsed_ms,
        }), 200

    except Exception as e:
        # Something failed. Log it so it shows up in `docker logs`.
        # current_app.logger writes to Flask's built-in logger, which
        # Gunicorn forwards to stdout — visible in `docker compose logs`.
        current_app.logger.error("Readiness check failed: %s", str(e))

        elapsed_ms = round((time.time() - start_time) * 1000, 2)

        # Return 503 — "I am running but I am not ready."
        # Include the error message so you can diagnose the problem
        # directly from the response body without grepping logs.
        return jsonify({
            "status": "not ready",
            "reason": str(e),
            "checks": {
                "mysql_connection": "failed",
                "message_table": "unknown",
            },
            "response_time_ms": elapsed_ms,
        }), 503
