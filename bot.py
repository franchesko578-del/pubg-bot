"""
PUBG Mobile natija boti - BITTA FAYL.

O'RNATISH:  pip install aiogram==3.13.1 httpx aiohttp pillow

SUPABASE SQL (SQL Editor'ga bir marta tashlang):
create table if not exists state (chat_id bigint primary key, map_no int not null default 1);
create table if not exists slots (chat_id bigint, nick_norm text, nick text, slot int, primary key (chat_id, nick_norm));
create table if not exists kills (id bigserial primary key, chat_id bigint, map_no int, nick text, nick_norm text,
  slot int, place int, kills int default 0, unique (chat_id, map_no, nick_norm));

ENV O'ZGARUVCHILAR: BOT_TOKEN, ADMIN_IDS, SUPABASE_URL, SUPABASE_KEY (service_role),
  GROQ_KEYS, GEMINI_KEYS, OPENROUTER_KEYS (vergul bilan, kamida bittasi)
"""
import asyncio, base64, hashlib, html, io, json, os, re
from collections import Counter, defaultdict
from difflib import SequenceMatcher

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.enums import ParseMode
from aiohttp import web

try:
    from PIL import Image
except ImportError:
    Image = None

# ---------------- SOZLAMALAR ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]
WAIT = float(os.getenv("WAIT_SECONDS", "6"))  # oxirgi rasmdan keyin kutish


def keys(name):
    return [k.strip() for k in os.getenv(name, "").split(",") if k.strip()]


