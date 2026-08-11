"""Remove match bookmark action route."""

from flask import request

from .. import app as app_module
from . import _match_actions as actions


@app_module.app.route("/api/match/unbookmark", methods=["POST"])
def api_match_unbookmark():
    match_id = request.form.get("match-id")
    actions.update_match_bookmark(match_id, False)
    return (
        '<button class="btn btn-outline-primary" type="button" '
        'hx-post="/api/match/bookmark" hx-swap="outerHTML">'
        '<i class="bi bi-bookmark me-1"></i> Merken</button>'
    )
