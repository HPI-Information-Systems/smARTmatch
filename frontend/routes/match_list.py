"""Match list route."""

from flask import render_template, request

from .. import app as app_module


_REVIEWED_CATEGORY_RATINGS = {"accepted", "discarded"}


def _default_sort_for_category(rating, bookmarked):
    if bookmarked == "true" or rating in _REVIEWED_CATEGORY_RATINGS:
        return "newest"
    return "similarity"


@app_module.app.route("/list")
def match_list():
    page = request.args.get("page")
    search = app_module._clean_search_value(request.args.get("search"))
    rating = request.args.get("rating")
    sort = request.args.get("sort")
    bookmarked = request.args.get("bookmarked")
    image_weight = app_module.normalize_image_weight(request.args.get("image_weight"))

    if search:
        rating = rating or "all"
        sort = sort or "similarity"
        bookmarked = bookmarked or "all"
    elif rating is None and sort is None and bookmarked is None:
        rating = "unrated"
        sort = "similarity"
        bookmarked = "false"
    else:
        sort = sort or _default_sort_for_category(rating, bookmarked)

    match_page = app_module.get_matches_page(
        rating=rating,
        sort=sort,
        bookmarked=bookmarked,
        search=search,
        image_weight=image_weight,
        page=page,
        per_page=20,
    )

    return render_template(
        "list.html",
        matches=match_page.matches,
        total_matches=match_page.total_matches,
        page=match_page.page,
        max_page=match_page.max_page,
        rating=rating,
        sort=sort,
        bookmarked=bookmarked,
        search=search,
        search_active=bool(search),
        image_weight=image_weight,
        directory_counts=app_module.get_directory_counts(),
    )
