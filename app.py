#!/usr/bin/env python3
"""
FBTC0 BTC Faucet Bot - Web Version (Railway Ready)
v3 - Full fix: proper lifespan, global error handler, robust upload
"""

import os
import sys
import json
import hashlib
import random
import asyncio
import uuid
import re
import logging
import contextlib
import traceback
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, unquote
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

# ========== LOGGING ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---------- SUPRESS TELETHON OUTPUT ----------
with contextlib.redirect_stdout(open(os.devnull, "w")):
    from telethon.sync import TelegramClient
    from telethon.tl.types import InputPeerUser, InputBotAppShortName
    from telethon.tl.functions.messages import RequestAppWebViewRequest

import requests as _rq

# ========== PATHS ==========
BASE_DIR = Path(__file__).parent.resolve()
SESSIONS_DIR = BASE_DIR / "sessions"
DATA_DIR = BASE_DIR / "data"
FILE_AKUN = DATA_DIR / "akun.json"
FILE_STATUS = DATA_DIR / "status.json"

# Ensure directories exist (try BASE_DIR first, fallback to /tmp)
for d in [SESSIONS_DIR, DATA_DIR]:
    try:
        d.mkdir(parents=True, exist_ok=True)
        # Test write
        test_file = d / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
    except Exception:
        # Fallback to /tmp
        fallback = Path("/tmp") / d.name
        fallback.mkdir(parents=True, exist_ok=True)
        if d == SESSIONS_DIR:
            SESSIONS_DIR = fallback
        else:
            DATA_DIR = fallback
        logger.warning(f"Cannot write to {d}, using {fallback}")

FILE_AKUN = DATA_DIR / "akun.json"
FILE_STATUS = DATA_DIR / "status.json"

logger.info(f"BASE_DIR: {BASE_DIR}")
logger.info(f"SESSIONS_DIR: {SESSIONS_DIR}")
logger.info(f"DATA_DIR: {DATA_DIR}")

# ========== KONFIGURASI ==========
API_ID = 38787744
API_HASH = "047e4afe5c7be80dc29988f4b4c8fd84"
BOT_USERNAME = "fbtc0bot"
API_BASE = "https://btc.tonrevenue.space/api"
GIGA_V1 = "https://ad.gigapub.tech/v1/ad"

# ========== GLOBAL STATE ==========
accounts_db = {}
claim_ok = {}
claim_fail = {}
total_berhasil = 0
total_gagal = 0
riwayat = []
riwayat_lock = asyncio.Lock()
workers = {}
ws_clients = set()


# ========== LIFESPAN ==========
@asynccontextmanager
async def lifespan(app):
    logger.info("=== APP STARTING ===")
    muat_status()
    akun_list = muat_akun()
    for acc in akun_list:
        session_path = acc.get("session_path", "")
        acc_id = acc.get("acc_id", "")
        if not os.path.exists(session_path):
            session_file = acc.get("session_file", "")
            if session_file and (SESSIONS_DIR / session_file).exists():
                session_path = str(SESSIONS_DIR / session_file)
            else:
                continue
        if not acc_id:
            acc_id = uuid.uuid4().hex[:8]
        accounts_db[acc_id] = {
            "nama": acc.get("nama", "?"),
            "phone": acc.get("phone", acc.get("nama", "?")),
            "session_path": session_path,
            "session_file": acc.get("session_file", os.path.basename(session_path)),
            "status": "IDLE",
            "saldo": 0,
        }
    logger.info(f"Loaded {len(accounts_db)} accounts")
    yield
    logger.info("=== APP SHUTTING DOWN ===")
    for acc_id in list(workers.keys()):
        if not workers[acc_id].done():
            workers[acc_id].cancel()


# ========== FASTAPI APP ==========
from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="FBTC0 Bot Web", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========== GLOBAL ERROR HANDLER ==========
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.url}: {exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={"ok": False, "detail": str(exc)[:200]},
    )


# ========== Pydantic Models ==========
class IdPayload(BaseModel):
    id: str

class RenamePayload(BaseModel):
    id: str
    name: str


# ========== HTTP ASYNC WRAPPER ==========
_DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
}
_EXECUTOR = None

async def _get_executor():
    global _EXECUTOR
    if _EXECUTOR is None:
        loop = asyncio.get_event_loop()
        _EXECUTOR = loop.run_in_executor
    return _EXECUTOR

