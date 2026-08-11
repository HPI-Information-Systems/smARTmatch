"""Route that opens the first match needing review."""

from flask import redirect, url_for

from .. import app as app_module


@app_module.app.route("/match")
def match_redirect():
    match_page = app_module.get_matches_page(
        rating="unrated",
        sort="newest",
        bookmarked="false",
        image_weight=app_module.DEFAULT_IMAGE_WEIGHT,
        page=1,
        per_page=1,
    )
    if not match_page.matches:
        return redirect(url_for("match_list"))
    return redirect(
        url_for(
            "match",
            match_id=match_page.matches[0].match_id,
            rating="unrated",
            sort="newest",
            bookmarked="false",
            image_weight=app_module.DEFAULT_IMAGE_WEIGHT,
        )
    )
