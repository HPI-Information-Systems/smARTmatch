"""Image file route for DB-backed gallery and visualization images."""

from flask import send_file

from .. import app as app_module


@app_module.app.route("/db_image/file/<int:image_file_id>")
def db_image_file(image_file_id):
    image_path = app_module.get_image_file_path(image_file_id)
    return send_file(image_path)
