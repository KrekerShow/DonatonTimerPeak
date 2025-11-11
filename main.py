import os, time, json, asyncio, contextlib
from typing import Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
import websockets

DA_TOKEN = os.getenv("DA_TOKEN", "")          # Токен DonationAlerts (WebSocket API)
START_SECONDS = int(os.getenv("START_SECONDS", "0"))

RUB_PER_5MIN = 1000
SECS_PER_RUB = (5 * 60) / RUB_PER_5MIN        # 0.3 сек за 1 ₽
DONATION_TEXT_SHOW_MS = 15_000

timer_ends_at_ms = int(time.time() * 1000) + START_SECONDS * 1000
last_label = ""
show_label_until_ms = 0
clients: Set[WebSocket] = set()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=True
)

WIDGET_HTML = """<!doctype html>
<html><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"><title>DA Timer</title>
<style>
  html,body{margin:0;padding:0;background:transparent}
  .wrap{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none}
  .timer{font:800 clamp(40px,12vw,120px)/1 system-ui,Segoe UI,Roboto,Arial; color:#fff; text-shadow:0 2px 8px rgba(0,0,0,.6)}
  .label{margin-top:.4em; font:600 clamp(18px,4vw,32px)/1.25 system-ui,Segoe UI,Roboto,Arial; color:#fff; text-shadow:0 1px 6px rgba(0,0,0,.55); opacity:.95; max-width:90vw; text-align:center}
</style></head>
<body>
<div class="wrap">
  <div class="timer" id="timer">00:00</div>
  <div class="label" id="label"></div>
</div>
<script>
  function fmt(ms){ if(ms<0)ms=0; const t=Math.floor(ms/1000);
    const h=Math.floor(t/3600), m=Math.floor((t%3600)/60), s=t%60;
    return (h>0?String(h).padStart(2,'0')+':':'')+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0'); }
  let ws;
  function connect(){
    const proto=location.protocol==='https:'?'wss':'ws';
    ws=new WebSocket(proto+'://'+location.host+'/ws');
    ws.onmessage=e=>{try{const d=JSON.parse(e.data);
      if(d.type==='tick'){document.getElementById('timer').textContent=fmt(d.remaining);
        document.getElementById('label').textContent=d.label||'';}}catch(_){}
    };
    ws.onclose=()=>setTimeout(connect,1000);
  }
  connect();
</script>
</body></html>
"""

@app.get("/", response_class=HTMLResponse)
def home():
    return ('<div style="font-family:system-ui;padding:24px">'
            '<h2>DA Timer</h2><p>Overlay: <a href="/widget" target="_blank">/widget</a></p>'
            '<p>Test API: /api/add?sec=60&label=Test , /api/reset?sec=0</p></div>')

@app.get("/widget", response_class=HTMLResponse)
def widget(): return WIDGET_HTML

@app.get("/health", response_class=PlainTextResponse)
def health(): return "ok"

@app.get("/api/add")
def api_add(sec: int = 10, label: str = "Добавили время"):
    global timer_ends_at_ms, last_label, show_label_until_ms
    now = int(time.time() * 1000)
    base = max(timer_ends_at_ms, now)
    timer_ends_at_ms = base + sec * 1000
    last_label = f"+{sec} сек • {label}"
    show_label_until_ms = now + DONATION_TEXT_SHOW_MS
    return {"ok": True, "added_sec": sec, "ends_at": timer_ends_at_ms}

@app.get("/api/reset")
def api_reset(sec: int = 0):
    global timer_ends_at_ms
    timer_ends_at_ms = int(time.time() * 1000) + sec * 1000
    return {"ok": True, "ends_at": timer_ends_at_ms}

@app.websocket("/ws")
async def ws_overlay(ws: WebSocket):
    await ws.accept(); clients.add(ws)
    try:
        while True: await asyncio.sleep(60)
    except WebSocketDisconnect: pass
    finally:
        with contextlib.suppress(KeyError): clients.remove(ws)

async def broadcaster():
    global timer_ends_at_ms, last_label, show_label_until_ms
    while True:
        now = int(time.time() * 1000)
        remaining = max(0, timer_ends_at_ms - now)
        label = last_label if now <= show_label_until_ms else ""
        payload = json.dumps({"type":"tick","remaining":remaining,"label":label})
        dead=[]
        for c in list(clients):
            try: await c.send_text(payload)
            except Exception: dead.append(c)
        for c in dead:
            with contextlib.suppress(KeyError): clients.remove(c)
        await asyncio.sleep(0.1)

async def handle_da_message(msg: dict):
    global timer_ends_at_ms, last_label, show_label_until_ms
    data = msg.get("data", msg)
    donations = []
    if isinstance(data, list): donations = data
    elif isinstance(data, dict) and isinstance(data.get("donations"), list): donations = data["donations"]
    elif isinstance(data, dict) and ("amount" in data or "sum" in data): donations = [data]

    for d in donations:
        username = d.get("username") or d.get("name") or "Аноним"
        amount = float(d.get("amount") or d.get("sum") or 0)
        currency = (d.get("currency") or d.get("cur") or "RUB").upper()
        amount_rub = amount  # при желании добавь конвертацию
        add_sec = amount_rub * SECS_PER_RUB
        now = int(time.time() * 1000)
        base = max(timer_ends_at_ms, now)
        timer_ends_at_ms = base + int(round(add_sec * 1000))
        last_label = f"+{int(round(add_sec))} сек • {username} — {amount:g} {currency}"
        show_label_until_ms = now + DONATION_TEXT_SHOW_MS
        print(f"[DA] {username} {amount:g} {currency} -> +{int(round(add_sec))} сек")

async def da_connect_loop():
    if not DA_TOKEN:
        print("[WARN] DA_TOKEN не указан — донаты не ловим."); return
    endpoints = ["wss://socket.donationalerts.ru:443","wss://socket.donationalerts.com:443"]
    idx = 0
    while True:
        url = endpoints[idx % len(endpoints)]; idx += 1
        try:
            print("[DA] Connecting:", url)
            async with websockets.connect(url, ping_interval=30, ping_timeout=20) as ws:
                subscribe = {"command":"subscribe","params":{"token":DA_TOKEN,"type":"donation"}}
                await ws.send(json.dumps(subscribe))
                print("[DA] Subscribed, waiting for events...")
                async for raw in ws:
                    try: await handle_da_message(json.loads(raw))
                    except Exception as e: print("[DA] parse error:", e)
        except Exception as e:
            print("[DA] error/closed:", e); await asyncio.sleep(2)

@app.on_event("startup")
async def on_start():
    asyncio.create_task(broadcaster())
    asyncio.create_task(da_connect_loop())
