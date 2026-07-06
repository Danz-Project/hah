# FBTC0 BTC Faucet Bot - Web Version

Versi website dari bot FBTC0 untuk deploy di Railway. Tinggal upload file `.session` Telegram dan mulai auto-claim.

## Cara Deploy ke Railway

1. Push repository ini ke GitHub
2. Buka [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Pilih repository ini
4. Railway akan otomatis detect Python dan install dependencies
5. Selesai! Buka URL yang diberikan Railway

## Cara Pakai

1. Buka website
2. Klik **Upload Session** → pilih file `.session` Telethon
3. Klik tombol **Start** pada akun, atau **Start All**
4. Monitor status dan log secara real-time

## Fitur

- Upload session file (.session) via web
- Real-time status dashboard (WebSocket)
- Start/Stop per akun atau semua sekaligus
- Auto-claim BTC faucet
- Auto captcha solver
- GigaPubs & Adexium bypass
- Rename & hapus akun
- Riwayat log real-time
- Persistent data (akun.json + status.json)

## Environment Variables (Optional)

- `PORT` - Port server (default: 8000, Railway auto-set)