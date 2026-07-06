#!/usr/bin/env python3
"""
FBTC0 BTC Faucet Bot - Web Version (Railway Ready)
FastAPI backend with WebSocket real-time dashboard
"""

import os
import sys
import json
import hashlib
import random
import asyncio
import shutil
import contextlib
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, unquote
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ---------- SUPRESS TELETHON OUTPUT ----------
with contextlib.redirect_stdout(open(os.devnull, "w")):
    from telethon.sync import TelegramClient
    from telethon.tl.types import InputPeerUser, InputBotAppShortName
    from telethon.tl.functions.messages import RequestAppWebViewRequest

import requests as _rq

# ========== PATHS ==========
BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "sessions"
DATA_DIR = BASE_DIR / "data"
FILE_AKUN = DATA_DIR / "akun.json"
FILE_STATUS = DATA_DIR / "status.json"

SESSIONS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# ========== KONFIGURASI ==========
API_ID = 38787744
API_HASH = "047e4afe5c7be80dc29988f4b4c8fd84"
BOT_USERNAME = "fbtc0bot"
API_BASE = "https://btc.tonrevenue.space/api"
GIGA_V1 = "https://ad.gigapub.tech/v1/ad"

# ========== FASTAPI APP ==========
app = FastAPI(title="FBTC0 Bot Web")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== GLOBAL STATE ==========
accounts_db = {}       # key -> {nama, phone, session_path, status, saldo, ...}
claim_ok = {}          # key -> int
claim_fail = {}        # key -> int
total_berhasil = 0
total_gagal = 0
riwayat = []
riwayat_lock = asyncio.Lock()
workers = {}           # key -> asyncio.Task
broadcast_queue = asyncio.Queue()
ws_clients = set()


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


async def http_get(url, headers=None, timeout=20):
    return await http_req("get", url, None, headers, timeout)


# ========== PARSE TG USER ==========
def parse_tg_user(init_data):
    try:
        params = dict(
            pair.split("=", 1) for pair in init_data.split("&") if "=" in pair
        )
        user_json = json.loads(unquote(params.get("user", "{}")))
        return user_json
    except Exception:
        return {"id": 0}


# ========== FINGERPRINT GENERATOR ==========
_UA_ANDROID = (
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.6778.200 Mobile Safari/537.36"
)
_UA_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Mobile/15E148 Safari/604.1"
)

_DEVICE_PROFILES = {
    "android": {
        "ua": _UA_ANDROID,
        "screen": "412x915",
        "platform": "Linux armv8l",
        "viewport_width": 412,
        "viewport_height": 915,
    },
    "ios": {
        "ua": _UA_IOS,
        "screen": "390x844",
        "platform": "iPhone",
        "viewport_width": 390,
        "viewport_height": 844,
    },
}


def generate_fingerprint(ua_platform="android"):
    prof = _DEVICE_PROFILES.get(ua_platform, _DEVICE_PROFILES["android"])
    ua = prof["ua"]
    raw = f"{ua}|{ua_platform}|en-US|en,id|8|4|5|{prof['screen']}|24|Asia/Jakarta"
    visitor_id = hashlib.md5(raw.encode()).hexdigest()
    info = {
        "ua": ua,
        "screen": prof["screen"],
        "lang": "en-US",
        "tz": "Asia/Jakarta",
        "platform": prof["platform"],
        "tg_platform": ua_platform,
        "viewport_width": prof["viewport_width"],
        "viewport_height": prof["viewport_height"],
        "max_touch_points": 5,
        "device_pixel_ratio": 3,
    }
    return visitor_id, info


