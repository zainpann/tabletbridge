#!/usr/bin/env python3
"""TabletBridge local agent — connects to the cloud relay and injects cursor events via pyautogui."""

import asyncio
import struct
import sys

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
except ImportError:
    print("pyautogui not installed.  Run:  pip install pyautogui")
    sys.exit(1)

try:
    import websockets
except ImportError:
    print("websockets not installed.  Run:  pip install websockets")
    sys.exit(1)

EVT_DOWN  = 1
EVT_MOVE  = 2
EVT_UP    = 3
EVT_HOVER = 4


async def run(relay: str, session: str) -> None:
    mon_w, mon_h = pyautogui.size()
    uri = f"{relay.rstrip('/')}/ws?session={session}&role=injector"
    print(f"Connecting to {uri}")
    print(f"Screen: {mon_w}x{mon_h}")

    async with websockets.connect(uri) as ws:
        print("Connected — draw on your phone to move the cursor. Ctrl+C to stop.\n")
        async for msg in ws:
            if not isinstance(msg, bytes) or len(msg) < 21:
                continue
            t, x, y = struct.unpack_from("<Bff", msg)
            px = int(max(0.0, min(x, 1.0)) * (mon_w - 1))
            py = int(max(0.0, min(y, 1.0)) * (mon_h - 1))
            try:
                if t in (EVT_MOVE, EVT_HOVER):
                    pyautogui.moveTo(px, py)
                elif t == EVT_DOWN:
                    pyautogui.mouseDown(px, py, button="left")
                elif t == EVT_UP:
                    pyautogui.mouseUp(px, py, button="left")
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage:   python agent.py <relay-url> <session-id>")
        print("Example: python agent.py wss://tabletbridge.up.railway.app abc123def456")
        sys.exit(1)

    relay, session = sys.argv[1], sys.argv[2]
    try:
        asyncio.run(run(relay, session))
    except KeyboardInterrupt:
        print("\nStopped.")
