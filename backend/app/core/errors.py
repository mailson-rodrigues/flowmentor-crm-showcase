from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("flowmentor")


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        self.status_code, self.code, self.message, self.details = status_code, code, message, details or {}
        super().__init__(message)


def not_found(what: str = "Recurso") -> AppError:
    return AppError(404, "not_found", f"{what} não encontrado")


def forbidden(message: str = "Sem permissão para esta ação") -> AppError:
    return AppError(403, "forbidden", message)


def bad_request(message: str, code: str = "bad_request", **details) -> AppError:
    return AppError(400, code, message, details)


def conflict(message: str, code: str = "conflict") -> AppError:
    return AppError(409, code, message)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, "details": exc.details}}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        errs = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"error": {"code": "validation_error", "message": "Dados inválidos", "details": {"fields": errs}}}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("erro não tratado em %s %s", request.method, request.url.path)
        return JSONResponse({"error": {"code": "internal_error", "message": "Erro interno do servidor"}}, status_code=500)
