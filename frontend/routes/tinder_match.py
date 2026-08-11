"""Tinder-style match review route."""

from flask import abort, render_template, url_for

from .. import app as app_module


@app_module.app.route("/tinder/match/<match_id>")
def tinder_match(match_id):
    match = app_module.get_match_by_id(match_id)
    if match is None:
        abort(404)
    next_match = app_module.get_next_match_to_label(
        match_id,
        rating="unrated",
        sort="newest",
        bookmarked="false",
    )
    next_url = (
        url_for("tinder_match", match_id=next_match.match_id)
        if next_match
        else url_for("tinder_index")
    )
    return render_template(
        "tinder_swipe.html", match=match, next_match=next_match, next_url=next_url
    )
