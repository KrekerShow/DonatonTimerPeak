import os, asyncio, json, logging, random, time
from typing import Any, AsyncGenerator, Dict, List

import httpx, websockets
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn

# === TOKEN: сначала ENV, иначе хардкод (как резерв)
DA_TOKEN = os.getenv("DA_TOKEN", "").strip() or (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9."
    "eyJhdWQiOiIxNjU2NiIsImp0aSI6IjliMWQxNjg0MzQ2YTQ4MWQzYWU2ODNlMmFhZTQ5NGQ2YzFmY2RkYjIyOTIyZmY0YWJlMDA5ZDQyN2JmODQ1YzdmNTk3Y2VjZTIzNmM5ZDVkIiwiaWF0IjoxNzYyOTM2OTQzLjA5ODcsIm5iZiI6MTc2MjkzNjk0My4wOTg3LCJleHAiOjE3OTQ0NzI5NDMuMDkxOCwic3ViIjoiNzgyMzIxIiwic2NvcGVzIjpbIm9hdXRoLXVzZXItc2hvdyIsIm9hdXRoLWRvbmF0aW9uLXN1YnNjcmliZSJdfQ."
    "IwgcfwGM3YC2DREwDalnjGKkjYvD-Q81Lnxs363o12s8UTDQNpnt6BkvJqRtDpWayIe8dLQ_p5tVF8IWQZduGiS637o8RKU7mB_gv7FHXmUxToLS53SNANkhAGK6UYcx7s6u9EjpFiR9phCf7da2MfCZFygeINLNg4YJlZd70XsFTQOanawwZyXEb5vdLMDJxsp263V9CRFiB5favgZTShDr3N4hhXyNZi1ilelN0NL4kidD8H45fCycE4RlrJs35NjUK6Uiz4x26QkAFUMFVHZk49skqCsXWEQf30fYEp3HMqg5oLoVPinVu4jYIelorKP4xp6_WTWTfxVyq2RBm6pLUOHV8ZP3Cj_gdSzDZCenXr-7rWALbAAREoyl2geb0ntQg4TuxSx_8-8rd5SkpZPqT6Y3i_RVV90zFNvnMKGEpYIXLbFduXu3dLTIjUAsFjmktikzflIkAmdAsH9BJ-EfGvklo71hGpg6Nmvvlr89Lm6M6C5K7lUzec7ZPNe_zQqvaF6I-DenS2YGebXiPknYXb9EjHRtnB25dpV0PMroD3LQKVzM86Fvbh9H6VGgREpLxA15L-JYX5I1znbUMS5ORatYjNOIi4jEqDtco04Re2LzygWTp8jnpVVJCHPSoUwm3EaWCzxxEo1z1ZDK_Gtr4rRh93RFqvKVtxYEQEs"
)

API_BASE = "https://www.donationalerts.com/api/v1"
CENTRIFUGO_WS = "wss://centrifugo.donationalerts.com/connection/websocket"

# === НАСТРОЙКИ ТАЙМЕРА: 500 ₽ = 2.5 минуты
RUB_PER_STEP = 500
SECONDS_PER_STEP = 150  # 2.5 минуты

RECONNECT_MIN, RECONNECT_MAX = 3, 10
STATE_FILE = "timer_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

app = FastAPI(title="DA Timer Overlay", version="1.0.2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=True
)

timer_end_ms: int = 0
subscribers: List[asyncio.Queue] = []


def now_ms() -> int:
    return int(time.time() * 1000)


def load_state():
    global timer_end_ms
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            timer_end_ms = int(json.load(f).get("timer_end_ms", 0))
    except Exception:
        timer_end_ms = 0


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"timer_end_ms": timer_end_ms}, f)
    except Exception:
        pass


