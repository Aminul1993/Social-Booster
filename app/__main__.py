"""``python -m app`` - start the development server."""

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=not settings.is_production,
        proxy_headers=True,
        log_config=None,
    )


if __name__ == "__main__":
    main()
