"""Vercel entry point: runs ShiftTable as a live demo.

The database lives in /tmp and is re-created with the demo business whenever Vercel
starts a fresh copy of the function, so changes don't last. See README.md.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

server.serverless_setup()


class handler(server.Handler):
    pass
