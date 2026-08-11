"""Reset match rating action route."""

from flask import request

from .. import app as app_module
from . import _match_actions as actions


@app_module.app.route("/api/match/reset-rating", methods=["POST"])
def api_match_reset():
    match_id = request.form.get("match-id")
    actions.update_match_rating(match_id, 0)

    return (
        '<button class="btn btn-danger match-rating-action" type="button" '
        'hx-post="/api/match/discard" hx-target="#match-rating-buttons" '
        'hx-swap="innerHTML">Verwerfen</button>'
        '<button class="btn btn-success match-rating-action" type="button" '
        'hx-post="/api/match/accept" hx-target="#match-rating-buttons" '
        'hx-swap="innerHTML">Akzeptieren</button>'
    )
