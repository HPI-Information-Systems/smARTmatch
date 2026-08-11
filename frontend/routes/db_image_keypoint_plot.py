"""Keypoint plot image route."""

from flask import send_file

from .. import app as app_module


@app_module.app.route("/db_image/plot/<match_id>/keypoint")
def db_image_keypoint_plot(match_id):
    keypoint_plot = app_module.get_keypoint_plot_path(match_id)
    return send_file(keypoint_plot)
