#!/usr/bin/env python3
"""
remoteauth.py -- a token for anything that is not this machine.

WHY
---
The dashboard can arm engines, transmit orders, flatten positions and delete
tickers, and it has never had any authentication because it only ever listened
on 127.0.0.1. The moment it binds to a network address that assumption is gone,
and on a network Windows itself classifies as Public it is gone in the worst
way: everyone else on that wifi gets a trading control panel.

WHAT THIS DOES
--------------
Requests from the loopback address are untouched -- working on the machine
itself behaves exactly as before, with no login and no friction. Every other
source must present the token, as `?k=<token>` once (after which it is stored
in a cookie) or as an `X-Dash-Key` header.

WHAT THIS IS NOT
----------------
This is not real security. The traffic is plain HTTP, so the token crosses the
network in clear text and anyone able to watch that network can lift it. It
raises the bar from "anyone who can reach the port" to "anyone who can read the
traffic", which is worth having for a laptop on the same wifi and is NOT worth
relying on for anything exposed to the internet. For that, put it behind a
tunnel that terminates TLS.

The token lives in state/dash_token.txt, and is generated once.
"""
from __future__ import annotations

import ipaddress
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / "state" / "dash_token.txt"
COOKIE = "dashkey"


def token() -> str:
    """The token, generated on first use and stable thereafter."""
    env = (os.environ.get("TICKAVERAGER_DASH_TOKEN") or "").strip()
    if env:
        return env
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    TOKEN_FILE.parent.mkdir(exist_ok=True)
    t = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    return t


def is_local(host: str) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "::1")


def install(app) -> str:
    """Add the middleware. Returns the token so the caller can print it."""
    from fastapi import Request
    from fastapi.responses import JSONResponse, PlainTextResponse

    tok = token()

    @app.middleware("http")
    async def _gate(request: Request, call_next):
        client = request.client.host if request.client else ""
        if is_local(client):
            return await call_next(request)

        given = (request.query_params.get("k")
                 or request.headers.get("x-dash-key")
                 or request.cookies.get(COOKIE) or "")
        if secrets.compare_digest(given, tok):
            resp = await call_next(request)
            # remember it so every later asset request does not need the query
            if request.query_params.get("k"):
                resp.set_cookie(COOKIE, tok, max_age=60 * 60 * 24 * 30,
                                httponly=True, samesite="lax")
            return resp

        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "a key is required"},
                                status_code=401)
        return PlainTextResponse(
            "This dashboard controls a live trading account and needs a key.\n\n"
            "Open it as:  http://<this-machine>:8010/?k=YOUR_KEY\n\n"
            "The key is printed by the server on startup and stored in\n"
            "state/dash_token.txt on the machine running it.\n",
            status_code=401)

    return tok
