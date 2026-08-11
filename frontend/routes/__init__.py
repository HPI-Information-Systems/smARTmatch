"""Route registration for the SmartMatch frontend."""

from importlib import import_module

_ROUTE_MODULES = (
    "home",
    "stats_dashboard",
    "match_list",
    "match_redirect",
    "match_detail",
    "db_image_auction",
    "db_image_lost",
    "db_image_file",
    "db_image_keypoint_plot",
    "api_match_accept",
    "api_match_discard",
    "api_match_reset",
    "api_match_bookmark",
    "api_match_unbookmark",
    "tinder_index",
    "tinder_match",
)


def register_routes():
    for module_name in _ROUTE_MODULES:
        import_module(f"{__name__}.{module_name}")
