"""Vercel serverless function: every request is routed here (see vercel.json)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pickhelper.web import handler  # noqa: E402,F401  (Vercel looks for `handler`)