async def http_req(method, url, json_payload=None, headers=None, timeout=20):
    run = await _get_executor()
    h = headers or _DEFAULT_HEADERS
    def _do():
        r = getattr(_rq, method)(url, json=json_payload, headers=h, timeout=timeout)
        return r.status_code, r.text
    return await run(None, _do)

async def http_post(url, payload=None, headers=None, timeout=20):
    return await http_req("post", url, payload, headers, timeout)


# ========== PARSE TG USER ==========
def parse_tg_user(init_data):
    try:
        params = dict(pair.split("=", 1) for pair in init_data.split("&") if "=" in pair)
        user_json = json.loads(unquote(params.get("user", "{}")))
        return user_json
    except Exception:
        return {"id": 0}


# ========== FINGERPRINT GENERATOR ==========
_UA_ANDROID = ("Mozilla/5.0 (Linux; Android 12; Pixel 6) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/131.0.6778.200 Mobile Safari/537.36")
_UA_IOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
           "AppleWebKit/605.1.15 (KHTML, like Gecko) "
           "Version/17.4 Mobile/15E148 Safari/604.1")

_DEVICE_PROFILES = {
    "android": {"ua": _UA_ANDROID, "screen": "412x915", "platform": "Linux armv8l",
                "viewport_width": 412, "viewport_height": 915},
    "ios": {"ua": _UA_IOS, "screen": "390x844", "platform": "iPhone",
            "viewport_width": 390, "viewport_height": 844},
}

def generate_fingerprint(ua_platform="android"):
    prof = _DEVICE_PROFILES.get(ua_platform, _DEVICE_PROFILES["android"])
    ua = prof["ua"]
    raw = f"{ua}|{ua_platform}|en-US|en,id|8|4|5|{prof['screen']}|24|Asia/Jakarta"
    visitor_id = hashlib.md5(raw.encode()).hexdigest()
    info = {"ua": ua, "screen": prof["screen"], "lang": "en-US", "tz": "Asia/Jakarta",
            "platform": prof["platform"], "tg_platform": ua_platform,
            "viewport_width": prof["viewport_width"], "viewport_height": prof["viewport_height"],
            "max_touch_points": 5, "device_pixel_ratio": 3}
    return visitor_id, info

def generate_interaction(device_info):
    return {
        "pointer_type": "touch",
        "page_x_norm": round(random.uniform(0.3, 0.7), 4),
        "page_y_norm": round(random.uniform(0.3, 0.7), 4),
        "button_x_norm": round(random.uniform(0.35, 0.65), 4),
        "button_y_norm": round(random.uniform(0.35, 0.65), 4),
        "press_ms": random.randint(80, 250), "move_count": random.randint(0, 4),
        "path_length": round(random.uniform(0.0, 0.05), 5),
        "screen_orientation": "portrait-primary",
        **{k: v for k, v in device_info.items() if k != "ua"},
    }


# ========== AMBIL INIT DATA ==========
async def get_init_data(session_path):
    client = TelegramClient(str(session_path), API_ID, API_HASH)
    await client.connect()
    try:
        bot = await client.get_entity(BOT_USERNAME)
        app_req = InputBotAppShortName(
            bot_id=InputPeerUser(user_id=bot.id, access_hash=bot.access_hash),
            short_name="app",
        )
        result = await client(RequestAppWebViewRequest(
            peer=InputPeerUser(user_id=bot.id, access_hash=bot.access_hash),
            app=app_req, platform="android", write_allowed=False,
        ))
        parsed = urlparse(result.url)
        fragment = parse_qs(parsed.fragment)
        if "tgWebAppData" in fragment:
            init_data = unquote(fragment["tgWebAppData"][0])
            start_param = fragment.get("tgWebAppStartParam", [""])[0]
            return init_data, start_param
    finally:
        await client.disconnect()
    return None, None