def generate_interaction(device_info):
    return {
        "pointer_type": "touch",
        "page_x_norm": round(random.uniform(0.3, 0.7), 4),
        "page_y_norm": round(random.uniform(0.3, 0.7), 4),
        "button_x_norm": round(random.uniform(0.35, 0.65), 4),
        "button_y_norm": round(random.uniform(0.35, 0.65), 4),
        "press_ms": random.randint(80, 250),
        "move_count": random.randint(0, 4),
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
        result = await client(
            RequestAppWebViewRequest(
                peer=InputPeerUser(user_id=bot.id, access_hash=bot.access_hash),
                app=app_req,
                platform="android",
                write_allowed=False,
            )
        )
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
        return {
            "initData": self.init_data,
            "start_param": self.start_param,
            "fingerprint": self.fp_id,
            **self.device_info,
        }

    def _claim_payload(self):
        interaction = generate_interaction(self.device_info)
        return {"initData": self.init_data, "interaction": interaction}

    async def auth(self):
        try:
            self.init_data, self.start_param = await get_init_data(
                self.session_path
            )
            if not self.init_data:
                return False, "Gagal ambil initData"
            return True, "OK"
        except Exception as e:
            return False, str(e)[:60]

    async def init_app(self):
        try:
            payload = self._payload_base()
            status_code, text = await http_post(
                f"{API_BASE}/init", payload, self.headers
            )
            if status_code == 401 or status_code == 403:
                return False, "Unauthorized/Blokir"
            if status_code != 200:
                return False, f"Init error {status_code}"
            data = json.loads(text)
            if data.get("status") != "success":
                return False, "Init gagal"
            user = data.get("user", {})
            self.saldo = user.get("balance", 0)
            self.cooldown_server = user.get("cooldown", 0)
            self.captcha_required = user.get("captcha_required", False)
            self.is_blocked = user.get("is_blocked", False)
            if self.is_blocked:
                return False, "AKUN BLOKIR"
            access = data.get("access", {})
            if access.get("mobile_only_blocked"):
                self.fp_id, self.device_info = generate_fingerprint("ios")
                self.headers["User-Agent"] = self.device_info["ua"]
                return await self.init_app()
            return True, "OK"
        except json.JSONDecodeError:
            return False, "Init response bukan JSON"
        except Exception as e:
            return False, str(e)[:50]

    async def solve_captcha(self):
        try:
            sc, text = await http_post(
                f"{API_BASE}/captcha/challenge",
                {"initData": self.init_data},
                self.headers,
            )
            if sc != 200:
                return False
            data = json.loads(text)
            challenge = data.get("challenge", {})
            challenge_id = challenge.get("challenge_id")
            prompt = challenge.get("prompt", "").lower()
            options = challenge.get("options", [])
            if not challenge_id or not options:
                return False
            answer_id = None
            for opt in options:
                label = opt.get("label", "").lower()
                emoji = opt.get("emoji", "")
                if label and label in prompt:
                    answer_id = opt.get("id")
                    break
            if not answer_id:
                for opt in options:
                    label = opt.get("label", "").lower()
                    emoji = opt.get("emoji", "").lower()
                    for word in prompt.split():
                        if len(word) > 2 and (word in label or word in emoji):
                            answer_id = opt.get("id")
                            break
                    if answer_id:
                        break
            if not answer_id and options:
                answer_id = random.choice(options).get("id")
            if not answer_id:
                return False
            sc2, text2 = await http_post(
                f"{API_BASE}/captcha/verify",
                {
                    "initData": self.init_data,
                    "challenge_id": challenge_id,
                    "answer": answer_id,
                },
                self.headers,
            )
            return sc2 == 200
        except Exception:
            return False

    async def _gigapubs_bypass(self, session_uid):
        try:
            tg_user = parse_tg_user(self.init_data)
            giga_headers = {
                "Content-Type": "application/json",
                "project-id": "5736",
                "User-Agent": self.device_info["ua"],
            }
            giga_user = {
                "user": tg_user,
                "platform": self.device_info["tg_platform"],
                "version": "10.0",
                "start_param": self.start_param,
            }
            await http_post(
                GIGA_V1,
                {
                    "method": "init",
                    "args": {
                        "user": giga_user,
                        "version": "v85",
                        "seconds": 5,
                    },
                },
                giga_headers,
            )
            await asyncio.sleep(0.5)
            await http_post(
                GIGA_V1,
                {
                    "method": "adShowed",
                    "args": {
                        "user": giga_user,
                        "placementId": "main",
                        "network": "gigapubs",
                        "rotationType": "fallback",
                        "showCounter": 0,
                        "transactionId": session_uid,
                        "version": "v85",
                        "seconds": 8,
                        "anyData": {},
                    },
                },
                giga_headers,
            )
            return True
        except Exception:
            return False

    async def _poll_balance(self, max_wait=15):
        old_saldo = self.saldo
        for _ in range(max_wait):
            await asyncio.sleep(1)
            ok, _ = await self.init_app()
            if not ok:
                continue
            if self.cooldown_server > 0 or self.saldo > old_saldo:
                return True
        return False

    async def klaim(self):
        if not self.init_data:
            return False, 0, "No initData"
        if self.captcha_required:
            if await self.solve_captcha():
                self.captcha_required = False
            else:
                return False, 60, "Captcha gagal"
        try:
            payload = self._claim_payload()
            sc, text = await http_post(
                f"{API_BASE}/claim", payload, self.headers
            )
            if sc == 428:
                if await self.solve_captcha():
                    self.captcha_required = False
                    await asyncio.sleep(1)
                    sc, text = await http_post(
                        f"{API_BASE}/claim", payload, self.headers
                    )
                else:
                    return False, 60, "Captcha 428 gagal"
            if sc != 200:
                return False, 60, f"HTTP {sc}"
            data = json.loads(text)
            status = data.get("status", "")
            if status == "success":
                self.saldo = data.get("new_balance", self.saldo)
                reward = data.get("reward", 0)
                cooldown = data.get("cooldown", 300)
                return True, cooldown, f"+{reward} sat"
            if status == "ad_required":
                session_uid = data.get("session_uid")
                provider = data.get("provider", "")
                reward_sats = data.get("reward_sats", 0)
                self.adexium_remaining = data.get("adexium_remaining", 0)
                adexium_reset = data.get("adexium_reset_at")
                if adexium_reset:
                    try:
                        self.adexium_reset_at = datetime.fromisoformat(
                            adexium_reset.replace("Z", "+00:00")
                        )
                    except Exception:
                        self.adexium_reset_at = None
                if not session_uid:
                    return False, 60, "No session_uid"
                if provider == "gigapubs":
                    bypass_ok = await self._gigapubs_bypass(session_uid)
                    if bypass_ok and await self._poll_balance(max_wait=15):
                        ok_init, _ = await self.init_app()
                        if ok_init:
                            return (
                                True,
                                self.cooldown_server or 300,
                                f"+{reward_sats} sat",
                            )
                    if self.adexium_remaining > 0:
                        await asyncio.sleep(random.uniform(2.5, 4.0))
                        sc2, text2 = await http_post(
                            f"{API_BASE}/claim/confirm",
                            {
                                "initData": self.init_data,
                                "session_uid": session_uid,
                            },
                            self.headers,
                        )
                        if sc2 == 200:
                            data2 = json.loads(text2)
                            if data2.get("status") == "success":
                                self.saldo = data2.get("new_balance", self.saldo)
                                return (
                                    True,
                                    data2.get("cooldown", 300),
                                    f"+{data2.get('reward', reward_sats)} sat",
                                )
                    return False, 120, "GigaPubs bypass gagal"
                if provider == "adexium":
                    await asyncio.sleep(random.uniform(2.5, 4.0))
                    sc2, text2 = await http_post(
                        f"{API_BASE}/claim/confirm",
                        {
                            "initData": self.init_data,
                            "session_uid": session_uid,
                        },
                        self.headers,
                    )
                    if sc2 == 200:
                        data2 = json.loads(text2)
                        if data2.get("status") == "success":
                            self.saldo = data2.get("new_balance", self.saldo)
                            return (
                                True,
                                data2.get("cooldown", 300),
                                f"+{data2.get('reward', reward_sats)} sat",
                            )
                    sc_fb, text_fb = await http_post(
                        f"{API_BASE}/claim/fallback",
                        {"initData": self.init_data},
                        self.headers,
                    )
                    if sc_fb == 200:
                        d_fb = json.loads(text_fb)
                        fb_uid = d_fb.get("session_uid")
                        if fb_uid and d_fb.get("provider") == "gigapubs":
                            bypass_ok = await self._gigapubs_bypass(fb_uid)
                            if bypass_ok and await self._poll_balance(max_wait=15):
                                ok_init, _ = await self.init_app()
                                if ok_init:
                                    return (
                                        True,
                                        self.cooldown_server or 300,
                                        f"+{reward_sats} sat",
                                    )
                    return False, 120, "Adexium + GigaPubs gagal"
            return False, 60, f"Status: {status}"
        except json.JSONDecodeError:
            return False, 60, "Bukan JSON"
        except Exception as e:
            return False, 60, f"Error: {str(e)[:40]}"

    async def daily_tasks(self):
        hasil = []
        try:
            sc, text = await http_post(
                f"{API_BASE}/tasks/telegram/list",
                {"initData": self.init_data},
                self.headers,
            )
            if sc == 200:
                data = json.loads(text)
                tasks = data.get("tasks", [])
                for task in tasks:
                    tid = task.get("id")
                    title = task.get("title", "?")
                    is_done = task.get("is_done", False)
                    is_claimed = task.get("is_claimed", False)
                    if is_done and not is_claimed and tid:
                        sc2, _ = await http_post(
                            f"{API_BASE}/tasks/telegram/claim",
                            {"initData": self.init_data, "task_id": tid},
                            self.headers,
                        )
                        if sc2 == 200:
                            hasil.append(f"Task: {title}")
        except Exception:
            pass
        return hasil

    async def cek_saldo(self):
        ok, msg = await self.init_app()
        return self.saldo


# ========== RIWAYAT HELPERS ==========
async def tambah_riwayat(akun, pesan):
    async with riwayat_lock:
        sekarang = datetime.now().strftime("%H:%M:%S")
        riwayat.append(f"{sekarang} | {akun} | {pesan}")
        if len(riwayat) > 100:
            riwayat.pop(0)
    await broadcast_event(
        {"type": "log", "data": {"time": sekarang, "account": akun, "message": pesan}}
    )


async def broadcast_event(event: dict):
    """Broadcast event ke semua WebSocket clients"""
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    ws_clients -= dead


def format_waktu(detik):
    if detik <= 0:
        return "-"
    if detik >= 3600:
        j = detik // 3600
        m = (detik % 3600) // 60
        return f"{j}j {m}m"
    elif detik >= 60:
        m = detik // 60
        d = detik % 60
        return f"{m}m {d}d"
    return f"{detik}d"


# ========== PEKERJA AKUN (Worker) ==========
async def pekerja_akun(info_akun, key):
    global total_berhasil, total_gagal

    bot = FBTC0Bot(info_akun)
    nama = info_akun["nama"]

    accounts_db[key]["status"] = "AUTH..."
    await broadcast_event({"type": "status_update", "data": get_all_status()})

    if key not in claim_ok:
        claim_ok[key] = 0
    if key not in claim_fail:
        claim_fail[key] = 0

    # 1. Auth
    accounts_db[key]["status"] = "AUTH..."
    await broadcast_event({"type": "status_update", "data": get_all_status()})
    ok, msg = await bot.auth()
    if not ok:
        accounts_db[key]["status"] = "GAGAL AUTH"
        total_gagal += 1
        claim_fail[key] += 1
        await tambah_riwayat(nama, f"Gagal auth: {msg}")
        simpan_status()
        await broadcast_event({"type": "status_update", "data": get_all_status()})
        return

    await tambah_riwayat(nama, "Auth OK")

    # 2. Init
    accounts_db[key]["status"] = "LOAD..."
    await broadcast_event({"type": "status_update", "data": get_all_status()})
    ok, msg = await bot.init_app()
    if not ok:
        accounts_db[key]["status"] = "GAGAL INIT"
        total_gagal += 1
        claim_fail[key] += 1
        await tambah_riwayat(nama, f"Init gagal: {msg}")
        simpan_status()
        await broadcast_event({"type": "status_update", "data": get_all_status()})
        return

    accounts_db[key]["saldo"] = bot.saldo
    await tambah_riwayat(nama, f"Saldo: {bot.saldo} sat")

    # 3. Captcha
    if bot.captcha_required:
        accounts_db[key]["status"] = "CAPTCHA..."
        await broadcast_event({"type": "status_update", "data": get_all_status()})
        await tambah_riwayat(nama, "Captcha detected, solving...")
        if await bot.solve_captcha():
            bot.captcha_required = False
            await tambah_riwayat(nama, "Captcha solved!")
        else:
            await tambah_riwayat(nama, "Captcha gagal, lanjut anyway")

    # 4. Cooldown
    if bot.cooldown_server > 0:
        await tambah_riwayat(nama, f"Cooldown {format_waktu(bot.cooldown_server)}")
        for t in range(bot.cooldown_server, 0, -1):
            if key not in accounts_db:
                return  # Account was deleted
            accounts_db[key]["status"] = format_waktu(t)
            if t % 3 == 0:
                await broadcast_event(
                    {"type": "status_update", "data": get_all_status()}
                )
            await asyncio.sleep(1)

    accounts_db[key]["status"] = "SIAP"
    await broadcast_event({"type": "status_update", "data": get_all_status()})

    # 5. Main claim loop
    while key in accounts_db:
        try:
            accounts_db[key]["status"] = "KLAIM..."
            await broadcast_event({"type": "status_update", "data": get_all_status()})

            berhasil, cooldown, pesan = await bot.klaim()

            if berhasil:
                total_berhasil += 1
                claim_ok[key] += 1
                accounts_db[key]["saldo"] = bot.saldo
                await tambah_riwayat(nama, f"OK {pesan} | Saldo: {bot.saldo}")
            else:
                for retry in range(3):
                    await tambah_riwayat(nama, f"Retry {retry + 1}/3 - {pesan}")
                    accounts_db[key]["status"] = f"RETRY {retry + 1}/3"
                    await broadcast_event(
                        {"type": "status_update", "data": get_all_status()}
                    )
                    await asyncio.sleep(3)
                    ok_r, _ = await bot.auth()
                    if ok_r:
                        berhasil, cooldown, pesan = await bot.klaim()
                        if berhasil:
                            break
                    else:
                        await tambah_riwayat(nama, f"Refresh gagal retry {retry + 1}")
                        await asyncio.sleep(2)
                        berhasil, cooldown, pesan = await bot.klaim()

                if berhasil:
                    total_berhasil += 1
                    claim_ok[key] += 1
                    accounts_db[key]["saldo"] = bot.saldo
                    await tambah_riwayat(
                        nama, f"OK {pesan} | Saldo: {bot.saldo}"
                    )
                else:
                    total_gagal += 1
                    claim_fail[key] += 1
                    await tambah_riwayat(nama, f"GAGAL - {pesan}")
                    if bot.adexium_reset_at and bot.adexium_remaining <= 0:
                        try:
                            now = datetime.now(timezone.utc)
                            diff = (bot.adexium_reset_at - now).total_seconds()
                            if diff > 0:
                                jam = int(diff // 3600)
                                menit = int((diff % 3600) // 60)
                                reset_str = (
                                    f"{jam}j {menit}m" if jam > 0 else f"{menit}m"
                                )
                                await tambah_riwayat(
                                    nama, f"Adexium reset: {reset_str}"
                                )
                        except Exception:
                            pass

            simpan_status()

            accounts_db[key]["status"] = "CEK..."
            await broadcast_event({"type": "status_update", "data": get_all_status()})
            await bot.cek_saldo()
            accounts_db[key]["saldo"] = bot.saldo

            sisa = max(cooldown, 60)
            sisa = min(sisa, 3600)

            for t in range(sisa, 0, -1):
                if key not in accounts_db:
                    return  # Account was deleted/removed
                accounts_db[key]["status"] = format_waktu(t)
                if t % 5 == 0:
                    await broadcast_event(
                        {"type": "status_update", "data": get_all_status()}
                    )
                await asyncio.sleep(1)

            ok_ref, _ = await bot.auth()
            if ok_ref:
                await bot.init_app()
                if bot.captcha_required:
                    await bot.solve_captcha()
                    bot.captcha_required = False

        except asyncio.CancelledError:
            break
        except Exception as e:
            await tambah_riwayat(nama, f"Error: {str(e)[:50]}")
            await asyncio.sleep(10)


# ========== STATUS HELPERS ==========
def get_all_status():
    total = len(accounts_db)
    aktif = sum(
        1
        for d in accounts_db.values()
        if any(k in d["status"] for k in ["SIAP", "KLAIM", "CEK", "RETRY"])
    )
    akun_list = []
    for key, d in accounts_db.items():
        akun_list.append(
            {
                "id": key,
                "nama": d["nama"],
                "phone": d.get("phone", d["nama"]),
                "saldo": d["saldo"],
                "status": d["status"],
                "ok": claim_ok.get(key, 0),
                "fail": claim_fail.get(key, 0),
                "running": key in workers and not workers[key].done(),
            }
        )
    return {
        "total": total,
        "aktif": aktif,
        "total_ok": total_berhasil,
        "total_fail": total_gagal,
        "accounts": akun_list,
        "riwayat": list(riwayat[-20:]),
    }


def muat_status():
    if FILE_STATUS.exists():
        try:
            with open(FILE_STATUS, "r") as f:
                data = json.load(f)
            for item in data:
                key = None
                for v in item.values():
                    if isinstance(v, str) and len(v) > 3:
                        key = v
                        break
                if key:
                    claim_ok[key] = int(item.get("ok", 0))
                    claim_fail[key] = int(item.get("fail", 0))
        except Exception:
            pass


def muat_akun():
    if FILE_AKUN.exists():
        try:
            with open(FILE_AKUN, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def simpan_status():
    data = []
    for key in accounts_db:
        info = accounts_db[key]
        data.append(
            {
                info["nama"]: key,
                "ok": str(claim_ok.get(key, 0)),
                "fail": str(claim_fail.get(key, 0)),
            }
        )
    try:
        with open(FILE_STATUS, "w") as f:
            json.dump(data, f, indent=4)
    except Exception:
        pass


def simpan_akun():
    akun_list = []
    for key, d in accounts_db.items():
        akun_list.append(
            {
                "session_path": d["session_path"],
                "nama": d["nama"],
                "phone": d.get("phone", d["nama"]),
            }
        )
    try:
        with open(FILE_AKUN, "w") as f:
            json.dump(akun_list, f, indent=4)
    except Exception:
        pass


# ========== API ROUTES ==========
@app.get("/")
async def serve_dashboard():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/status")
async def api_status():
    return get_all_status()


@app.post("/api/upload")
async def upload_session(file: UploadFile = File(...)):
    """Upload .session file dan tambah akun baru"""
    if not file.filename.endswith(".session"):
        raise HTTPException(400, "File harus .session")

    # Baca file content
    content = await file.read()
    nama = file.filename.replace(".session", "")

    # Simpan ke sessions/
    session_filename = f"{nama}.session"
    session_path = SESSIONS_DIR / session_filename
    with open(session_path, "wb") as f:
        f.write(content)

    # Cek apakah sudah ada
    key = str(session_path)
    if key in accounts_db:
        # Update session file
        accounts_db[key]["session_path"] = str(session_path)
        await tambah_riwayat(nama, "Session file updated")
    else:
        # Tambah akun baru
        accounts_db[key] = {
            "nama": nama,
            "phone": nama,
            "session_path": str(session_path),
            "status": "IDLE",
            "saldo": 0,
        }
        await tambah_riwayat(nama, "Akun ditambahkan")

    simpan_akun()
    return {
        "ok": True,
        "message": f"Akun '{nama}' berhasil ditambahkan",
        "data": get_all_status(),
    }


@app.post("/api/account/start")
async def start_account(payload: dict):
    """Mulai bot untuk satu akun"""
    account_id = payload.get("id")
    if not account_id or account_id not in accounts_db:
        raise HTTPException(404, "Akun tidak ditemukan")

    if account_id in workers and not workers[account_id].done():
        return {"ok": False, "message": "Akun sudah berjalan"}

    info = accounts_db[account_id]
    workers[account_id] = asyncio.create_task(
        pekerja_akun(
            {
                "session_path": info["session_path"],
                "nama": info["nama"],
                "phone": info.get("phone", info["nama"]),
            },
            account_id,
        )
    )
    await tambah_riwayat(info["nama"], "Bot dimulai")
    return {"ok": True, "message": f"Bot '{info['nama']}' dimulai"}


@app.post("/api/account/stop")
async def stop_account(payload: dict):
    """Hentikan bot untuk satu akun"""
    account_id = payload.get("id")
    if not account_id or account_id not in accounts_db:
        raise HTTPException(404, "Akun tidak ditemukan")

    if account_id in workers and not workers[account_id].done():
        workers[account_id].cancel()
        accounts_db[account_id]["status"] = "STOPPED"
        await tambah_riwayat(accounts_db[account_id]["nama"], "Bot dihentikan")
    else:
        accounts_db[account_id]["status"] = "IDLE"

    return {"ok": True, "message": "Bot dihentikan"}


@app.post("/api/start-all")
async def start_all():
    """Mulai semua akun yang IDLE"""
    started = 0
    for key, info in accounts_db.items():
        if key not in workers or workers[key].done():
            workers[key] = asyncio.create_task(
                pekerja_akun(
                    {
                        "session_path": info["session_path"],
                        "nama": info["nama"],
                        "phone": info.get("phone", info["nama"]),
                    },
                    key,
                )
            )
            started += 1
    await tambah_riwayat("SYSTEM", f"Start all: {started} akun")
    return {"ok": True, "started": started}


@app.post("/api/stop-all")
async def stop_all():
    """Hentikan semua bot"""
    stopped = 0
    for key in list(workers.keys()):
        if not workers[key].done():
            workers[key].cancel()
            if key in accounts_db:
                accounts_db[key]["status"] = "STOPPED"
            stopped += 1
    await tambah_riwayat("SYSTEM", f"Stop all: {stopped} akun")
    return {"ok": True, "stopped": stopped}


@app.delete("/api/account/{account_id}")
async def delete_account(account_id: str):
    """Hapus akun"""
    if account_id not in accounts_db:
        raise HTTPException(404, "Akun tidak ditemukan")

    # Stop worker
    if account_id in workers and not workers[account_id].done():
        workers[account_id].cancel()

    nama = accounts_db[account_id]["nama"]
    session_path = accounts_db[account_id].get("session_path", "")

    # Hapus session file
    if session_path and os.path.exists(session_path):
        try:
            os.remove(session_path)
        except Exception:
            pass

    del accounts_db[account_id]
    claim_ok.pop(account_id, None)
    claim_fail.pop(account_id, None)
    workers.pop(account_id, None)

    simpan_akun()
    simpan_status()
    await tambah_riwayat("SYSTEM", f"Akun '{nama}' dihapus")
    return {"ok": True, "message": f"Akun '{nama}' dihapus"}


@app.post("/api/account/rename")
async def rename_account(payload: dict):
    """Rename akun"""
    account_id = payload.get("id")
    new_name = payload.get("name", "").strip()
    if not account_id or account_id not in accounts_db:
        raise HTTPException(404, "Akun tidak ditemukan")
    if not new_name:
        raise HTTPException(400, "Nama tidak boleh kosong")

    old_name = accounts_db[account_id]["nama"]
    accounts_db[account_id]["nama"] = new_name
    accounts_db[account_id]["phone"] = new_name
    simpan_akun()
    await tambah_riwayat("SYSTEM", f"Rename: {old_name} -> {new_name}")
    return {"ok": True, "message": f"Rename ke '{new_name}'"}


# ========== WEBSOCKET ==========
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        # Send current status on connect
        await ws.send_json(
            {"type": "init", "data": get_all_status()}
        )
        # Keep alive
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=30)
                # Client ping
                if data == "ping":
                    await ws.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)


# ========== STARTUP ==========
@app.on_event("startup")
async def startup():
    muat_status()
    akun_list = muat_akun()
    for acc in akun_list:
        session_path = acc.get("session_path", "")
        if session_path and os.path.exists(session_path):
            accounts_db[session_path] = {
                "nama": acc.get("nama", "?"),
                "phone": acc.get("phone", acc.get("nama", "?")),
                "session_path": session_path,
                "status": "IDLE",
                "saldo": 0,
            }


# ========== RAILWAY / UVICORN ==========
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)