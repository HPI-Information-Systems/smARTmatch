"""Home page route."""

from flask import render_template

from .. import app as app_module


@app_module.app.route("/")
def home():
    match_count = app_module.get_match_count(rating="unrated", bookmarked="false")
    auction_image_previews = app_module.get_top_unlabeled_auction_previews()
    return render_template(
        "index.html",
        match_count=match_count,
        auction_image_previews=auction_image_previews,
    )
