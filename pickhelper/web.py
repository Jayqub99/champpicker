"""Entry point for Vercel: builds the app once per warm instance."""
from __future__ import annotations

import threading

from . import config, ddragon
from .server import App, make_handler

_app: App | None = None
_lock = threading.Lock()


def get_app() -> App:
    global _app
    with _lock:
        if _app is None:
            cfg = config.load()
            if not cfg.database_url:
                raise RuntimeError("DATABASE_URL isn't set. Add the Neon database to the Vercel project.")
            _app = App(cfg, ddragon.load(config.DATA_DIR), watching=False)
        return _app


handler = make_handler(get_app)
