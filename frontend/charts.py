"""Small SVG chart data builders for the SmartMatch frontend."""

from __future__ import annotations

from math import floor, log10

CHART_WIDTH = 720
CHART_HEIGHT = 260
PLOT_LEFT = 48
PLOT_RIGHT = 700
PLOT_TOP = 18
PLOT_BOTTOM = 222


def _compact_number(value):
    number = int(round(value))
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.1f}M".replace(".0M", "M")
    if abs(number) >= 1_000:
        return f"{number / 1_000:.1f}k".replace(".0k", "k")
    return str(number)


def _nice_max(value):
    if value <= 0:
        return 1
    magnitude = 10 ** floor(log10(value))
    for step in (1, 2, 5, 10):
        candidate = step * magnitude
        if candidate >= value:
            return candidate
    return 10 * magnitude


def _label_indices(count, maximum=6):
    if count <= 0:
        return []
    if count <= maximum:
        return list(range(count))
    step = (count - 1) / (maximum - 1)
    return sorted({round(index * step) for index in range(maximum)})


def _point(index, count, value, max_value):
    plot_width = PLOT_RIGHT - PLOT_LEFT
    plot_height = PLOT_BOTTOM - PLOT_TOP
    x = PLOT_LEFT + (plot_width / (count - 1) * index if count > 1 else plot_width / 2)
    y = PLOT_BOTTOM - (float(value) / max_value * plot_height)
    return round(x, 2), round(y, 2)


def _time_meta(time_labels, index):
    if not time_labels or index >= len(time_labels) or not time_labels[index]:
        return {}
    item = time_labels[index]
    return {
        "time_iso": item.get("iso"),
        "time_format": item.get("format"),
    }


def make_line_chart(labels, series, time_labels=None):
    """Return SVG geometry for one or more count series over shared labels."""
    normalized = []
    all_values = []
    point_count = len(labels)
    for item in series:
        values = [int(value or 0) for value in item["values"]]
        values = (values + [0] * point_count)[:point_count]
        projected_value = item.get("projected_value")
        if projected_value is not None and point_count > 0:
            projected_value = int(round(float(projected_value)))
            all_values.append(projected_value)
        normalized.append(
            {**item, "values": values, "projected_value": projected_value}
        )
        all_values.extend(values)

    max_value = _nice_max(max(all_values, default=0))
    x_labels = [
        {
            "label": labels[index],
            "x": _point(index, point_count, 0, max_value)[0],
            **_time_meta(time_labels, index),
        }
        for index in _label_indices(point_count)
    ]
    y_ticks = []
    for fraction in (0, 0.25, 0.5, 0.75, 1):
        value = max_value * fraction
        y_ticks.append(
            {
                "value": int(round(value)),
                "label": _compact_number(value),
                "y": _point(0, max(point_count, 1), value, max_value)[1],
            }
        )

    chart_series = []
    for item in normalized:
        data = []
        projection_index = (
            point_count - 1 if item["projected_value"] is not None else None
        )
        for index, actual_value in enumerate(item["values"]):
            is_projection = index == projection_index
            value = item["projected_value"] if is_projection else actual_value
            x, y = _point(index, point_count, value, max_value)
            value_label = str(value)
            if is_projection:
                value_label = f"{value} (Hochrechnung; bisher {actual_value})"
            data.append(
                {
                    "x": x,
                    "y": y,
                    "label": labels[index],
                    "value": value,
                    "value_label": value_label,
                    "is_projection": is_projection,
                    **_time_meta(time_labels, index),
                }
            )

        solid_data = data if projection_index is None else data[:projection_index]
        point_strings = [f'{point["x"]},{point["y"]}' for point in solid_data]
        projection_data = []
        if projection_index is not None:
            projection_data = data[max(0, projection_index - 1) : projection_index + 1]
        projection_points = " ".join(
            f'{point["x"]},{point["y"]}' for point in projection_data
        )

        area_points = ""
        if point_strings:
            first_x = solid_data[0]["x"]
            last_x = solid_data[-1]["x"]
            area_points = f"{first_x},{PLOT_BOTTOM} {' '.join(point_strings)} {last_x},{PLOT_BOTTOM}"
        chart_series.append(
            {
                **item,
                "data": data,
                "points": " ".join(point_strings),
                "projection_points": projection_points,
                "area_points": area_points,
            }
        )

    return {
        "view_box": f"0 0 {CHART_WIDTH} {CHART_HEIGHT}",
        "point_count": point_count,
        "show_points": point_count <= 120,
        "plot_left": PLOT_LEFT,
        "plot_right": PLOT_RIGHT,
        "plot_bottom": PLOT_BOTTOM,
        "x_labels": x_labels,
        "y_ticks": y_ticks,
        "series": chart_series,
    }


def make_bar_chart(items):
    """Return percentages for horizontal bar charts."""
    max_value = max([int(item["value"] or 0) for item in items] + [1])
    return [
        {
            **item,
            "value": int(item["value"] or 0),
            "percent": round(int(item["value"] or 0) / max_value * 100, 2),
        }
        for item in items
    ]