# ========== FBTC0 BOT CLASS ==========
class FBTC0Bot:
    def __init__(self, info_akun):
        self.session_path = info_akun["session_path"]
        self.nama = info_akun["nama"]
        self.phone = info_akun.get("phone", info_akun["nama"])
        self.init_data = None
        self.start_param = ""
        self.saldo = 0
        self.cooldown_server = 0
        self.captcha_required = False
        self.is_blocked = False
        self.adexium_remaining = 0
        self.adexium_reset_at = None
        self.fp_id, self.device_info = generate_fingerprint("android")
        self.headers = {
            "User-Agent": self.device_info["ua"],
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://btc.tonrevenue.space",
            "Referer": "https://btc.tonrevenue.space/",
        }

    def _payload_base(self):
        return {"initData": self.init_data, "start_param": self.start_param,
                "fingerprint": self.fp_id, **self.device_info}

    def _claim_payload(self):
        return {"initData": self.init_data, "interaction": generate_interaction(self.device_info)}

    async def auth(self):
        try:
            self.init_data, self.start_param = await get_init_data(self.session_path)
            if not self.init_data:
                return False, "Gagal ambil initData"
            return True, "OK"
        except Exception as e:
            return False, str(e)[:60]

    async def init_app(self):
        try:
            sc, text = await http_post(f"{API_BASE}/init", self._payload_base(), self.headers)
            if sc in (401, 403): return False, "Unauthorized/Blokir"
            if sc != 200: return False, f"Init error {sc}"
            data = json.loads(text)
            if data.get("status") != "success": return False, "Init gagal"
            user = data.get("user", {})
            self.saldo = user.get("balance", 0)
            self.cooldown_server = user.get("cooldown", 0)
            self.captcha_required = user.get("captcha_required", False)
            self.is_blocked = user.get("is_blocked", False)
            if self.is_blocked: return False, "AKUN BLOKIR"
            if data.get("access", {}).get("mobile_only_blocked"):
                self.fp_id, self.device_info = generate_fingerprint("ios")
                self.headers["User-Agent"] = self.device_info["ua"]
                return await self.init_app()
            return True, "OK"
        except json.JSONDecodeError: return False, "Bukan JSON"
        except Exception as e: return False, str(e)[:50]

    async def solve_captcha(self):
        try:
            sc, text = await http_post(f"{API_BASE}/captcha/challenge",
                                        {"initData": self.init_data}, self.headers)
            if sc != 200: return False
            data = json.loads(text)
            ch = data.get("challenge", {})
            cid, prompt, opts = ch.get("challenge_id"), ch.get("prompt", "").lower(), ch.get("options", [])
            if not cid or not opts: return False
            aid = None
            for o in opts:
                if o.get("label", "").lower() in prompt: aid = o.get("id"); break
            if not aid:
                for o in opts:
                    for w in prompt.split():
                        if len(w) > 2 and (w in o.get("label", "").lower() or w in o.get("emoji", "").lower()):
                            aid = o.get("id"); break
                    if aid: break
            if not aid and opts: aid = random.choice(opts).get("id")
            if not aid: return False
            sc2, _ = await http_post(f"{API_BASE}/captcha/verify",
                {"initData": self.init_data, "challenge_id": cid, "answer": aid}, self.headers)
            return sc2 == 200
        except: return False

    async def _gigapubs_bypass(self, session_uid):
        try:
            gu = {"user": parse_tg_user(self.init_data), "platform": self.device_info["tg_platform"],
                  "version": "10.0", "start_param": self.start_param}
            gh = {"Content-Type": "application/json", "project-id": "5736", "User-Agent": self.device_info["ua"]}
            await http_post(GIGA_V1, {"method": "init", "args": {"user": gu, "version": "v85", "seconds": 5}}, gh)
            await asyncio.sleep(0.5)
            await http_post(GIGA_V1, {"method": "adShowed", "args": {
                "user": gu, "placementId": "main", "network": "gigapubs", "rotationType": "fallback",
                "showCounter": 0, "transactionId": session_uid, "version": "v85", "seconds": 8, "anyData": {}}}, gh)
            return True
        except: return False

    async def _poll_balance(self, max_wait=15):
        old = self.saldo
        for _ in range(max_wait):
            await asyncio.sleep(1)
            ok, _ = await self.init_app()
            if ok and (self.cooldown_server > 0 or self.saldo > old): return True
        return False

    async def klaim(self):
        if not self.init_data: return False, 60, "No initData"
        if self.captcha_required:
            if await self.solve_captcha(): self.captcha_required = False
            else: return False, 60, "Captcha gagal"
        try:
            sc, text = await http_post(f"{API_BASE}/claim", self._claim_payload(), self.headers)
            if sc == 428:
                if await self.solve_captcha():
                    self.captcha_required = False
                    await asyncio.sleep(1)
                    sc, text = await http_post(f"{API_BASE}/claim", self._claim_payload(), self.headers)
                else: return False, 60, "Captcha 428 gagal"
            if sc != 200: return False, 60, f"HTTP {sc}"
            data = json.loads(text)
            st = data.get("status", "")
            if st == "success":
                self.saldo = data.get("new_balance", self.saldo)
                return True, data.get("cooldown", 300), f"+{data.get('reward', 0)} sat"
            if st == "ad_required":
                suid, prov, rsats = data.get("session_uid"), data.get("provider", ""), data.get("reward_sats", 0)
                self.adexium_remaining = data.get("adexium_remaining", 0)
                ar = data.get("adexium_reset_at")
                if ar:
                    try: self.adexium_reset_at = datetime.fromisoformat(ar.replace("Z", "+00:00"))
                    except: pass
                if not suid: return False, 60, "No session_uid"
                if prov == "gigapubs":
                    if await self._gigapubs_bypass(suid) and await self._poll_balance():
                        if (await self.init_app())[0]: return True, self.cooldown_server or 300, f"+{rsats} sat"
                    if self.adexium_remaining > 0:
                        await asyncio.sleep(random.uniform(2.5, 4.0))
                        sc2, t2 = await http_post(f"{API_BASE}/claim/confirm",
                            {"initData": self.init_data, "session_uid": suid}, self.headers)
                        if sc2 == 200:
                            d2 = json.loads(t2)
                            if d2.get("status") == "success":
                                self.saldo = d2.get("new_balance", self.saldo)
                                return True, d2.get("cooldown", 300), f"+{d2.get('reward', rsats)} sat"
                    return False, 120, "GigaPubs gagal"
                if prov == "adexium":
                    await asyncio.sleep(random.uniform(2.5, 4.0))
                    sc2, t2 = await http_post(f"{API_BASE}/claim/confirm",
                        {"initData": self.init_data, "session_uid": suid}, self.headers)
                    if sc2 == 200:
                        d2 = json.loads(t2)
                        if d2.get("status") == "success":
                            self.saldo = d2.get("new_balance", self.saldo)
                            return True, d2.get("cooldown", 300), f"+{d2.get('reward', rsats)} sat"
                    sc_fb, t_fb = await http_post(f"{API_BASE}/claim/fallback",
                        {"initData": self.init_data}, self.headers)
                    if sc_fb == 200:
                        dfb = json.loads(t_fb)
                        fuid = dfb.get("session_uid")
                        if fuid and dfb.get("provider") == "gigapubs":
                            if await self._gigapubs_bypass(fuid) and await self._poll_balance():
                                if (await self.init_app())[0]: return True, self.cooldown_server or 300, f"+{rsats} sat"
                    return False, 120, "Adexium+GigaPubs gagal"
            return False, 60, f"Status: {st}"
        except json.JSONDecodeError: return False, 60, "Bukan JSON"
        except Exception as e: return False, 60, f"Error: {str(e)[:40]}"

    async def cek_saldo(self):
        await self.init_app()
        return self.saldo


