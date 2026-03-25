"""Flask-based batch review UI."""

import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

from .config import FLASK_PORT, PHOTOS_DIR
from .contacts_writer import apply_approved_matches
from .db import LinkedinMatch, db, get_pending_matches
from .image_processor import download_and_resize

# Templates are in the package's parent directory
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_STATIC_DIR = Path(__file__).parent.parent / "static"

app = Flask(
    __name__,
    template_folder=str(_TEMPLATES_DIR),
    static_folder=str(_STATIC_DIR),
)
app.config["SECRET_KEY"] = "linkedin-enricher-review"


@app.route("/photos/<path:filename>")
def serve_photo(filename):
    return send_from_directory(str(PHOTOS_DIR), filename)


@app.route("/")
def index():
    matches = list(get_pending_matches())

    # Pre-download photos for all pending matches so the UI can display them
    rows = []
    for m in matches:
        photo_file = None
        if m.photo_url and not m.photo_local:
            local = download_and_resize(m.photo_url, m.contact.id)
            if local:
                with db.atomic():
                    m.photo_local = str(local)
                    m.save()
                photo_file = local.name
        elif m.photo_local:
            p = Path(m.photo_local)
            photo_file = p.name if p.exists() else None

        rows.append(
            {
                "id": m.id,
                "contact_name": m.contact.full_name,
                "contact_org": m.contact.organization or "",
                "contact_email": m.contact.email or "",
                "linkedin_name": m.linkedin_name or "",
                "linkedin_url": m.linkedin_url or "",
                "headline": m.headline or "",
                "confidence": round(m.confidence, 2),
                "confidence_pct": int(m.confidence * 100),
                "photo_file": photo_file,
                # Default action: approve if confidence >= 0.75, else skip
                "default_action": "approved" if m.confidence >= 0.75 else "skipped",
            }
        )

    return render_template("review.html", rows=rows)


@app.route("/approve", methods=["POST"])
def approve():
    decisions = (
        request.json
    )  # [{id: int, action: "approved"|"rejected"|"skipped"}, ...]
    if not decisions:
        return jsonify({"error": "no decisions"}), 400

    counts = {"approved": 0, "rejected": 0, "skipped": 0}
    with db.atomic():
        for d in decisions:
            match_id = d.get("id")
            action = d.get("action")
            if action not in ("approved", "rejected", "skipped"):
                continue
            try:
                m = LinkedinMatch.get_by_id(match_id)
                m.status = action
                m.save()
                counts[action] += 1
            except LinkedinMatch.DoesNotExist:
                pass

    # Apply approved matches immediately
    applied = apply_approved_matches()
    counts["applied"] = applied
    counts["failed"] = counts["approved"] - applied

    return jsonify(counts)


@app.route("/done")
def done():
    return render_template(
        "done.html",
        applied=request.args.get("applied", 0),
        failed=request.args.get("failed", 0),
        rejected=request.args.get("rejected", 0),
        skipped=request.args.get("skipped", 0),
    )


def run_review_server(port: int = FLASK_PORT, open_browser: bool = True):
    """Start Flask review server and open browser."""
    url = f"http://127.0.0.1:{port}"
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Review UI at {url}  (Ctrl-C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
