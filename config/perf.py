from __future__ import annotations

import time
from collections import deque

from django.conf import settings
from django.db import connections, reset_queries


_SLOW_REQUESTS: deque[tuple[float, str, int | str, int, float]] = deque(maxlen=20)


class PerformanceLogMiddleware:
    """Print lightweight request/DB timings for local demo diagnostics."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not getattr(settings, 'ENABLE_PERF_LOG', False):
            return self.get_response(request)

        previous_debug_cursor = {}
        for alias in connections:
            connection = connections[alias]
            previous_debug_cursor[alias] = connection.force_debug_cursor
            connection.force_debug_cursor = True
        reset_queries()

        started_at = time.perf_counter()
        response = None
        try:
            response = self.get_response(request)
            return response
        finally:
            total_ms = (time.perf_counter() - started_at) * 1000
            query_count = 0
            db_ms = 0.0
            for connection in connections.all():
                query_count += len(connection.queries)
                for query in connection.queries:
                    try:
                        db_ms += float(query.get('time', 0)) * 1000
                    except (TypeError, ValueError):
                        continue

            for alias in connections:
                connection = connections[alias]
                connection.force_debug_cursor = previous_debug_cursor.get(alias, False)

            status = getattr(response, 'status_code', 'error')
            path = request.get_full_path()
            redirect_to = ''
            if response is not None and 300 <= getattr(response, 'status_code', 0) < 400:
                redirect_to = f" redirect={response.get('Location', '')}"

            print(
                f"[PERF] {request.method} {path} status={status} "
                f"total={total_ms:.0f}ms queries={query_count} db={db_ms:.0f}ms{redirect_to}"
            )

            slow_threshold_ms = getattr(settings, 'PERF_SLOW_THRESHOLD_MS', 300)
            if total_ms >= slow_threshold_ms:
                _SLOW_REQUESTS.append((total_ms, path, status, query_count, db_ms))
                slow_rows = sorted(_SLOW_REQUESTS, reverse=True)[:5]
                summary = ' | '.join(
                    f"{row_path} {row_total:.0f}ms q={row_queries} db={row_db:.0f}ms"
                    for row_total, row_path, _row_status, row_queries, row_db in slow_rows
                )
                print(f"[PERF][SLOW_TOP] {summary}")
