from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from image_hub.config import settings
from image_hub.db import init_database
from image_hub.management import router as management_router
from image_hub.web import router
from image_hub.worker import generation_worker


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_directories()
    init_database()
    if settings.worker_enabled:
        generation_worker.start()
    try:
        yield
    finally:
        if settings.worker_enabled:
            generation_worker.stop()


app = FastAPI(title="AI Image Hub", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=settings.env == "production",
    max_age=60 * 60 * 12,
)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
app.include_router(management_router)
app.include_router(router)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
        "form-action 'self'"
    )
    if settings.env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response
