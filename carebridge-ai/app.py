"""
WeCareSG - Main Application Server
==========================================
Flask server tying together:
  - A public landing page.
  - A single login page with separate Citizen and Case Worker sign-in flows
    (session-based auth).
  - A Citizen Dashboard (chat intake + the citizen's own case history) and a
    Case Worker Dashboard (live multi-channel case feed), each gated to its
    own role.
  - REST endpoints for triage submissions, case retrieval, and status
    updates — all gated by session role.
  - A Server-Sent Events (SSE) stream that pushes newly created cases (from
    EITHER the web UI or the Telegram bot) to the case worker dashboard in
    real time.
  - Telegram bot bootstrap (long-polling for local dev, or webhook for
    production), running on a background thread so it never blocks Flask.

Every submission runs through the same pipeline regardless of channel:
    sanitizer.sanitize() -> triage_engine.analyze_hardship()
    -> database.save_full_case() -> database.get_case() -> database.publish_case()

Run with:  python app.py
"""

import functools
import json
import logging
import math
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    stream_with_context,
    url_for,
)
from werkzeug.utils import secure_filename

import bot_service
import database
import triage_engine
from utils import sanitizer

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("wecaresg.app")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "").strip() or "dev-only-insecure-secret-change-me"
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

FOLLOW_UP_UPLOAD_DIR = Path(app.instance_path) / "follow_up_documents"
FOLLOW_UP_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_FOLLOW_UP_EXTENSIONS = {"pdf", "doc", "docx", "txt"}
ONEMAP_API_TOKEN = os.environ.get("ONEMAP_API_TOKEN", "").strip()

# Curated support points displayed after a live postal-code lookup. Review these
# periodically against the MSF/Food Bank directories before production launch.
SUPPORT_LOCATIONS = [
    {"name": "Social Service Office @ Ang Mo Kio", "type": "SSO", "address": "AMK Hub, 53 Ang Mo Kio Ave 3, Singapore 569933", "lat": 1.3697, "lng": 103.8488, "support": "ComCare and social assistance"},
    {"name": "Social Service Office @ Bedok", "type": "SSO", "address": "21 Bedok North Street 1, Singapore 469659", "lat": 1.3315, "lng": 103.9305, "support": "ComCare and social assistance"},
    {"name": "Social Service Office @ Bukit Batok", "type": "SSO", "address": "369 Bukit Batok Street 31, Singapore 650369", "lat": 1.3580, "lng": 103.7502, "support": "ComCare and social assistance"},
    {"name": "Social Service Office @ Jurong East", "type": "SSO", "address": "80 Jurong East Street 21, Singapore 609607", "lat": 1.3334, "lng": 103.7409, "support": "ComCare and social assistance"},
    {"name": "Allkin Family Service Centre @ Ang Mo Kio", "type": "FSC", "address": "230 Ang Mo Kio Ave 3, Singapore 560230", "lat": 1.3678, "lng": 103.8430, "support": "Family casework and practical support"},
    {"name": "Care Corner FSC @ Toa Payoh", "type": "FSC", "address": "158 Lorong 1 Toa Payoh, Singapore 310158", "lat": 1.3302, "lng": 103.8468, "support": "Family casework and practical support"},
    {"name": "Care Corner FSC @ Tampines", "type": "FSC", "address": "299B Tampines Street 22, Singapore 522299", "lat": 1.3516, "lng": 103.9544, "support": "Family casework and practical support"},
    {"name": "The Food Bank Singapore", "type": "Food support", "address": "218 Pandan Loop, Singapore 128408", "lat": 1.3102, "lng": 103.7607, "support": "Food support and Bank Card programme"},
    {"name": "Food Bank Box @ Plaza Singapura", "type": "Food support", "address": "68 Orchard Road, Singapore 238839", "lat": 1.3005, "lng": 103.8453, "support": "Food Bank Box and essentials support"},
]

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_MODE = os.environ.get("TELEGRAM_MODE", "polling").strip().lower()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()

