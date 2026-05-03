# 🎯 Webook Bot v4.0 — Hydra Seat Engine

بوت تيليجرام احترافي لحجز تذاكر [webook.com](https://webook.com) — مصمّم للعمل 24/7 على Render Free مع تكامل عميق مع خرائط seats.io.

> ⚡ تحديث v4: نظام "قنّاص الثواني" و "كلمات المراقبة" حُذفا. وُلِد محرك **Hydra** للتعامل مع seats.io: اكتشاف تلقائي للبلوكات، حجز مقاعد متجاورة، تنقّل ذكي بين البلوكات الاحتياطية، توسّع هندسي تلقائي، ووضع ترقّب مدفوع بأحداث WebSocket.

---

## 🔬 المعمارية

```
B2/
├── main.py                       FastAPI + Telegram dispatcher + lifespan
├── Dockerfile                    Playwright base (chromium pre-installed)
├── render.yaml                   نشر Docker على Render Free / Frankfurt
├── requirements.txt
└── app/
    ├── core/
    │   ├── config.py             ENV + DB-backed bot_settings (هجين)
    │   ├── db.py                 PostgreSQL / SQLite abstraction
    │   └── storage.py            CRUD: accounts, events, bookings,
    │                              event_blocks, drop_watchers, seat_maps,
    │                              bot_settings
    ├── bot/
    │   ├── handlers.py           dispatcher (link → blocks → qty → book)
    │   ├── keyboards.py          inline keyboards + blocks_picker
    │   └── …
    ├── services/
    │   ├── booking_http.py       محرك الحجز HTTP-direct
    │   ├── booking_orchestrator  التوازي عبر الحسابات
    │   ├── block_analyzer.py     ⭐ extract_blocks + adjacency +
    │   │                          geometric_neighbors
    │   ├── drop_watcher.py       ⭐ WS multiplexer event-driven sniper
    │   ├── seat_summarizer.py    ⭐ تلخيص ذكي: "CAT 1 - S block 5 (117-121)"
    │   ├── seatsio_client.py     SeatCloud REST + WS + hold tokens
    │   ├── seatsio_runtime.py    prewarm cache (rendering_info + statuses)
    │   ├── seatsio_token_fetcher  استخراج workspace_key من frontend bundle
    │   └── …
    └── web/admin.py              لوحة إدارة كاملة /admin
```

---

## 🚀 سير العمل (User Flow)

1. **الإدخال**: المستخدم يرسل رابط فعالية أو يختار من القائمة.
2. **اكتشاف seats.io**: البوت يستخرج `event_key` ويجلب `rendering_info` ويُحضّرها في `seat_maps` cache (قابلة لإعادة الاستخدام).
3. **اختيار البلوكات**: قائمة تفاعلية بكل البلوكات + عدد المتاح/الإجمالي. المستخدم يضغط ⭐ لتحديد الرئيسي ثم يضيف احتياطية بالترتيب (S1, S2, …).
4. **خوارزمية الحجز**:
   - Adjacency: مقاعد متجاورة لكل حساب على نفس الصف
   - Auto-fallback: رئيسي → احتياطي 1 → احتياطي 2 → …
   - Geometric expansion: عند استنفاد المختار، يحسب أقرب البلوكات بـ Euclidean distance ويُكمل
   - Drop watching: عند امتلاء الخريطة، يدخل وضع ترقّب مدفوع بـ WebSocket
5. **التلخيص**: تجميع المقاعد في صيغة `CAT 1 - S block 5 (117-121)` بدلاً من سرد كل مقعد.

---

## 🔧 Hydra Seat Engine — طبقات

| الطبقة | الوظيفة |
|---|---|
| **A — Bundle Sentinel** | يتابع `index-*.js` ويستخرج `workspace_key` ديناميكياً |
| **B — Seat Map Cache** | يحفظ `rendering_info` في PostgreSQL لإعادة الاستخدام عبر الجلسات |
| **C — WS Multiplexer** | اتصال WebSocket واحد لكل `event_key` يُوزّع الأحداث على كل المراقبين (يخفض ذاكرة Render Free) |
| **D — Predictive Drop Sniper** | عند `objectStatusChanged → free` يُنفّذ hold فوراً (~80ms) |

---

## 📦 النشر على Render

1. اربط هذا المستودع بحساب Render
2. اضبط متغيرات البيئة الحرجة فقط (يدوياً، **لا تُحفظ في Git**):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `DATABASE_URL` (PostgreSQL)
   - `CAPTCHA_API_KEY` (2captcha)
   - `ADMIN_PASSWORD`
3. باقي المتغيرات لها قيم افتراضية في `render.yaml`، أو يمكن تعديلها live من **/admin** أو من قائمة "⚙️ الإعدادات" في البوت.

---

## ⚙️ نموذج الإعدادات الهجين

| النوع | المكان | السبب |
|---|---|---|
| أسرار حرجة (Tokens, DB) | Render فقط | محمية، sealed |
| متغيرات تشغيل (PAYMENT, INTERVALS) | DB (`bot_settings`) + ENV fallback | تعديل live بدون redeploy |
| دفع موحّد لكل الحسابات | DB → ENV → `credit_card` | افتراضي |

ترتيب الحلّ: `os.environ` → `bot_settings` (DB) → default.

---

## 🧪 Smoke Test

```bash
python smoke_test.py
# → SMOKE_OK ✅ — v4 Hydra engine passes all checks
```

يفحص: config, block analyzer, geometric neighbors, find_seats_with_fallback, seat summarizer, bot settings round-trip.

---

## 🔗 Endpoints

| | |
|---|---|
| `GET  /` | Dashboard |
| `GET  /health` | فحص (مع backend & accounts ready) |
| `GET  /ping` | Keep-alive |
| `GET  /stats` | إحصائيات JSON |
| `POST /telegram/webhook` | تحديثات تيليجرام |
| `GET  /admin/` | لوحة إدارة (محمية بـ ADMIN_PASSWORD) |

---

## 📝 Changelog

- **v4.0 (2026-05)** — Hydra Seat Engine; حذف Sniper + Watch Keywords; إضافة block picker + drop watcher + smart summarizer + bot_settings + per-event seat_maps cache.
- v3.x — البنية الأصلية (HTTP-direct + Playwright fallback + sniper_loop + watch_keywords).