# ========== HELPERS ==========
async def tambah_riwayat(akun, pesan):
    async with riwayat_lock:
        sekarang = datetime.now().strftime("%H:%M:%S")
        riwayat.append(f"{sekarang} | {akun} | {pesan}")
        if len(riwayat) > 100: riwayat.pop(0)
    await broadcast_event({"type": "log", "data": {"time": sekarang, "account": akun, "message": pesan}})

async def broadcast_event(event: dict):
    dead = set()
    for ws in ws_clients:
        try: await ws.send_json(event)
        except: dead.add(ws)
    ws_clients -= dead

def format_waktu(d):
    if d <= 0: return "-"
    if d >= 3600: return f"{d//3600}j {(d%3600)//60}m"
    if d >= 60: return f"{d//60}m {d%60}d"
    return f"{d}d"


# ========== WORKER ==========
async def pekerja_akun(info_akun, acc_id):
    global total_berhasil, total_gagal
    bot = FBTC0Bot(info_akun)
    nama = info_akun["nama"]
    if acc_id not in accounts_db: return

    for k in [acc_id]: 
        if k not in claim_ok: claim_ok[k] = 0
        if k not in claim_fail: claim_fail[k] = 0

    def _status(s):
        if acc_id in accounts_db:
            accounts_db[acc_id]["status"] = s

    _status("AUTH..."); await broadcast_event({"type": "status_update", "data": get_all_status()})
    ok, msg = await bot.auth()
    if not ok:
        _status("GAGAL AUTH"); total_gagal += 1; claim_fail[acc_id] += 1
        await tambah_riwayat(nama, f"Gagal auth: {msg}"); simpan_status()
        await broadcast_event({"type": "status_update", "data": get_all_status()}); return
    await tambah_riwayat(nama, "Auth OK")

    _status("LOAD..."); await broadcast_event({"type": "status_update", "data": get_all_status()})
    ok, msg = await bot.init_app()
    if not ok:
        _status("GAGAL INIT"); total_gagal += 1; claim_fail[acc_id] += 1
        await tambah_riwayat(nama, f"Init gagal: {msg}"); simpan_status()
        await broadcast_event({"type": "status_update", "data": get_all_status()}); return
    accounts_db[acc_id]["saldo"] = bot.saldo
    await tambah_riwayat(nama, f"Saldo: {bot.saldo} sat")

    if bot.captcha_required:
        _status("CAPTCHA..."); await broadcast_event({"type": "status_update", "data": get_all_status()})
        await tambah_riwayat(nama, "Captcha solving...")
        if await bot.solve_captcha():
            bot.captcha_required = False; await tambah_riwayat(nama, "Captcha solved!")
        else: await tambah_riwayat(nama, "Captcha gagal")

    if bot.cooldown_server > 0:
        await tambah_riwayat(nama, f"Cooldown {format_waktu(bot.cooldown_server)}")
        for t in range(bot.cooldown_server, 0, -1):
            if acc_id not in accounts_db: return
            _status(format_waktu(t))
            if t % 3 == 0: await broadcast_event({"type": "status_update", "data": get_all_status()})
            await asyncio.sleep(1)

    _status("SIAP"); await broadcast_event({"type": "status_update", "data": get_all_status()})

    while acc_id in accounts_db:
        try:
            _status("KLAIM..."); await broadcast_event({"type": "status_update", "data": get_all_status()})
            berhasil, cooldown, pesan = await bot.klaim()
            if berhasil:
                total_berhasil += 1; claim_ok[acc_id] += 1; accounts_db[acc_id]["saldo"] = bot.saldo
                await tambah_riwayat(nama, f"OK {pesan} | Saldo: {bot.saldo}")
            else:
                for retry in range(3):
                    await tambah_riwayat(nama, f"Retry {retry+1}/3 - {pesan}")
                    _status(f"RETRY {retry+1}/3")
                    await broadcast_event({"type": "status_update", "data": get_all_status()})
                    await asyncio.sleep(3)
                    if (await bot.auth())[0]:
                        berhasil, cooldown, pesan = await bot.klaim()
                        if berhasil: break
                    else:
                        await tambah_riwayat(nama, f"Refresh gagal retry {retry+1}")
                        await asyncio.sleep(2)
                        berhasil, cooldown, pesan = await bot.klaim()
                if berhasil:
                    total_berhasil += 1; claim_ok[acc_id] += 1; accounts_db[acc_id]["saldo"] = bot.saldo
                    await tambah_riwayat(nama, f"OK {pesan} | Saldo: {bot.saldo}")
                else:
                    total_gagal += 1; claim_fail[acc_id] += 1
                    await tambah_riwayat(nama, f"GAGAL - {pesan}")
            simpan_status()
            _status("CEK..."); await broadcast_event({"type": "status_update", "data": get_all_status()})
            await bot.cek_saldo(); accounts_db[acc_id]["saldo"] = bot.saldo
            sisa = max(min(cooldown, 3600), 60)
            for t in range(sisa, 0, -1):
                if acc_id not in accounts_db: return
                _status(format_waktu(t))
                if t % 5 == 0: await broadcast_event({"type": "status_update", "data": get_all_status()})
                await asyncio.sleep(1)
            if (await bot.auth())[0]:
                await bot.init_app()
                if bot.captcha_required: await bot.solve_captcha(); bot.captcha_required = False
        except asyncio.CancelledError: break
        except Exception as e:
            await tambah_riwayat(nama, f"Error: {str(e)[:50]}"); await asyncio.sleep(10)


