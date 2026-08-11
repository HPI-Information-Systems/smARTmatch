"""Discard match action route."""

from flask import request

from .. import app as app_module
from . import _match_actions as actions


@app_module.app.route("/api/match/discard", methods=["POST"])
def api_match_discard():
    match_id = request.form.get("match-id")
    filters = actions.filter_query_args_from_form()
    return_url = actions.safe_local_url(request.form.get("return-url"))
    redirect_url = return_url or actions.next_match_or_list_url(match_id, filters)
    actions.update_match_rating(match_id, -1)
    return actions.redirect_response(redirect_url)