PROVIDERS = (
    [("openai", "https://api.groq.com/openai/v1", k,
      os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")) for k in keys("GROQ_KEYS")]
    + [("gemini", "", k, os.getenv("GEMINI_MODEL", "gemini-2.0-flash")) for k in keys("GEMINI_KEYS")]
    + [("openai", "https://openrouter.ai/api/v1", k,
        os.getenv("OPENROUTER_MODEL", "meta-llama/llama-4-maverick:free")) for k in keys("OPENROUTER_KEYS")]
)
_rot = 0
http = httpx.AsyncClient(timeout=30)

PROMPT = """Bu PUBG Mobile skrinshoti. Turini aniqla va FAQAT JSON qaytar.
A tur: lobby ro'yxati ("Remaining / Team"), har jamoa blokida katta slot raqami va o'yinchi nomlari.
 {"type":"A","teams":[{"slot":3,"players":["nick1","nick2"]}]}
B tur: yakuniy natija (PUBG MOBILE logosi, o'rinlar 1,2,3...), har o'yinchida "N elimination(s)".
 {"type":"B","teams":[{"place":1,"players":[{"nick":"nick1","kills":1}]}]}
Qoida: "0 eliminations"=0. O'qib bo'lmasa kills=null. Nicklarni aynan yozilgandek ko'chir.
Yarim kesilgan jamoalarni ham ko'rinib turgan qismi bilan yoz. Hech qanday izoh yozma."""


# ---------------- SUPABASE (REST) ----------------
async def db(method, table, params=None, body=None, upsert=None):
    h = {"apikey": SB_KEY, "Content-Type": "application/json"}
    p = dict(params or {})
    if upsert:
        h["Prefer"] = "resolution=merge-duplicates,return=minimal"
        p["on_conflict"] = upsert
    r = await http.request(method, f"{SB_URL}/rest/v1/{table}", params=p, json=body, headers=h)
    r.raise_for_status()
    return r.json() if method == "GET" else None


async def get_map(chat):
    rows = await db("GET", "state", {"chat_id": f"eq.{chat}"})
    if rows:
        return rows[0]["map_no"]
    await db("POST", "state", body={"chat_id": chat, "map_no": 1})
    return 1


# ---------------- VISION AI ----------------
def prep(data: bytes) -> bytes:
    if not Image:
        return data
    im = Image.open(io.BytesIO(data)).convert("RGB")
    if im.width > 1280:
        im = im.resize((1280, int(im.height * 1280 / im.width)))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=82)
    return out.getvalue()


async def call(p, b64):
    kind, base, key, model = p
    if kind == "gemini":
        r = await http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": key},
            json={"contents": [{"parts": [{"text": PROMPT},
                                          {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}],
                  "generationConfig": {"responseMimeType": "application/json", "temperature": 0}})
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    r = await http.post(
        f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "temperature": 0, "response_format": {"type": "json_object"},
              "messages": [{"role": "user", "content": [
                  {"type": "text", "text": PROMPT},
                  {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def parse(txt):
    txt = txt.strip()
    s, e = txt.find("{"), txt.rfind("}")
    d = json.loads(txt[s:e + 1])
    assert d.get("type") in ("A", "B") and isinstance(d.get("teams"), list)
    return d


async def read_image(data: bytes):
    """Provayderlarni ketma-ket sinaydi (zaxira zanjiri)."""
    global _rot
    b64 = base64.b64encode(data).decode()
    n = len(PROVIDERS)
    start = _rot % n
    _rot += 1
    for i in range(n):
        try:
            return parse(await asyncio.wait_for(call(PROVIDERS[(start + i) % n], b64), 25))
        except Exception as ex:
            print("provayder xato:", repr(ex)[:150])
    return None


# ---------------- YORDAMCHI ----------------
def norm(s):
    return re.sub(r"[^a-z0-9а-яё]", "", str(s).lower())


def match(nick_norm, known):
    """known: {norm: (slot, nick)} -> (slot, norm, nick) yoki None"""
    if nick_norm in known:
        return known[nick_norm][0], nick_norm, known[nick_norm][1]
    if len(nick_norm) <= 3:
        return None
    best, score = None, 0.85
    for k, (slot, nick) in known.items():
        r = SequenceMatcher(None, nick_norm, k).ratio()
        if r >= score:
            best, score = (slot, k, nick), r
    return best


def to_int(x):
    try:
        return int(x)
    except Exception:
        return None


# ---------------- ASOSIY ISHLOV ----------------
async def process(bot: Bot, chat: int, file_ids: list, status_id: int):
    map_no = await get_map(chat)
    sem = asyncio.Semaphore(6)

    async def one(fid):
        async with sem:
            buf = io.BytesIO()
            await bot.download(fid, destination=buf)
            return await read_image(prep(buf.getvalue()))

    # dublikat rasmlarni hash orqali olib tashlash
    seen, uniq = set(), []
    for fid in file_ids:
        buf = io.BytesIO()
        await bot.download(fid, destination=buf)
        h = hashlib.md5(buf.getvalue()).hexdigest()
        if h not in seen:
            seen.add(h)
            uniq.append(fid)

    results = [r for r in await asyncio.gather(*[one(f) for f in uniq]) if r]
    warns = []
    if len(results) < len(uniq):
        warns.append(f"⚠️ {len(uniq) - len(results)} ta rasm o'qilmadi, qayta yuboring.")

    # A turlar -> slotlar bazasi
    slot_rows = {}
    for r in results:
        if r["type"] == "A":
            for t in r["teams"]:
                slot = to_int(t.get("slot"))
                for nick in t.get("players", []):
                    if slot and norm(nick):
                        slot_rows[norm(nick)] = {"chat_id": chat, "nick_norm": norm(nick),
                                                 "nick": str(nick), "slot": slot}
    if slot_rows:
        await db("POST", "slots", body=list(slot_rows.values()), upsert="chat_id,nick_norm")

    # B turlar -> birlashtirish (to'liqroq versiya yutadi)
    teams = {}
    for r in results:
        if r["type"] == "B":
            for t in r["teams"]:
                place = to_int(t.get("place"))
                pl = [p for p in t.get("players", []) if isinstance(p, dict) and p.get("nick")]
                if not place:
                    continue
                sc = (sum(1 for p in pl if p.get("kills") is not None), len(pl))
                if place not in teams or sc > teams[place][0]:
                    teams[place] = (sc, pl)

    known = {x["nick_norm"]: (x["slot"], x["nick"])
             for x in await db("GET", "slots", {"chat_id": f"eq.{chat}", "select": "nick_norm,nick,slot"})}

    rows = []
    for place, (_, pl) in teams.items():
        found = []
        for p in pl:
            m = match(norm(p["nick"]), known)
            found.append(m)
        votes = Counter(m[0] for m in found if m)
        team_slot = votes.most_common(1)[0][0] if votes else None
        if team_slot is None:
            warns.append(f"⚠️ Top {place} jamoasi slotini topa olmadim. Lobby rasmini yuboring.")
        for p, m in zip(pl, found):
            rows.append({"chat_id": chat, "map_no": map_no,
                         "nick": m[2] if m else str(p["nick"]),
                         "nick_norm": m[1] if m else norm(p["nick"]),
                         "slot": team_slot, "place": place,
                         "kills": to_int(p.get("kills")) or 0})
    if rows:
        uniq_rows = {r["nick_norm"]: r for r in rows}
        await db("POST", "kills", body=list(uniq_rows.values()), upsert="chat_id,map_no,nick_norm")

    await send_results(bot, chat, map_no, warns)
    try:
        await bot.delete_message(chat, status_id)
    except Exception:
        pass


async def send_results(bot, chat, map_no, warns=()):
    allrows = await db("GET", "kills", {"chat_id": f"eq.{chat}", "select": "*"})
    if not allrows:
        await bot.send_message(chat, "Natija topilmadi.")
        return
    # 1-blok: joriy karta o'rinlari, kill = barcha kartalar yig'indisi (slot bo'yicha)
    slot_kills = defaultdict(int)
    for r in allrows:
        if r["slot"]:
            slot_kills[r["slot"]] += r["kills"]
    cur = defaultdict(list)
    for r in allrows:
        if r["map_no"] == map_no:
            cur[r["place"]].append(r)
    lines1 = []
    for place in sorted(cur):
        slot = next((r["slot"] for r in cur[place] if r["slot"]), None)
        k = slot_kills[slot] if slot else sum(r["kills"] for r in cur[place])
        lines1.append(f"{slot if slot else '?'} slot - Top {place} - {k} ta kill")

    # 2-blok: Top 5 fraggers
    agg = {}
    for r in allrows:
        a = agg.setdefault(r["nick_norm"], {"nick": r["nick"], "k": 0, "m": 0, "p": 99})
        a["k"] += r["kills"]
        if r["map_no"] >= a["m"]:
            a["m"], a["p"] = r["map_no"], r["place"]
    top = sorted(agg.values(), key=lambda a: (-a["k"], a["p"], a["nick"].lower()))[:5]
    lines2 = [f"{i}. {a['nick']} — {a['k']}" for i, a in enumerate(top, 1)]

    for lines in (lines1, lines2):
        await bot.send_message(chat, "<pre>" + html.escape("\n".join(lines)) + "</pre>",
                               parse_mode=ParseMode.HTML)
    for w in warns:
        await bot.send_message(chat, w)


# ---------------- TELEGRAM ----------------
dp = Dispatcher()
buffers, timers = defaultdict(list), {}
KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="/next"), KeyboardButton(text="/undo"),
                                    KeyboardButton(text="/reset")]], resize_keyboard=True)


def allowed(m: Message):
    return not ADMINS or m.from_user.id in ADMINS


@dp.message(Command("start"))
async def start(m: Message):
    if allowed(m):
        await m.answer("Rasmlarni tashlang (lobby + natija). 6 soniyada hisoblayman.\n"
                       "/next - keyingi karta\n/undo - shu kartani bekor qilish\n/reset - hammasini tozalash",
                       reply_markup=KB)


@dp.message(Command("next"))
async def nxt(m: Message):
    if not allowed(m):
        return
    n = await get_map(m.chat.id) + 1
    await db("POST", "state", body={"chat_id": m.chat.id, "map_no": n}, upsert="chat_id")
    await m.answer(f"✅ {n}-karta boshlandi. Natija rasmlarini yuboring.")


@dp.message(Command("undo"))
async def undo(m: Message):
    if not allowed(m):
        return
    n = await get_map(m.chat.id)
    await db("DELETE", "kills", {"chat_id": f"eq.{m.chat.id}", "map_no": f"eq.{n}"})
    await m.answer(f"🗑 {n}-karta natijalari o'chirildi.")


@dp.message(Command("reset"))
async def reset(m: Message):
    if not allowed(m):
        return
    for t in ("kills", "slots", "state"):
        await db("DELETE", t, {"chat_id": f"eq.{m.chat.id}"})
    await m.answer("🗑 Hammasi tozalandi. Yangi turnir.")


@dp.message(F.photo)
async def photo(m: Message, bot: Bot):
    if not allowed(m):
        return
    chat = m.chat.id
    buffers[chat].append(m.photo[-1].file_id)
    if chat in timers:
        timers[chat].cancel()

    async def later():
        await asyncio.sleep(WAIT)
        ids, buffers[chat] = buffers[chat], []
        st = await bot.send_message(chat, f"⏳ {len(ids)} ta rasm ishlanmoqda...")
        try:
            await process(bot, chat, ids, st.message_id)
        except Exception as ex:
            print("XATO:", repr(ex))
            await bot.send_message(chat, f"❌ Xato: {str(ex)[:200]}")

    timers[chat] = asyncio.create_task(later())


async def health(_):
    return web.Response(text="ok")


async def main():
    if not PROVIDERS:
        raise SystemExit("Hech bo'lmasa bitta AI kalit kiriting (GROQ_KEYS / GEMINI_KEYS / OPENROUTER_KEYS)")
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
