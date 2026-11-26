# Live SMS only

Purane matrix bot se **sirf live SMS forwarding** nikal kar ye clean version banaya gaya hai.

Kya hai:
- Matrix SMS Report (`/api/sms`) ko har `POLL_SEC` second me padhta hai.
- Sirf **nayi** entries Telegram chat/group me bhejta hai (boot par purani rows skip).
- Card me: client, range, CLI, number, SMS text, time (IST, 12-hour).
- Premium custom emoji (sample se liye gaye emoji-id): `PREMIUM_EMOJI=0` karne se plain emoji, reject hone par auto-fallback.
- **DM access control (sirf DM ke liye):** jab koi user bot ko DM kare, bot `getChatMember` se uska ID group (`GROUP_CHAT_ID`) me check karta hai.
  - Group me nahi → user ko **koi reply nahi**, ID memory block-list me, aur group me warning: "Random user ne DM kiya…".
  - Group me hai → bot puchta hai: "Boss, aapka ID allowed list me hai ✅ Apna client username batayenge? (jaise DEVIL_LUFFY)".
  - User client name bheje (startup par ek baar liye gaye roster se verify) → `chat_id -> client` memory me save, reply: "Apka custom ID add ho gaya".
  - Uske baad sirf usi client ki nayi SMS us DM chat me jaati hain. **Group me purana behaviour bilkul same** (sab clients).
  - **DM card bilkul waisa hi** jaisa DEVIL group me — dono `card_text()` se render hote hain (same premium emoji, fields, HTML, time format).
  - `/cancel` se subscription hata sakte hain. `DM_FEATURE=0` se poora DM feature band.
  - Bot API me "block user" ka method nahi hota — isliye unknown ID ko ignore kiya jata hai.

## Speed / no-storage changes

- **Koi disk storage nahi:** `dm_subs.json` / `blocked_ids.json` hata diye — sab kuch pure in-memory functions me (process restart par state reset, by design). Memory caps: `_seen` 4000 keys (24h TTL), `_blocked` 5000, `_pending` 1000. Ek process ~10-20 MB RAM; Render free (512 MB) me kabhi full nahi hota.
- **Speed:** `POLL_SEC=2`, `SEND_DELAY_SEC=0.2`, `getUpdates` long-poll 1s, panel timeout `(3,12)s`, Telegram timeout `(2,6)s`. Ek hi `requests.Session` panel ke liye aur ek Telegram ke liye (connection reuse).
- **Stable:** transient network / 5xx par 3 retries + exponential backoff. Session expire ya panel 5xx par auto re-login. Telegram 429 par `retry_after` follow. 400 (emoji/parse) par turant plain-text fallback — retry wait nahi. doosra instance aa jaye (409) to loop 1s me dobara try.
- **Late reply fix:** allow-check har DM par nahi — pehli baar `getChatMember`, baad me memory. Client roster bhi startup par sirf ek baar fetch (fail ho to agle DM par dobara try), phir memory.
- **Active device/session growth:** panel par **ek hi login** startup par, usi `requests.Session` se SMS poll + roster dono. Baar-baar naya session nahi banta. (Process restart par naya login hoga — usse rokne ke liye cookie disk me save karna padega, jo aapne nahi chaaha; isliye process ko chalte rehna hi solution hai.)

Kya nahi hai (sab hata diya):
- payout (`MY PAYOUT` / `CLIENT PAYOUT`) — code me kahi payout field read nahi hoti
- `/update`, stats, leaderboard, announcements
- CLI hunt / CLI lookup / mirror / GitHub backup
- AI, disk cache, purane patches/tests ke files

## Render deployment notes

- Repo: `alya-matrix-live-sms` (GitHub). Secrets repo me nahi — sirf Render env vars me hain. `.env` gitignore hai.
- Render Web Service: build `pip install -r requirements.txt`, start `python bot.py`, health check `/health`.
- `/health` → `ok` bot ke andar hi chalne wale HTTP server se. Bot khud **random 1–7 minute** (`SELF_PING_MIN_SEC`/`SELF_PING_MAX_SEC`) me `SELF_PING_URL` ko ping karta hai — Render ka 15-min idle spin-down reset hota hai. Bahar se bhi do ping chal rahe hain (Windows Task `MatrixSMS Render Keepalive` + OpenCode automation) taaki spin-down ke baad instance wake ho. Isliye free par bhi bot off nahi hota.
- Free quota: 750 instance-hours / month. 31 din × 24h = 744h, yani keepalive ke saath poora month technically fit hota hai (6h margin). Render free instance ko kabhi bhi restart kar sakta hai — restart par DM subscriptions reset, baseline dobara set, group flow waisa hi.
- Ephemeral filesystem: restart/deploy/spin-down par local files lost. Bot koi long-term file nahi rakhta, isliye "storage full" ka issue nahi. `pip install requests` boot par ~ek baar hota hai (chhota transient cache).

## Run

```powershell
py -m pip install -r requirements.txt
py bot.py
```

Is PC par `python` PATH me nahi hai, yahan se chalao:

```powershell
& 'C:\Users\zyren\Documents\Default Project\sms-monitor-bot\.venv\Scripts\python.exe' bot.py
```

Config `.env` me hai (secret file — commit na karein). Destination `SMS_LIVE_CHAT=-1004451914143`, jo purane bot ka configured group tha. Badalne ke liye `.env` me numeric chat id likhein.

Agar Telegram flood (429) aaye to `SEND_DELAY_SEC` badha dein (default 0.2s). Panel slow ho to `POLL_SEC` badha dein (default 2s, minimum 1s). Premium custom emoji reject ho jaaye to poora code plain emoji par automatically switch ho jata hai (restart ki zarurat nahi).

Purana 282 KB wala bot aur uske saare patch/test files waise hi backup me rehte hain — kuch nahi mitaya gaya.
