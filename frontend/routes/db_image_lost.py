"""Lost artwork image route."""

from flask import send_file

from .. import app as app_module


@app_module.app.route("/db_image/lost/<artwork_id>")
def db_image_lost(artwork_id):
    primary_image = app_module.get_primary_image_path(
        app_module.LostArtwork,
        app_module.LostArtwork.lost_artwork_id,
        artwork_id,
    )
    return send_file(primary_image)
