"""Bookmark match action route."""

from flask import request

from .. import app as app_module
from . import _match_actions as actions


@app_module.app.route("/api/match/bookmark", methods=["POST"])
def api_match_bookmark():
    match_id = request.form.get("match-id")
    actions.update_match_bookmark(match_id, True)
    return (
        '<button class="btn btn-primary" type="button" '
        'hx-post="/api/match/unbookmark" hx-swap="outerHTML">'
        '<i class="bi bi-bookmark-fill me-1"></i> Gemerkt</button>'
    )
