"""Shared helpers for match review action routes."""

from flask import abort, redirect, request, url_for
from sqlalchemy import update

from .. import app as app_module


def filter_query_args_from_form():
    return {
        "rating": request.form.get("rating"),
        "sort": request.form.get("sort"),
        "bookmarked": request.form.get("bookmarked"),
        "search": app_module._clean_search_value(request.form.get("search")),
        "source_filter": app_module.normalize_source_filter(
            request.form.get("source_filter")
        ),
        "image_weight": str(
            app_module.normalize_image_weight(request.form.get("image_weight"))
        ),
    }


def clean_query_args(**values):
    return {key: value for key, value in values.items() if value not in (None, "")}


def safe_local_url(url):
    text = str(url or "")
    if text.startswith("/") and not text.startswith("//"):
        return text
    return None


def next_match_or_list_url(match_id, filters):
    next_match = app_module.get_next_match_to_label(match_id, **filters)
    query_args = clean_query_args(**filters)
    if next_match is not None:
        return url_for("match", match_id=next_match.match_id, **query_args)
    return url_for("match_list", **query_args)


def target_filter(match_id):
    pair = app_module.match_pair_from_values(
        request.form.get("lost-artwork-id"),
        request.form.get("auction-artwork-id"),
    )
    if pair is None:
        pair = app_module.match_pair_from_id(match_id)
    if pair is None:
        abort(400)
    lost_id, auction_id = pair
    return (
        app_module.MatchScore.lost_id == lost_id,
        app_module.MatchScore.auction_id == auction_id,
    )


def update_match_rating(match_id, rating):
    stmt = (
        update(app_module.MatchScore)
        .where(*target_filter(match_id))
        .values(rating=rating)
    )
    stmt.compile()

    with app_module.engine.connect() as conn:
        conn.execute(stmt)
        conn.commit()
    app_module.session.expire_all()


def update_match_bookmark(match_id, bookmarked):
    stmt = (
        update(app_module.MatchScore)
        .where(*target_filter(match_id))
        .values(bookmarked=bookmarked)
    )
    stmt.compile()

    with app_module.engine.connect() as conn:
        conn.execute(stmt)
        conn.commit()
    app_module.session.expire_all()


def redirect_response(url):
    if request.headers.get("HX-Request"):
        response = app_module.app.response_class("", status=204)
        response.headers["HX-Redirect"] = url
        return response
    return redirect(url)