# ========== STATUS HELPERS ==========
def get_all_status():
    akun_list = []
    for aid, d in accounts_db.items():
        akun_list.append({
            "id": aid, "nama": d["nama"], "session_file": d.get("session_file", ""),
            "saldo": d["saldo"], "status": d["status"],
            "ok": claim_ok.get(aid, 0), "fail": claim_fail.get(aid, 0),
            "running": aid in workers and not workers[aid].done(),
        })
    return {"total": len(accounts_db),
            "aktif": sum(1 for d in accounts_db.values() if any(k in d["status"] for k in ["SIAP","KLAIM","CEK","RETRY"])),
            "total_ok": total_berhasil, "total_fail": total_gagal,
            "accounts": akun_list, "riwayat": list(riwayat[-20:])}

def muat_status():
    if FILE_STATUS.exists():
        try:
            for item in json.loads(FILE_STATUS.read_text()):
                aid = item.get("acc_id", "")
                if aid: claim_ok[aid] = int(item.get("ok", 0)); claim_fail[aid] = int(item.get("fail", 0))
        except: pass

def muat_akun():
    if FILE_AKUN.exists():
        try: return json.loads(FILE_AKUN.read_text())
        except: return []
    return []

def simpan_status():
    try:
        data = [{"acc_id": k, "nama": v["nama"], "ok": claim_ok.get(k, 0), "fail": claim_fail.get(k, 0)} for k, v in accounts_db.items()]
        FILE_STATUS.write_text(json.dumps(data, indent=4))
    except Exception as e: logger.error(f"simpan_status: {e}")

