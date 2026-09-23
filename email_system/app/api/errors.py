"""Map pipeline/workflow errors to HTTP responses (no stack traces, no email content)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm.exc import StaleDataError

from app.core.errors import ErrorCode, PipelineError

_STATUS: dict[ErrorCode, int] = {
    ErrorCode.LLM_UNAVAILABLE: 503,
    ErrorCode.LLM_TIMEOUT: 504,
    ErrorCode.LLM_SCHEMA_INVALID: 502,
    ErrorCode.ATTACHMENT_PARSE_FAILED: 422,
    ErrorCode.ATTACHMENT_TOO_LARGE: 422,
    ErrorCode.OCR_UNAVAILABLE: 503,
    ErrorCode.GMAIL_AUTH_FAILED: 502,
    ErrorCode.GMAIL_FETCH_FAILED: 502,
    ErrorCode.SEND_DISABLED: 403,
    ErrorCode.SEND_FAILED: 502,
    ErrorCode.SEND_DELIVERY_UNKNOWN: 409,
    ErrorCode.INVALID_RECIPIENT: 422,
    ErrorCode.INVALID_ADMIN: 400,
}  # everything else (workflow conflicts) -> 409


def install(app: FastAPI) -> None:
    @app.exception_handler(PipelineError)
    async def _pipeline(_: Request, exc: PipelineError) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS.get(exc.code, 409),
            content={
                "error": exc.code.value,
                "message": exc.policy.user_message,
                "detail": exc.detail,
                "retryable": exc.policy.retryable,
            },
        )

    @app.exception_handler(StaleDataError)
    async def _stale(_: Request, __: StaleDataError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": "CONCURRENT_UPDATE",
                "message": "The email was changed by someone else. Reload and retry.",
                "detail": "",
                "retryable": False,
            },
        )
