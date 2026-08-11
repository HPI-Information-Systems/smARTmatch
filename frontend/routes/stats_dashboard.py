"""Statistics dashboard routes."""

from flask import render_template

from .. import app as app_module


@app_module.app.route("/stats")
@app_module.app.route("/stats/")
def stats_dashboard():
    return render_template(
        "stats.html", stats=app_module.get_dashboard_stats(app_module.engine)
    )