def simpan_akun():
    try:
        data = [{"acc_id": k, "session_path": v["session_path"], "session_file": v.get("session_file", ""),
                 "nama": v["nama"], "phone": v.get("phone", v["nama"])} for k, v in accounts_db.items()]
        FILE_AKUN.write_text(json.dumps(data, indent=4))
    except Exception as e: logger.error(f"simpan_akun: {e}")

def sanitize_filename(name):
    name = os.path.basename(name)
    name = re.sub(r'[^\w\-.]', '_', name)
    return name or "unknown"


# ========== API ROUTES ==========
@app.get("/")
async def serve_dashboard():
    return FileResponse(BASE_DIR / "static" / "index.html")

@app.get("/api/status")
async def api_status():
    return get_all_status()

@app.get("/api/debug")
async def debug_info():
    """Debug endpoint to check filesystem and state"""
    return {
        "base_dir": str(BASE_DIR),
        "sessions_dir": str(SESSIONS_DIR),
        "sessions_writable": os.access(SESSIONS_DIR, os.W_OK),
        "data_dir": str(DATA_DIR),
        "data_writable": os.access(DATA_DIR, os.W_OK),
        "sessions_files": [f.name for f in SESSIONS_DIR.iterdir()] if SESSIONS_DIR.exists() else [],
        "accounts_count": len(accounts_db),
        "workers_count": len(workers),
    }


