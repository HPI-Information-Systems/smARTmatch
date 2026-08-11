"""Match detail route."""

from flask import abort, render_template, request

from .. import app as app_module


@app_module.app.route("/match/<match_id>")
def match(match_id):
    sort = request.args.get("sort")
    rating = request.args.get("rating")
    bookmarked = request.args.get("bookmarked")
    search = app_module._clean_search_value(request.args.get("search"))
    image_weight = app_module.normalize_image_weight(request.args.get("image_weight"))

    match = app_module.get_match_by_id(match_id, image_weight=image_weight)
    previous_match, next_match = app_module.get_previous_and_next_match(
        match_id,
        rating=rating,
        sort=sort,
        bookmarked=bookmarked,
        search=search,
        image_weight=image_weight,
    )

    if match is not None:
        return render_template(
            "match.html",
            match=match,
            previous_match=previous_match,
            next_match=next_match,
            rating=rating,
            sort=sort,
            bookmarked=bookmarked,
            search=search,
            image_weight=image_weight,
        )
    abort(400)
