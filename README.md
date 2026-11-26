# Live SMS only (group only)

Purane matrix bot se **sirf live SMS forwarding** nikal kar ye clean version banaya gaya hai.

Kya hai:
- Matrix SMS Report (`/api/sms`) ko har `POLL_SEC` second me padhta hai.
- Sirf **nayi** entries Telegram group (`SMS_LIVE_CHAT`) me bhejta hai (boot par purani rows skip).
- Card me: client, range, CLI, number, SMS text, time (IST, 12-hour).
- Premium custom emoji (sample se liye gaye emoji-id): `PREMIUM_EMOJI=0` karne se plain emoji, reject hone par auto-fallback.
- Har **1–2 minute** random self-ping (boot par turant pehla ping): Render free instance idle spin-down nahi hota.
- **Restart-safe:** boot ke baad purani rows baseline me skip hoti hain, lekin boot se **30 second pehle tak** ki nayi SMS bhi bhej di jaati hain (`RESTART_CATCHUP_SEC`) — restart gap me SMS kho nahi jaati.

Kya **nahi** hai (sab hata diya):
- **DM bilkul nahi** — koi DM reply nahi, username prompt nahi, subscription nahi, warning nahi, `getUpdates` nahi, `getChatMember` nahi. Bot sirf group me bhejta hai.
- payout (`MY PAYOUT` / `CLIENT PAYOUT`) — code me kahi payout field read nahi hoti
- `/update`, stats, leaderboard, announcements
- CLI hunt / CLI lookup / mirror / GitHub backup
- AI, disk cache, purane patches/tests ke files

## Speed / no-storage changes

- **Koi disk storage nahi:** in-memory dedupe sirf `_seen` (4000 keys, 24h TTL). DM state, block list, pending list sab code se hataye gaye. Process restart par baseline dobara set hota hai.
- **Speed:** `POLL_SEC=2`, `SEND_DELAY_SEC=0.2`, panel timeout `(3,12)s`, Telegram timeout `(2,6)s`. Ek hi `requests.Session` panel ke liye aur ek Telegram ke liye (connection reuse).
- **Stable:** transient network / 5xx par 3 retries + exponential backoff. Session expire ya panel 5xx par auto re-login. Telegram 429 par `retry_after` follow. 400 (emoji/parse) par turant plain-text fallback — retry wait nahi.
- **Group-only:** `getUpdates` loop hata diya gaya, isliye duplicate-instance `409 Conflict` errors aur nahi aate.
- **Auto-start:** Render service restart/deploy par `python bot.py` khud chalta hai; health server + self-ping + watcher sab thread me auto-start. Koi manual command nahi.

## Render deployment notes

- Repo: `https://github.com/00subh02/alya-matrix-live-sms` (public). Secrets repo me nahi — sirf Render env vars me hain. `.env` gitignore hai.
- Render Web Service: build `pip install -r requirements.txt`, start `python bot.py`, health check `/health`.
- `/health` → `ok` bot ke andar hi chalne wale HTTP server se. Bot khud **boot par turant**, phir **random 1–2 minute** (`SELF_PING_MIN_SEC`/`SELF_PING_MAX_SEC`) me `SELF_PING_URL` ko ping karta hai. Bahar se bhi do ping backup ke liye chal rahe hain (Windows Task `MatrixSMS Render Keepalive` + OpenCode automation).
- Free quota: 750 instance-hours / month. 31 din × 24h = 744h, yani keepalive ke saath poora month technically fit hota hai (6h margin). Render free instance ko kabhi bhi restart kar sakta hai — restart par baseline dobara set, group flow waisa hi.
- Ephemeral filesystem: restart/deploy/spin-down par local files lost. Bot koi long-term file nahi rakhta, isliye "storage full" ka issue nahi.

## Run

```powershell
py -m pip install -r requirements.txt
py bot.py
```

Is PC par `python` PATH me nahi hai, yahan se chalao:

```powershell
& 'C:\Users\zyren\Documents\Default Project\sms-monitor-bot\.venv\Scripts\python.exe' bot.py
```

Config `.env` me hai (secret file — commit na karein). Destination `SMS_LIVE_CHAT=-1004451914143`.

Agar Telegram flood (429) aaye to `SEND_DELAY_SEC` badha dein (default 0.2s). Panel slow ho to `POLL_SEC` badha dein (default 2s, minimum 1s). Premium custom emoji reject ho jaaye to poora code plain emoji par automatically switch ho jata hai (restart ki zarurat nahi).

Purana 282 KB wala bot aur uske saare patch/test files waise hi backup me rehte hain — kuch nahi mitaya gaya.