@app.post("/api/upload")
async def upload_session(file: UploadFile = File(...)):
    try:
        filename = file.filename or ""
        logger.info(f"Upload request: filename={filename}, content_type={file.content_type}")

        if not filename.lower().endswith(".session"):
            return JSONResponse(status_code=400, content={"ok": False, "detail": "File harus .session"})

        clean_name = sanitize_filename(filename[:-7] if filename.lower().endswith(".session") else filename)
        session_filename = f"{clean_name}.session"
        session_path = SESSIONS_DIR / session_filename

        # Handle duplicate
        counter = 1
        orig = clean_name
        while session_path.exists():
            clean_name = f"{orig}_{counter}"
            session_filename = f"{clean_name}.session"
            session_path = SESSIONS_DIR / session_filename
            counter += 1

        content = await file.read()
        logger.info(f"File size: {len(content)} bytes")

        if len(content) == 0:
            return JSONResponse(status_code=400, content={"ok": False, "detail": "File kosong"})

        session_path.write_bytes(content)
        logger.info(f"Saved to: {session_path}")

        # Check existing
        existing_id = None
        for aid, d in accounts_db.items():
            if d.get("session_path") == str(session_path):
                existing_id = aid; break

        if existing_id:
            accounts_db[existing_id]["session_path"] = str(session_path)
            accounts_db[existing_id]["session_file"] = session_filename
            await tambah_riwayat(clean_name, "Session updated")
        else:
            acc_id = uuid.uuid4().hex[:8]
            accounts_db[acc_id] = {
                "nama": clean_name, "phone": clean_name,
                "session_path": str(session_path), "session_file": session_filename,
                "status": "IDLE", "saldo": 0,
            }
            await tambah_riwayat(clean_name, "Akun ditambahkan")

        simpan_akun()
        return {"ok": True, "message": f"Akun '{clean_name}' berhasil ditambahkan"}

    except Exception as e:
        logger.error(f"Upload error: {e}\n{traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"ok": False, "detail": str(e)[:200]})


@app.post("/api/account/start")
async def start_account(payload: IdPayload):
    aid = payload.id
    if aid not in accounts_db:
        return JSONResponse(status_code=404, content={"ok": False, "message": "Akun tidak ditemukan"})
    if aid in workers and not workers[aid].done():
        return {"ok": False, "message": "Akun sudah berjalan"}
    info = accounts_db[aid]
    workers[aid] = asyncio.create_task(pekerja_akun(
        {"session_path": info["session_path"], "nama": info["nama"], "phone": info.get("phone", info["nama"])}, aid))
    await tambah_riwayat(info["nama"], "Bot dimulai")
    return {"ok": True, "message": f"Bot '{info['nama']}' dimulai"}


@app.post("/api/account/stop")
async def stop_account(payload: IdPayload):
    aid = payload.id
    if aid not in accounts_db:
        return JSONResponse(status_code=404, content={"ok": False, "message": "Akun tidak ditemukan"})
    if aid in workers and not workers[aid].done():
        workers[aid].cancel()
        accounts_db[aid]["status"] = "STOPPED"
        await tambah_riwayat(accounts_db[aid]["nama"], "Bot dihentikan")
    else:
        accounts_db[aid]["status"] = "IDLE"
    await broadcast_event({"type": "status_update", "data": get_all_status()})
    return {"ok": True, "message": "Bot dihentikan"}


@app.post("/api/start-all")
async def start_all():
    started = 0
    for aid, info in accounts_db.items():
        if aid not in workers or workers[aid].done():
            workers[aid] = asyncio.create_task(pekerja_akun(
                {"session_path": info["session_path"], "nama": info["nama"], "phone": info.get("phone", info["nama"])}, aid))
            started += 1
    await tambah_riwayat("SYSTEM", f"Start all: {started} akun")
    return {"ok": True, "started": started}


@app.post("/api/stop-all")
async def stop_all():
    stopped = 0
    for aid in list(workers.keys()):
        if not workers[aid].done():
            workers[aid].cancel()
            if aid in accounts_db: accounts_db[aid]["status"] = "STOPPED"
            stopped += 1
    await tambah_riwayat("SYSTEM", f"Stop all: {stopped} akun")
    await broadcast_event({"type": "status_update", "data": get_all_status()})
    return {"ok": True, "stopped": stopped}


@app.post("/api/account/delete")
async def delete_account(payload: IdPayload):
    aid = payload.id
    if aid not in accounts_db:
        return JSONResponse(status_code=404, content={"ok": False, "message": "Akun tidak ditemukan"})
    if aid in workers and not workers[aid].done():
        workers[aid].cancel()
    nama = accounts_db[aid]["nama"]
    sp = accounts_db[aid].get("session_path", "")
    if sp and os.path.exists(sp):
        try: os.remove(sp)
        except: pass
    del accounts_db[aid]
    claim_ok.pop(aid, None); claim_fail.pop(aid, None); workers.pop(aid, None)
    simpan_akun(); simpan_status()
    await tambah_riwayat("SYSTEM", f"Akun '{nama}' dihapus")
    await broadcast_event({"type": "status_update", "data": get_all_status()})
    return {"ok": True, "message": f"Akun '{nama}' dihapus"}


@app.post("/api/account/rename")
async def rename_account(payload: RenamePayload):
    aid, new_name = payload.id, payload.name.strip()
    if aid not in accounts_db:
        return JSONResponse(status_code=404, content={"ok": False, "message": "Akun tidak ditemukan"})
    if not new_name:
        return JSONResponse(status_code=400, content={"ok": False, "message": "Nama kosong"})
    old = accounts_db[aid]["nama"]
    accounts_db[aid]["nama"] = new_name; accounts_db[aid]["phone"] = new_name
    simpan_akun()
    await tambah_riwayat("SYSTEM", f"Rename: {old} -> {new_name}")
    await broadcast_event({"type": "status_update", "data": get_all_status()})
    return {"ok": True, "message": f"Rename ke '{new_name}'"}


# ========== WEBSOCKET ==========
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        await ws.send_json({"type": "init", "data": get_all_status()})
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=30)
                if data == "ping": await ws.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)


# ========== MAIN ==========
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")