database.init_db()

# In-memory holding area for analyzed-but-not-yet-submitted web triage
# results. Citizens must explicitly confirm before a case is persisted and
# broadcast to the case worker dashboard — nothing is saved on analysis alone.
PENDING_PREVIEWS = {}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def citizen_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("role") != "citizen":
            return redirect(url_for("login_page", role="citizen"))
        return view(*args, **kwargs)

    return wrapped


def worker_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("role") != "worker":
            return redirect(url_for("login_page", role="worker"))
        return view(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------
@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/login")
def login_page():
    role = request.args.get("role", "citizen")
    if role not in ("citizen", "worker"):
        role = "citizen"
    return render_template("login.html", active_role=role, error=None)


@app.route("/login/citizen", methods=["POST"])
def login_citizen():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    citizen = database.verify_citizen_credentials(username, password)
    if not citizen:
        return render_template("login.html", active_role="citizen", error="Invalid username or password.")

    session["role"] = "citizen"
    session["citizen_id"] = citizen["id"]
    session["display_name"] = citizen["display_name"]
    return redirect(url_for("citizen_dashboard"))


@app.route("/login/worker", methods=["POST"])
def login_worker():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    worker = database.verify_worker_credentials(username, password)
    if not worker:
        return render_template("login.html", active_role="worker", error="Invalid username or password.")

    session["role"] = "worker"
    session["worker_id"] = worker["id"]
    session["display_name"] = worker["display_name"]
    session["worker_code"] = worker["worker_code"]
    return redirect(url_for("worker_dashboard"))


@app.route("/signup")
def signup_page():
    role = request.args.get("role", "citizen")
    if role not in ("citizen", "worker"):
        role = "citizen"
    return render_template("signup.html", active_role=role, error=None, form_values={})


@app.route("/signup/citizen", methods=["POST"])
def signup_citizen():
    display_name = (request.form.get("display_name") or "").strip()
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    confirm_password = request.form.get("confirm_password") or ""
    form_values = {"display_name": display_name, "username": username, "email": email}

    if not display_name or not username or not email or not password:
        return render_template("signup.html", active_role="citizen", error="All fields are required.", form_values=form_values)
    if not EMAIL_PATTERN.match(email):
        return render_template("signup.html", active_role="citizen", error="Please enter a valid email address.", form_values=form_values)
    if len(password) < 8:
        return render_template(
            "signup.html", active_role="citizen", error="Password must be at least 8 characters.", form_values=form_values
        )
    if password != confirm_password:
        return render_template("signup.html", active_role="citizen", error="Passwords do not match.", form_values=form_values)

    try:
        citizen = database.create_citizen_account(username, email, password, display_name)
    except database.UsernameTakenError:
        return render_template(
            "signup.html", active_role="citizen", error=f"Username '{username}' is already taken.", form_values=form_values
        )
    except database.EmailTakenError:
        return render_template(
            "signup.html", active_role="citizen", error=f"Email '{email}' is already registered.", form_values=form_values
        )

    session["role"] = "citizen"
    session["citizen_id"] = citizen["id"]
    session["display_name"] = citizen["display_name"]
    return redirect(url_for("citizen_dashboard"))


@app.route("/signup/worker", methods=["POST"])
def signup_worker():
    display_name = (request.form.get("display_name") or "").strip()
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    confirm_password = request.form.get("confirm_password") or ""
    form_values = {"display_name": display_name, "username": username, "email": email}

    if not display_name or not username or not email or not password:
        return render_template("signup.html", active_role="worker", error="All fields are required.", form_values=form_values)
    if not EMAIL_PATTERN.match(email):
        return render_template("signup.html", active_role="worker", error="Please enter a valid email address.", form_values=form_values)
    if len(password) < 8:
        return render_template(
            "signup.html", active_role="worker", error="Password must be at least 8 characters.", form_values=form_values
        )
    if password != confirm_password:
        return render_template("signup.html", active_role="worker", error="Passwords do not match.", form_values=form_values)

    try:
        worker = database.create_worker_account(username, email, password, display_name)
    except database.UsernameTakenError:
        return render_template(
            "signup.html", active_role="worker", error=f"Username '{username}' is already taken.", form_values=form_values
        )
    except database.EmailTakenError:
        return render_template(
            "signup.html", active_role="worker", error=f"Email '{email}' is already registered.", form_values=form_values
        )

    session["role"] = "worker"
    session["worker_id"] = worker["id"]
    session["display_name"] = worker["display_name"]
    session["worker_code"] = worker["worker_code"]
    return redirect(url_for("worker_dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("landing"))


# ---------------------------------------------------------------------------
# Role-gated dashboards
# ---------------------------------------------------------------------------
@app.route("/citizen")
@citizen_required
def citizen_dashboard():
    return render_template("citizen_dashboard.html", display_name=session.get("display_name"))


@app.route("/citizen/cases")
@citizen_required
def citizen_cases_page():
    return render_template("citizen_cases.html", display_name=session.get("display_name"))


@app.route("/support-locator")
@citizen_required
def support_locator_page():
    return render_template("support_locator.html", display_name=session.get("display_name"))


@app.route("/support-planner")
@citizen_required
def support_planner_page():
    return render_template("support_planner.html", display_name=session.get("display_name"))


@app.route("/worker")
@worker_required
def worker_dashboard():
    return render_template(
        "worker_dashboard.html", display_name=session.get("display_name"), worker_code=session.get("worker_code")
    )


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------
@app.route("/api/demo-scenarios", methods=["GET"])
@citizen_required
def get_demo_scenarios():
    return jsonify(triage_engine.DEMO_SCENARIOS)


@app.route("/api/triage/preview", methods=["POST"])
@citizen_required
def api_triage_preview():
    """Analyzes the citizen's message WITHOUT persisting anything. The
    result is held server-side under a one-time preview_id until the
    citizen explicitly confirms via /api/triage/submit."""
    payload = request.get_json(silent=True) or {}
    raw_message = (payload.get("message") or "").strip()

    if not raw_message:
        return jsonify({"error": "Message text is required."}), 400

    sanitized_text = sanitizer.sanitize(raw_message)
    try:
        analysis = triage_engine.analyze_hardship(sanitized_text)
    except triage_engine.TriageError as error:
        return jsonify({"error": str(error)}), 503

    preview_id = uuid.uuid4().hex
    PENDING_PREVIEWS[preview_id] = analysis

    return jsonify({"preview_id": preview_id, **analysis})


@app.route("/api/triage/submit", methods=["POST"])
@citizen_required
def api_triage_submit():
    """Persists a previously analyzed (but not yet saved) case and
    broadcasts it to the live case worker dashboard. This is the only way a
    web-submitted case is ever written to the database."""
    payload = request.get_json(silent=True) or {}
    preview_id = (payload.get("preview_id") or "").strip()

    analysis = PENDING_PREVIEWS.pop(preview_id, None)
    if not analysis:
        return jsonify({"error": "This analysis has expired or was already submitted. Please run it again."}), 400

    case_id = database.save_full_case(
        analysis, channel="web", citizen_id=session["citizen_id"]
    )
    record = database.get_case(case_id)
    database.publish_case(record)

    return jsonify(record)


@app.route("/api/my-cases", methods=["GET"])
@citizen_required
def api_my_cases():
    return jsonify(database.get_cases_for_citizen(session["citizen_id"]))


@app.route("/api/my-cases/<case_id>/follow-ups", methods=["GET"])
@citizen_required
def api_my_case_follow_ups(case_id):
    record = database.get_case(case_id)
    if not record or record.get("citizen_id") != session["citizen_id"]:
        return jsonify({"error": "Case not found."}), 404
    return jsonify(record.get("follow_ups", []))


@app.route("/api/my-cases/<case_id>", methods=["DELETE"])
@citizen_required
def api_delete_my_case(case_id):
    document_paths = database.delete_case_for_citizen(case_id, session["citizen_id"])
    if document_paths is None:
        return jsonify({"error": "Case not found."}), 404
    for filename in document_paths:
        safe_path = FOLLOW_UP_UPLOAD_DIR / Path(filename).name
        try:
            safe_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove follow-up document %s", safe_path)
    return jsonify({"ok": True})


def _distance_km(lat1, lng1, lat2, lng2):
    """Great-circle distance used as a walking-distance estimate."""
    radius_km = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi, delta_lambda = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@app.route("/api/support-locator", methods=["POST"])
@citizen_required
def api_support_locator():
    postal_code = str((request.get_json(silent=True) or {}).get("postal_code") or "").strip()
    if not re.fullmatch(r"\d{6}", postal_code):
        return jsonify({"error": "Enter a valid 6-digit Singapore postal code."}), 400
    if not ONEMAP_API_TOKEN:
        return jsonify({"error": "The locator needs an ONEMAP_API_TOKEN configured on the server."}), 503

    query = urlencode({"searchVal": postal_code, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": 1})
    try:
        one_map_request = Request(
            f"https://www.onemap.gov.sg/api/common/elastic/search?{query}",
            headers={"Authorization": ONEMAP_API_TOKEN},
        )
        with urlopen(one_map_request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        match = (result.get("results") or [None])[0]
        if not match or result.get("error"):
            return jsonify({"error": "We could not locate that postal code. Please check it and try again."}), 404
        lat, lng = float(match["LATITUDE"]), float(match["LONGITUDE"])
    except Exception:
        logger.exception("OneMap postal lookup failed")
        return jsonify({"error": "The live postal lookup is unavailable right now. Please try again shortly."}), 502

    locations = []
    for location in SUPPORT_LOCATIONS:
        straight_line_km = _distance_km(lat, lng, location["lat"], location["lng"])
        item = {**location, "walking_km": round(straight_line_km * 1.25, 1)}
        locations.append(item)
    locations.sort(key=lambda item: item["walking_km"])

    return jsonify({
        "origin": {"postal_code": postal_code, "address": match.get("ADDRESS", postal_code), "lat": lat, "lng": lng},
        "locations": locations[:6],
    })


@app.route("/api/cases", methods=["GET"])
@worker_required
def api_cases():
    """Returns cases visible to the current worker: unclaimed cases (open to
    everyone) plus cases already claimed by this worker specifically. Once
    another worker accepts a case, it stops appearing here. Reviewed cases
    are excluded — see /api/cases/reviewed for those."""
    limit = request.args.get("limit", default=20, type=int)
    return jsonify(database.get_recent_cases_for_worker(session["worker_id"], limit=limit))


@app.route("/api/cases/reviewed", methods=["GET"])
@worker_required
def api_reviewed_cases():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify(database.get_reviewed_cases_for_worker(session["worker_id"], limit=limit))


@app.route("/api/cases/<case_id>", methods=["GET"])
@worker_required
def api_case_detail(case_id):
    record = database.get_case(case_id)
    if not record:
        return jsonify({"error": "Case not found."}), 404
    return jsonify(record)


@app.route("/api/cases/<case_id>/status", methods=["POST"])
@worker_required
def api_update_case_status(case_id):
    payload = request.get_json(silent=True) or {}
    new_status = (payload.get("status") or "").strip()
    actor = session.get("display_name", "case_worker")

    if not new_status:
        return jsonify({"error": "status is required."}), 400

    record = database.update_case_status(case_id, new_status, actor)
    if not record:
        return jsonify({"error": "Case not found."}), 404

    database.publish_case(record)
    bot_service.notify_citizen_of_case_update(record, "status")
    return jsonify(record)


@app.route("/api/cases/<case_id>/refer", methods=["POST"])
@worker_required
def api_refer_case(case_id):
    """Hands off a case to another case worker who's already in this
    worker's colleague directory (see /api/worker/colleagues)."""
    payload = request.get_json(silent=True) or {}
    colleague_id = (payload.get("colleague_id") or "").strip()
    actor = session.get("display_name", "case_worker")

    if not colleague_id:
        return jsonify({"error": "colleague_id is required."}), 400

    colleagues = database.get_colleagues(session["worker_id"])
    if not any(c["id"] == colleague_id for c in colleagues):
        return jsonify({"error": "That case worker isn't in your colleague list."}), 403

    try:
        record = database.refer_case(case_id, colleague_id, actor)
    except database.WorkerNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    if not record:
        return jsonify({"error": "Case not found."}), 404

    database.publish_case(record)
    return jsonify(record)


@app.route("/api/cases/<case_id>/accept", methods=["POST"])
@worker_required
def api_accept_case(case_id):
    """Claims an unassigned case for the current worker. Once claimed, the
    case disappears from every other worker's live feed (broadcast via SSE
    so connected dashboards can drop it immediately)."""
    actor = session.get("display_name", "case_worker")

    try:
        record = database.accept_case(case_id, session["worker_id"], actor)
    except database.CaseAlreadyClaimedError as exc:
        return jsonify({"error": str(exc)}), 409
    if not record:
        return jsonify({"error": "Case not found."}), 404

    database.publish_case(record)
    return jsonify(record)


@app.route("/api/cases/<case_id>/follow-ups", methods=["POST"])
@worker_required
def api_send_follow_up(case_id):
    note = (request.form.get("note") or "").strip()
    uploaded = request.files.get("document")

    if not note and not (uploaded and uploaded.filename):
        return jsonify({"error": "Add a follow-up note or document."}), 400

    document = None
    document_file_path = None
    if uploaded and uploaded.filename:
        filename = secure_filename(uploaded.filename)
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if not filename or extension not in ALLOWED_FOLLOW_UP_EXTENSIONS:
            return jsonify({"error": "Document must be a PDF, DOC, DOCX, or TXT file."}), 400
        stored_name = f"{uuid.uuid4().hex}.{extension}"
        document_file_path = FOLLOW_UP_UPLOAD_DIR / stored_name
        uploaded.save(document_file_path)
        document = {"name": filename, "path": stored_name, "mime": uploaded.mimetype or "application/octet-stream"}

    try:
        record = database.create_case_follow_up(case_id, session["worker_id"], note, document, [])
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    if not record:
        return jsonify({"error": "Case not found."}), 404

    database.publish_case(record)
    bot_service.notify_citizen_of_case_update(record, "follow_up", document_file_path=document_file_path)
    return jsonify(record)


@app.route("/api/follow-ups/<int:follow_up_id>/document", methods=["GET"])
def api_download_follow_up_document(follow_up_id):
    follow_up = database.get_follow_up_for_download(follow_up_id)
    if not follow_up:
        return jsonify({"error": "Document not found."}), 404
    if session.get("role") == "citizen" and follow_up["citizen_id"] != session.get("citizen_id"):
        return jsonify({"error": "Document not found."}), 404
    if session.get("role") == "worker" and follow_up["worker_id"] != session.get("worker_id"):
        return jsonify({"error": "Document not found."}), 404
    if session.get("role") not in {"citizen", "worker"}:
        return jsonify({"error": "Please sign in first."}), 401
    return send_from_directory(FOLLOW_UP_UPLOAD_DIR, follow_up["document_path"], as_attachment=True, download_name=follow_up["document_name"])


@app.route("/api/worker/me", methods=["GET"])
@worker_required
def api_worker_me():
    worker = database.get_worker_by_id(session["worker_id"])
    if not worker:
        return jsonify({"error": "Worker not found."}), 404
    return jsonify(worker)


@app.route("/api/worker/colleagues", methods=["GET"])
@worker_required
def api_get_colleagues():
    return jsonify(database.get_colleagues(session["worker_id"]))


@app.route("/api/worker/colleagues", methods=["POST"])
@worker_required
def api_send_colleague_request():
    """Sends a colleague request to another case worker by worker_code. The
    recipient must accept it (see /api/worker/requests/<id>/accept) before
    either side can refer cases to the other."""
    payload = request.get_json(silent=True) or {}
    worker_code = (payload.get("worker_code") or "").strip()

    if not worker_code:
        return jsonify({"error": "worker_code is required."}), 400

    try:
        result = database.send_colleague_request(session["worker_id"], worker_code)
    except database.WorkerNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except database.ColleagueAlreadyAddedError as exc:
        return jsonify({"error": str(exc)}), 409
    except database.RequestAlreadyPendingError as exc:
        return jsonify({"error": str(exc)}), 409

    return jsonify(result)


@app.route("/api/worker/colleagues/<colleague_id>", methods=["DELETE"])
@worker_required
def api_remove_colleague(colleague_id):
    database.remove_colleague(session["worker_id"], colleague_id)
    return jsonify({"ok": True})


@app.route("/api/worker/requests", methods=["GET"])
@worker_required
def api_get_incoming_requests():
    return jsonify(database.get_incoming_requests(session["worker_id"]))


@app.route("/api/worker/requests/sent", methods=["GET"])
@worker_required
def api_get_outgoing_requests():
    return jsonify(database.get_outgoing_requests(session["worker_id"]))


@app.route("/api/worker/requests/<int:request_id>/accept", methods=["POST"])
@worker_required
def api_accept_colleague_request(request_id):
    result = database.respond_to_colleague_request(session["worker_id"], request_id, accept=True)
    if not result:
        return jsonify({"error": "Request not found or already resolved."}), 404
    return jsonify(result)


@app.route("/api/worker/requests/<int:request_id>/deny", methods=["POST"])
@worker_required
def api_deny_colleague_request(request_id):
    result = database.respond_to_colleague_request(session["worker_id"], request_id, accept=False)
    if not result:
        return jsonify({"error": "Request not found or already resolved."}), 404
    return jsonify(result)


# ---------------------------------------------------------------------------
# Server-Sent Events stream — pushes new/updated cases to the live worker dashboard
# ---------------------------------------------------------------------------
@app.route("/api/stream")
@worker_required
def api_stream():
    def event_generator():
        q = database.subscribe()
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    record = q.get(timeout=15)
                    yield f"data: {json.dumps(record)}\n\n"
                except Exception:
                    # periodic heartbeat comment keeps proxies/browsers from timing out
                    yield ": heartbeat\n\n"
        finally:
            database.unsubscribe(q)

    return Response(
        stream_with_context(event_generator()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Telegram webhook (production mode only)
# ---------------------------------------------------------------------------
@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    update_dict = request.get_json(force=True, silent=True) or {}
    bot_service.submit_webhook_update(update_dict)
    return "", 200


# ---------------------------------------------------------------------------
# Bot bootstrap
# ---------------------------------------------------------------------------
def _start_telegram_bot():
    if not TELEGRAM_BOT_TOKEN:
        logger.info("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled (web UI still fully functional).")
        return

    if TELEGRAM_MODE == "webhook":
        if not WEBHOOK_URL:
            logger.warning("TELEGRAM_MODE=webhook but WEBHOOK_URL is not set — falling back to polling.")
            bot_service.start_polling_in_background(TELEGRAM_BOT_TOKEN)
        else:
            bot_service.start_webhook_mode(TELEGRAM_BOT_TOKEN, WEBHOOK_URL)
    else:
        bot_service.start_polling_in_background(TELEGRAM_BOT_TOKEN)


_start_telegram_bot()


if __name__ == "__main__":
    # use_reloader=False is required: Flask's debug reloader spawns a second
    # process which would start a second Telegram polling loop and crash with
    # a "terminated by other getUpdates request" conflict.
    app.run(debug=True, use_reloader=False, threaded=True, host="0.0.0.0", port=5000)
