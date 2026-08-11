"""Auction artwork image route."""

from flask import send_file

from .. import app as app_module


@app_module.app.route("/db_image/auction/<artwork_id>")
def db_image_auction(artwork_id):
    primary_image = app_module.get_primary_image_path(
        app_module.AuctionArtwork,
        app_module.AuctionArtwork.auction_artwork_id,
        artwork_id,
    )
    return send_file(primary_image)
