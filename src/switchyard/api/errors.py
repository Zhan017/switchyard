"""Errors in OpenAI's shape, so OpenAI clients raise them the usual way."""

from __future__ import annotations

import math

from fastapi.responses import JSONResponse


def error_response(
    status: int,
    message: str,
    code: str,
    *,
    error_type: str = "switchyard_error",
    param: str | None = None,
    retry_after: float | None = None,
) -> JSONResponse:
    headers = {}
    if retry_after is not None:
        headers["retry-after"] = str(max(1, math.ceil(retry_after)))
    return JSONResponse(
        {"error": {"message": message, "type": error_type, "param": param, "code": code}},
        status_code=status,
        headers=headers,
    )
