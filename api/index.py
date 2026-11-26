"""Vercel serverless function: every request is routed here (see vercel.json)."""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pickhelper.web import handler as _ChampPicker  # noqa: E402


# Vercel looks for a top-level class named `handler` based on BaseHTTPRequestHandler,
# defined in this file itself.
class handler(_ChampPicker, BaseHTTPRequestHandler):
    pass
