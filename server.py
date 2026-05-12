#!/usr/bin/env python3
"""TabletBridge – turns your phone into a wireless graphics tablet."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import pathlib
import struct
import uuid
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

import qrcode
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
    PYAUTOGUI_OK = True
except Exception:
    PYAUTOGUI_OK = False

# ── paths ──────────────────────────────────────────────────────────────────────
BASE   = pathlib.Path(__file__).parent
STATIC = BASE / "static"

# ── constants ──────────────────────────────────────────────────────────────────
PORT        = int(os.environ.get("PORT", 8765))
EVT_HEARTBEAT = 0
EVT_DOWN      = 1
EVT_MOVE      = 2
EVT_UP        = 3
EVT_HOVER     = 4


# ── session ────────────────────────────────────────────────────────────────────
class Session:
    __slots__ = ("id", "phone_ws", "desktop_ws", "injector_ws", "inject", "mon_w", "mon_h")

    def __init__(self, sid: str) -> None:
        self.id            = sid
        self.phone_ws:    Optional[WebSocket] = None
        self.desktop_ws:  Optional[WebSocket] = None
        self.injector_ws: Optional[WebSocket] = None
        self.inject        = False
        self.mon_w         = 1920
        self.mon_h         = 1080


_sessions: dict[str, Session] = {}


# ── helpers ────────────────────────────────────────────────────────────────────
def _qr_data_url(url: str) -> str:
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0A0A16", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _parse_event(data: bytes) -> Optional[dict]:
    if len(data) < 21:
        return None
    t, x, y, pressure, tilt_x, tilt_y = struct.unpack_from("<Bfffff", data)
    return dict(type=t, x=x, y=y, pressure=pressure, tiltX=tilt_x, tiltY=tilt_y)


def _inject(s: Session, evt: dict) -> None:
    if not PYAUTOGUI_OK or not s.inject:
        return
    px = int(max(0.0, min(evt["x"], 1.0)) * (s.mon_w - 1))
    py = int(max(0.0, min(evt["y"], 1.0)) * (s.mon_h - 1))
    t = evt["type"]
    try:
        if t in (EVT_MOVE, EVT_HOVER):
            pyautogui.moveTo(px, py)
        elif t == EVT_DOWN:
            pyautogui.mouseDown(px, py, button="left")
        elif t == EVT_UP:
            pyautogui.mouseUp(px, py, button="left")
    except Exception:
        pass


async def _send_bytes(ws: Optional[WebSocket], data: bytes) -> bool:
    if ws is None:
        return False
    try:
        await ws.send_bytes(data)
        return True
    except Exception:
        return False


async def _send_text(ws: Optional[WebSocket], text: str) -> bool:
    if ws is None:
        return False
    try:
        await ws.send_text(text)
        return True
    except Exception:
        return False


# ── icon generation ────────────────────────────────────────────────────────────
def _make_icons() -> None:
    try:
        from PIL import Image, ImageDraw
        icons_dir = STATIC / "icons"
        icons_dir.mkdir(parents=True, exist_ok=True)
        for size in (192, 512):
            path = icons_dir / f"icon-{size}.png"
            if path.exists():
                continue
            img = Image.new("RGB", (size, size), "#0A0A16")
            d = ImageDraw.Draw(img)
            pad = size // 6
            d.ellipse([pad, pad, size - pad, size - pad], fill="#00E5CC")
            pad2 = size // 3
            d.ellipse([pad2, pad2, size - pad2, size - pad2], fill="#0A0A16")
            img.save(path, "PNG")
    except Exception as e:
        print(f"[warn] Could not generate icons: {e}")


# ── app ────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def _lifespan(app: FastAPI):
    _make_icons()
    print(f"\n  TabletBridge running on port {PORT}\n")
    yield


app = FastAPI(lifespan=_lifespan)


# ── HTTP API ───────────────────────────────────────────────────────────────────
@app.post("/api/sessions")
async def create_session(request: Request):
    sid = uuid.uuid4().hex[:12]
    _sessions[sid] = Session(sid)

    # Derive the public base URL from request headers (works locally + behind Railway/Render proxy)
    proto = request.headers.get("x-forwarded-proto") or "http"
    host  = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or f"localhost:{PORT}"
    )
    base    = f"{proto}://{host}"
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    pair_url = f"{base}/phone?session={sid}&wsBase={quote(ws_base, safe='')}"

    return {
        "sessionId": sid,
        "pairUrl":   pair_url,
        "wsBase":    ws_base,
        "qrCode":    _qr_data_url(pair_url),
        "pyautogui": PYAUTOGUI_OK,
    }


@app.post("/api/sessions/{sid}/inject")
async def toggle_inject(sid: str, enabled: bool = Query(...)):
    s = _sessions.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    s.inject = enabled
    return {"ok": True, "pyautogui": PYAUTOGUI_OK}


@app.post("/api/sessions/{sid}/monitor")
async def set_monitor(
    sid: str,
    width:  int = Query(1920),
    height: int = Query(1080),
):
    s = _sessions.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    s.mon_w, s.mon_h = width, height
    return {"ok": True}


# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_handler(
    ws:      WebSocket,
    session: str = Query(...),
    role:    str = Query(...),
):
    await ws.accept()
    s = _sessions.get(session)
    if not s:
        await ws.close(code=1008)
        return

    if role == "phone":
        if s.phone_ws:
            try:
                await s.phone_ws.close()
            except Exception:
                pass
        s.phone_ws = ws
        await _send_text(s.desktop_ws, '{"type":"phone-connected"}')

        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                raw: bytes = msg.get("bytes") or b""
                if not raw:
                    continue

                if raw[0] == EVT_HEARTBEAT:
                    await ws.send_bytes(raw)
                    continue

                evt = _parse_event(raw)
                if evt:
                    _inject(s, evt)
                    await _send_bytes(s.desktop_ws, raw)
                    await _send_bytes(s.injector_ws, raw)
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            if s.phone_ws is ws:
                s.phone_ws = None
            await _send_text(s.desktop_ws, '{"type":"phone-disconnected"}')

    elif role == "desktop":
        if s.desktop_ws:
            try:
                await s.desktop_ws.close()
            except Exception:
                pass
        s.desktop_ws = ws

        if s.phone_ws:
            await _send_text(ws, '{"type":"phone-connected"}')

        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            if s.desktop_ws is ws:
                s.desktop_ws = None

    elif role == "injector":
        # Local cursor-injection agent — receives events, does not displace the browser
        if s.injector_ws:
            try:
                await s.injector_ws.close()
            except Exception:
                pass
        s.injector_ws = ws
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            if s.injector_ws is ws:
                s.injector_ws = None

    else:
        await ws.close(code=1008)


# ── static files ───────────────────────────────────────────────────────────────
@app.get("/phone")
async def phone_page():
    return FileResponse(STATIC / "phone.html")


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


# ── entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
