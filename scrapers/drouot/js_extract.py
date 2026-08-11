from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional


class DrouotJsExtractMixin:
    def _extract_js_object(self, html: str, marker: str) -> str:
        marker_idx = html.find(marker)
        if marker_idx == -1:
            return ""

        start = html.find("{", marker_idx)
        if start == -1:
            return ""

        depth = 0
        in_string = False
        escape = False

        for idx in range(start, len(html)):
            ch = html[idx]

            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
                continue

            if ch == "{":
                depth += 1
                continue

            if ch == "}":
                depth -= 1
                if depth == 0:
                    return html[start : idx + 1]

        return ""

    def _extract_js_array_block(self, source: str, key: str) -> str:
        match = re.search(rf"\b{re.escape(key)}:\[", source)
        if not match:
            return ""

        start = match.end() - 1
        depth = 0
        in_string = False
        escape = False

        for idx in range(start, len(source)):
            ch = source[idx]

            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
                continue

            if ch == "[":
                depth += 1
                continue

            if ch == "]":
                depth -= 1
                if depth == 0:
                    return source[start : idx + 1]

        return ""

    def _extract_js_string(self, source: str, key: str, *, collapse_whitespace: bool = True) -> Optional[str]:
        if not source:
            return None

        match = re.search(rf'\b{re.escape(key)}:"((?:\\.|[^"\\])*)"', source)
        if not match:
            return None

        raw = match.group(1)
        try:
            value = json.loads(f'"{raw}"')
        except Exception:
            value = raw.encode("utf-8", errors="ignore").decode("unicode_escape")

        text = str(value)
        if collapse_whitespace:
            text = " ".join(text.split())
        else:
            text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
            text = re.sub(r"\n{3,}", "\n\n", text)

        return text or None

    def _extract_js_number(self, source: str, key: str) -> Optional[float]:
        if not source:
            return None

        match = re.search(rf"\b{re.escape(key)}:(-?\d+(?:\.\d+)?)", source)
        if not match:
            return None

        raw = match.group(1)
        try:
            return float(raw)
        except ValueError:
            return None

    def _extract_js_bool(self, source: str, key: str) -> Optional[bool]:
        if not source:
            return None

        match = re.search(rf"\b{re.escape(key)}:(true|false)", source)
        if not match:
            return None
        return match.group(1) == "true"

    def _extract_js_number_list(self, source: str, key: str) -> list[int]:
        block = self._extract_js_array_block(source, key)
        if not block:
            return []

        nums = re.findall(r"-?\d+", block)
        out: list[int] = []
        for num in nums:
            try:
                out.append(int(num))
            except ValueError:
                continue
        return out

    def _extract_epoch_key_as_iso(self, source: str, key: str) -> Optional[str]:
        ts = self._extract_js_number(source, key)
        if ts is None or ts <= 0:
            return None

        try:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.isoformat()
        except Exception:
            return None

    def _extract_js_number_as_str(self, source: str, key: str) -> Optional[str]:
        value = self._extract_js_number(source, key)
        if value is None:
            return None
        if value.is_integer():
            return str(int(value))
        return str(value)