async def broadcast_state():
    state = {"type": "state", "end_ms": timer_end_ms, "server_ts": now_ms()}
    dead = []
    for q in subscribers:
        try:
            await q.put(state)
        except Exception:
            dead.append(q)
    for q in dead:
        if q in subscribers:
            subscribers.remove(q)


def add_time_for_amount_rub(amount_rub: float) -> int:
    steps = int(max(0.0, amount_rub) // RUB_PER_STEP)
    return steps * SECONDS_PER_STEP


async def apply_donation_rub(amount_rub: float, who: str = "", message: str = ""):
    global timer_end_ms
    add_sec = add_time_for_amount_rub(amount_rub)
    if add_sec <= 0:
        logging.info("Донат от %s на %.2f RUB — +0 сек (меньше %d).", who or "—", amount_rub, RUB_PER_STEP)
        return
    base = max(timer_end_ms, now_ms())
    timer_end_ms = base + add_sec * 1000
    save_state()
    logging.info("🎉 TIMER +%ds (%.2f RUB) от %s — %s; new_end=%d",
                 add_sec, amount_rub, who or "—", message or "", timer_end_ms)
    await broadcast_state()


@app.get("/overlay", response_class=HTMLResponse)
async def overlay() -> str:
    return """
<!doctype html><html lang="ru"><head><meta charset="utf-8"/>
<title>Timer Overlay</title>
<style>
  /* Делаем страницу ровно по размеру контента — без лишнего пустого места */
  html, body {
    margin: 0;
    padding: 0;
    background: transparent;
    display: inline-block;
    width: auto;
    height: auto;
  }

  body {
    font-family: Inter, system-ui, Segoe UI, Arial;
    color: #fff;
  }

  .wrap {
    box-sizing: border-box;
    display: inline-flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 4px 12px;
    background: transparent;
  }

  .rule {
    font-size: 26px;
    font-weight: 600;
    opacity: .98;
    white-space: nowrap;
    text-shadow: 0 0 6px rgba(0,0,0,0.7);
  }

  .timer {
    margin-top: 4px;
    font-weight: 900;
    font-size: 82px;
    line-height: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0;
    text-shadow: 0 0 10px rgba(0,0,0,0.9);
  }

  .timer-emoji {
    font-size: 0.9em;
    line-height: 1;
  }

  .timer-value {
    line-height: 1;
  }
</style>
</head><body>
  <div class="wrap">
    <div class="rule">Каждые 500 руб = Спуск +2.5 минуты</div>
    <div class="timer">
      <span class="timer-emoji">⛰️</span>
      <span id="tm" class="timer-value">00:00</span>
      <span class="timer-emoji">⛰️</span>
    </div>
  </div>
<script>
let endMs = 0;
let driftCorr = 0;

function fmt(t){
  t = Math.max(0, Math.floor(t));
  const h = Math.floor(t/3600);
  const m = Math.floor((t%3600)/60);
  const s = t % 60;
  const pad = n => n < 10 ? "0" + n : "" + n;
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function tick(){
  const el = document.getElementById('tm');
  if (!el){
    requestAnimationFrame(tick);
    return;
  }
  if (!endMs){
    el.textContent = "00:00";
    requestAnimationFrame(tick);
    return;
  }
  const now = Date.now() + driftCorr;
  const left = Math.max(0, Math.round((endMs - now) / 1000));
  el.textContent = fmt(left);
  requestAnimationFrame(tick);
}

function connect(){
  const es = new EventSource('/stream');
  es.onmessage = ev => {
    try{
      const data = JSON.parse(ev.data);
      if (data && data.type === 'state'){
        endMs = Number(data.end_ms) || 0;
        if (data.server_ts){
          driftCorr = Number(data.server_ts) - Date.now();
        }
      }
    }catch(e){}
  };
  es.onerror = () => {
    es.close();
    setTimeout(connect, 2000);
  };
}
connect();
tick();
</script>
</body></html>
    """


@app.get("/stream")
async def stream() -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue()
    subscribers.append(q)

    async def gen() -> AsyncGenerator[str, None]:
        # отправляем текущее состояние сразу
        yield "data: " + json.dumps(
            {"type": "state", "end_ms": timer_end_ms, "server_ts": now_ms()},
            ensure_ascii=False
        ) + "\n\n"
        try:
            while True:
                evt = await q.get()
                yield "data: " + json.dumps(evt, ensure_ascii=False) + "\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if q in subscribers:
                subscribers.remove(q)

    headers = {
        "Cache-Control": "no-cache",
        "Content-Type": "text/event-stream",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",
    }
    return StreamingResponse(gen(), headers=headers)


def extract_amount_rub(it: Dict[str, Any]) -> float:
    for key in ("amount_main", "amount_in_user_currency", "amount", "sum"):
        v = it.get(key)
        try:
            if v is not None:
                return float(v)
        except Exception:
            continue
    return 0.0


async def centrifugo_connect_and_subscribe(socket_token: str, user_id: int, client_http: httpx.AsyncClient):
    channel_name = f"$alerts:donation_{user_id}"
    async with websockets.connect(CENTRIFUGO_WS, ping_interval=20, ping_timeout=20) as ws:
        # connect
        await ws.send(json.dumps({"params": {"token": socket_token}, "id": 1}))
        msg = json.loads(await ws.recv())
        client_id = (msg.get("result") or {}).get("client")
        if not client_id:
            raise RuntimeError(f"Не получили client_id: {msg}")

        # subscribe
        sub_req = {"channels": [channel_name], "client": client_id}
        resp = await client_http.post(f"{API_BASE}/centrifuge/subscribe", json=sub_req)
        resp.raise_for_status()
        channel_token = ((resp.json() or {}).get("channels") or [{}])[0].get("token")
        if not channel_token:
            raise RuntimeError(f"Нет token для канала {channel_name}: {resp.text}")

        await ws.send(json.dumps({
            "params": {"channel": channel_name, "token": channel_token},
            "method": 1,
            "id": 2
        }))
        logging.info("✅ Подписан на %s, слушаю донаты…", channel_name)

        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue

            pub = (data.get("result") or {}).get("publication") or data.get("publication")
            payload = pub.get("data") if isinstance(pub, dict) else None
            if payload is None:
                rdata = (data.get("result") or {}).get("data")
                if isinstance(rdata, dict) and "data" in rdata:
                    payload = rdata["data"]
            if payload is None:
                continue

            items = payload if isinstance(payload, list) else [payload]
            for it in items:
                if not isinstance(it, dict):
                    continue
                who = (
                    it.get("username")
                    or it.get("name")
                    or (it.get("recipient") or {}).get("name")
                    or "—"
                )
                msg_ = it.get("message") or it.get("comment") or it.get("text") or ""
                amount_rub = extract_amount_rub(it)
                await apply_donation_rub(amount_rub, who=who, message=msg_)


async def da_loop():
    if not DA_TOKEN:
        logging.info("DA_TOKEN не задан — realtime отключён.")
        return

    headers = {"Authorization": f"Bearer {DA_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
        while True:
            try:
                r = await client.get(f"{API_BASE}/user/oauth")
                r.raise_for_status()
                u = r.json().get("data") or {}
                user_id = int(u["id"])
                socket_token = u["socket_connection_token"]
                await centrifugo_connect_and_subscribe(socket_token, user_id, client)
            except Exception as e:
                logging.warning("Realtime ошибка: %s", e)
            delay = random.uniform(RECONNECT_MIN, RECONNECT_MAX)
            logging.info("Переподключение через %.1f с.", delay)
            await asyncio.sleep(delay)


@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time())}


@app.on_event("startup")
async def on_start():
    load_state()
    await broadcast_state()
    asyncio.create_task(da_loop())


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=False,
        timeout_graceful_shutdown=0,
        timeout_keep_alive=75
    )
