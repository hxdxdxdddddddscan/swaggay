import asyncio, hashlib, hmac, html, os, random, re, time, urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from telethon import TelegramClient, errors, functions, utils
from telethon.sessions import StringSession
from difflib import SequenceMatcher
from telethon.tl.types import InputMessagesFilterDocument, MessageService
import config as C

client: TelegramClient
h: httpx.AsyncClient
bot: httpx.AsyncClient
tx: httpx.AsyncClient
tcache: dict = {}
flags: dict = {}
scan = {"run": False, "msg": ""}
mstate = {"stop": False}
HERE = os.path.dirname(__file__)
EM = {"📃": 5404515205784806076, "🔹": 5994442901059276913, "➡️": 5215720576735255650,
      "🗂": 5298853345241358103, "👎": 5194952786361469233, "🆘": 5379882580499770351}

def now(): return datetime.now(timezone.utc).isoformat()
def em(e): return f'<tg-emoji emoji-id="{EM[e]}">{e}</tg-emoji>'
def peer(c): return int(c) if c.lstrip("-").isdigit() else c
def fname(m): return m.file.name if (m.document and m.file and m.file.name) else None
def fkey(m):  # дубликат = совпали название, формат (он в названии) и размер
    n = fname(m)
    return f"{n.lower()}|{m.file.size or 0}" if n else ""

async def db(method, table, params=None, body=None, prefer=None):
    for i in range(4):
        try: r = await h.request(method, "/" + table, params=params, json=body, headers={"Prefer": prefer} if prefer else None)
        except httpx.HTTPError:
            if i == 3: raise
            await asyncio.sleep(1 + 2 * i); continue
        if r.status_code in (429, 500, 502, 503, 504) and i < 3: await asyncio.sleep(1 + 2 * i); continue
        if r.status_code >= 300: raise RuntimeError(f"supabase {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

async def log(msg, job=None, level="info"):
    print(level, job, msg)
    try: await db("POST", "logs", body={"job_id": job, "level": level, "msg": msg})
    except Exception as e: print("log fail:", e)

async def notify(text):
    """Отчёт админу. Премиум-эмодзи; если Telegram их не принял, шлём обычные."""
    for plain in (False, True):
        t = re.sub(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', r"\1", text) if plain else text
        try:
            js = {"chat_id": C.ADMIN_ID, "text": t, "parse_mode": "HTML", "disable_web_page_preview": True}
            r = await bot.post("/sendMessage", json=js)
            if r.status_code == 429:
                await asyncio.sleep(r.json().get("parameters", {}).get("retry_after", 3) + 1); r = await bot.post("/sendMessage", json=js)
            if r.status_code == 200: return
            print("bot:", r.text[:200])
        except Exception as e: print("bot fail:", e)

async def upd(jid, **f): await db("PATCH", "jobs", {"id": f"eq.{jid}"}, {**f, "updated_at": now()})
async def sent_today(): return int(await db("POST", "rpc/sent_today", body={}))
async def get_cap(): return int((await db("GET", "settings", {"key": "eq.daily_cap", "select": "value"}))[0]["value"])

async def known(keys):
    out = set()
    for i in range(0, len(keys), 25):
        q = ",".join('"%s"' % k.replace("\\", "\\\\").replace('"', '\\"') for k in keys[i:i + 25])
        out |= {x["key"] for x in await db("GET", "files", {"select": "key", "key": f"in.({q})"})}
    return out

async def add_files(rows):
    if rows: await db("POST", "files", {"on_conflict": "key"}, rows, "resolution=ignore-duplicates,return=minimal")

def frow(m, name, tid, ttitle, jid=None):
    return {"key": fkey(m), "name": name, "size": m.file.size, "ext": os.path.splitext(name)[1].lstrip(".").lower(),
            "topic_id": tid, "topic_title": ttitle, "norm": norm(name), "msg_id": None if jid else m.id, "job_id": jid}

def norm_chat(c):
    c = c.lstrip("@")
    return "-100" + c if re.fullmatch(r"\d{9,}", c) else c

def _clean(s):
    s = urllib.parse.unquote(s.strip().strip("<>"))
    th = re.search(r"[?&]thread=(\d+)", s)
    return s.split("?")[0].split("#")[0].rstrip("/"), (int(th[1]) if th else None)

def parse(s):
    """Ссылка/запись ветки -> (чат, id ветки). Понимает t.me/c/ID/ТОПИК[/СООБЩЕНИЕ], t.me/имя/ТОПИК, ...?thread=N, чат:топик."""
    s, th = _clean(s)
    m = re.fullmatch(r"(-?\d+|@?\w+):(\d+)", s)
    if m: return norm_chat(m[1]), int(m[2])
    m = re.search(r"t\.me/c/(\d+)/(\d+)", s)
    if m: return "-100" + m[1], th or int(m[2])
    m = re.search(r"t\.me/(?!c/|\+|joinchat)(\w+)/(\d+)", s)
    if m: return m[1], th or int(m[2])
    raise ValueError(f"не понял ветку: {s}")

async def nap(jid, s):
    for _ in range(int(s)):
        if flags.get(jid): return
        await asyncio.sleep(1)

async def tt(ent, tid):
    try: return (await client(functions.messages.GetForumTopicsByIDRequest(peer=ent, topics=[tid]))).topics[0].title
    except Exception: return f"топик {tid}"

class Stop(Exception): pass

def parse_group(s):
    """Группа/канал: ссылка t.me/c/ID[/…], t.me/имя[/…], ссылка-приглашение (t.me/+…), @имя, id."""
    s, _ = _clean(s)
    m = re.search(r"t\.me/(?:\+|joinchat/)([\w-]+)", s)
    if m: return "invite:" + m[1]
    m = re.search(r"t\.me/c/(\d+)", s)
    if m: return "-100" + m[1]
    m = re.search(r"t\.me/(\w+)", s)
    if m: return m[1]
    if re.fullmatch(r"-?\d+|@?[A-Za-z]\w{3,}", s): return norm_chat(s)
    raise ValueError(f"не понял группу: {s}")

def check_dst(s):
    try: parse(s)
    except ValueError: parse_group(s)

async def resolve(c):
    """Ссылка-приглашение -> числовой id (если аккаунт ещё не в группе, вступает)."""
    if not c.startswith("invite:"): return c
    r = await client(functions.messages.CheckChatInviteRequest(c[7:]))
    ch = getattr(r, "chat", None)
    if ch is None:
        ch = (await client(functions.messages.ImportChatInviteRequest(c[7:]))).chats[0]
        await log(f"вступил в «{getattr(ch, 'title', '?')}» по ссылке-приглашению")
    return str(utils.get_peer_id(ch))

async def topics_of(ent):
    out, od, oi, ot = [], 0, 0, 0
    while True:
        r = await client(functions.messages.GetForumTopicsRequest(peer=ent, offset_date=od, offset_id=oi, offset_topic=ot, limit=100))
        ts = [t for t in r.topics if hasattr(t, "title")]
        out += [(t.id, t.title) for t in ts]
        if len(r.topics) < 100 or not ts: break
        mm = {x.id: x for x in r.messages}.get(ts[-1].top_message)
        if not mm: break
        od, oi, ot = mm.date, ts[-1].top_message, ts[-1].id
    return list(dict(out).items())

TR = {"а":"a","б":"b","в":"v","г":"g","ґ":"g","д":"d","е":"e","є":"e","ё":"e","ж":"zh","з":"z","и":"i","і":"i","ї":"i","й":"i","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"ts","ч":"ch","ш":"sh","щ":"shch","ъ":"","ь":"","ы":"i","э":"e","ю":"u","я":"a"}

COLLAPSE = (("shch", "s"), ("sch", "s"), ("sh", "s"), ("kh", "h"), ("zh", "z"), ("ts", "c"), ("ch", "c"),
            ("ph", "f"), ("ck", "k"), ("y", "i"), ("j", "i"), ("w", "v"), ("x", "ks"), ("q", "k"))

def norm(s):
    """Кириллица/латиница -> единая форма (буквы+цифры): «Ощадбанк», «Oschadbank», «ощабанк» сходятся."""
    s = "".join(TR.get(c, c) for c in s.lower())
    s = re.sub(r"[^a-z0-9]", "", s)
    for a, b in COLLAPSE: s = s.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", s)

def canon(s): return re.sub(r"\d", "", norm(s))

def find(s, tl, fuzzy=True):
    """Ищет ветку (tl = [(canon, id)]) в строке: сначала точное вхождение, потом нечёткое (опечатки)."""
    if len(s) < 3: return None
    ex = [(len(t), i) for t, i in tl if t in s]
    if ex: return max(ex)[1]
    if not fuzzy or len(s) > 120: return None
    best = (0.0, None)
    for t, i in tl:
        L = len(t)
        if L < 5: continue
        for w in range(max(3, L - 1), L + 2):
            for p in range(max(1, len(s) - w + 1)):
                sc = SequenceMatcher(None, s[p:p + w], t).ratio()
                if sc > best[0]: best = (sc, i)
    return best[1] if best[0] >= 0.85 else None

async def make_topic(dst, title):
    r = await client(functions.messages.CreateForumTopicRequest(peer=dst, title=title[:128], random_id=random.randrange(2**31)))
    return next(u.message.id for u in r.updates if hasattr(u, "message") and hasattr(u.message, "id"))

async def run_job(j):
    chan = j.get("mode") == "channel"
    jid, done = j["id"], j["forwarded"]
    src = await client.get_input_entity(peer(j["src_chat"])); dst = await client.get_input_entity(peer(j["dst_chat"]))
    stitle = (await client.get_entity(src)).title; gtitle = (await client.get_entity(dst)).title
    cache, last = {}, [0]
    if chan:
        have = {t: i for i, t in await topics_of(dst)}
        fbt = stitle[:128]
        if fbt not in have:
            have[fbt] = await make_topic(dst, fbt); await log(f"создана ветка «{fbt}» для файлов без совпадений", jid)
        fb = have[fbt]; last[0] = fb
        names = {i: t for t, i in have.items()}
        tl = [(canon(t), i) for t, i in have.items() if i not in (1, fb) and len(canon(t)) >= 3]
        a, b = stitle, f"{gtitle} (по веткам)"
    else:
        dt = j["dst_topic"]; dtitle = await tt(dst, dt); names = {dt: dtitle}
        a = f"{stitle}, {await tt(src, j['src_topic'])}"; b = f"{gtitle}, {dtitle}"
    tot = (await client.get_messages(src, reply_to=None if chan else j["src_topic"], limit=0)).total
    await upd(jid, status="running", total=tot, error=None)
    await log(f"старт {a} → {b}, ≈{tot} сообщений, с id {j['last_id']}, пачка {j['batch']}", jid)
    rep = {"n": 0, "dups": 0, "names": [], "errs": [], "by": {}}
    pend, failed, tot, pace, streak = [], [], {"dup": 0, "err": 0}, [j["pause_s"]], [0]

    def route(ms):
        if not chan: return dt
        nm = [os.path.splitext(fname(m))[0] for m in ms if fname(m)]
        for s in nm:  # название файла (любого из группы файлов)
            k = canon(s)
            if k not in cache: cache[k] = find(k, tl)
            if cache[k]: return cache[k]
        txt = canon(" ".join((m.message or "")[:300] for m in ms))  # подпись/текст: только точное совпадение
        r = find(txt, tl, fuzzy=False) if txt else None
        return r or (fb if nm else last[0])  # файл без совпадения -> ветка канала; чистый текст -> туда же, куда прошлое

    async def report():
        if not (rep["n"] or rep["dups"] or rep["errs"]): return
        L = [f"{em('📃')} <b>Новая пересылка #{jid}</b>", f"{em('🔹')} <b>Откуда:</b> {html.escape(a)}",
             f"{em('➡️')} <b>Куда:</b> {html.escape(b)}", f"{em('🗂')} <b>Файлы: {rep['n']}</b>"]
        if chan: L += [f"• <b>{html.escape(n)}</b>: {c}" for n, c in rep["by"].items()]
        L += [f"{i}. <b>{html.escape(n[:60])}</b>" for i, n in enumerate(rep["names"][:35], 1)]
        if len(rep["names"]) > 35: L.append(f"…и ещё {len(rep['names']) - 35}")
        L.append(f"{em('👎')} <b>Уже были в бд:</b> {rep['dups']}")
        L.append(f"{em('🆘')} <b>Ошибки:</b> {len(rep['errs'])}" + (": " + html.escape("; ".join(rep["errs"][:3])) if rep["errs"] else ""))
        await notify("\n".join(L)); rep.update(n=0, dups=0, names=[], errs=[], by={})

    async def fwd(ms, tid):
        while True:
            try:
                await client(functions.messages.ForwardMessagesRequest(from_peer=src, id=[m.id for m in ms],
                    random_id=[random.randrange(2**63) for _ in ms], to_peer=dst, top_msg_id=tid, drop_author=True))
                return
            except errors.FloodWaitError as e:
                await log(f"FloodWait {e.seconds}s", jid, "warn"); pace[0] = min(pace[0] * 2, 20); streak[0] = 0
                await nap(jid, e.seconds + 1)
                if flags.get(jid): raise Stop

    async def record(lst):
        nonlocal done
        done += len(lst); fs = [(m, t) for m, t in lst if fname(m)]
        await db("POST", "sent", body={"job_id": jid, "cnt": len(lst)})
        await add_files([frow(m, fname(m), t, names.get(t, ""), jid) for m, t in fs])
        rep["n"] += len(lst); rep["names"] += [fname(m) for m, _ in fs]
        for _, t in lst: n = names.get(t, str(t)); rep["by"][n] = rep["by"].get(n, 0) + 1
        await log(f"переслано {len(lst)} → {', '.join(sorted({names.get(t, str(t)) for _, t in lst}))[:120]}: {', '.join(fname(m) for m, _ in fs)[:250] or 'без названий'}", jid)

    async def flush():  # дедуп (название+размер) одним запросом к БД, затем пересылка по веткам
        nonlocal pend
        if not pend: return
        items, pend = pend, []
        kn = await known(list({k for k in (fkey(m) for m, _ in items) if k}))
        seen, send = set(), []
        for m, t in items:
            k = fkey(m)
            if k and (k in kn or k in seen): rep["dups"] += 1; tot["dup"] += 1; continue
            if k: seen.add(k)
            send.append((m, t))
        while send:
            if flags.get(jid): raise Stop
            cap = await get_cap()
            if await sent_today() + len(send) > cap:
                await upd(jid, status="limit"); await log(f"дневной лимит {cap}, жду (поднять можно в «Обзоре»)", jid, "warn")
                await nap(jid, 600); continue
            groups = {}
            for m, t in send: groups.setdefault(t, []).append(m)
            for t, g in groups.items():
                try: await fwd(g, t)
                except (Stop, errors.ChatForwardsRestrictedError): raise
                except Exception:  # в пачке битое сообщение: по одному
                    for m in g:
                        try: await fwd([m], t)
                        except (Stop, errors.ChatForwardsRestrictedError): raise
                        except Exception as e:
                            failed.append((m, t)); await log(f"пропуск id {m.id}, повторю позже: {e!r}", jid, "warn")
                        else: await record([(m, t)])
                else: await record([(m, t) for m in g])
            break
        await upd(jid, status="running", last_id=items[-1][0].id, forwarded=done)
        if rep["n"] >= 30: await report()
        if send:
            streak[0] += 1
            if streak[0] % 10 == 0 and pace[0] > j["pause_s"]: pace[0] -= 1  # после FloodWait темп плавно возвращается
            await nap(jid, pace[0])

    async def take(cur):
        if not cur: return
        tid = route(cur); last[0] = tid
        pend.extend((m, tid) for m in cur)
        if len(pend) >= j["batch"]: await flush()

    try:
        cur = []
        kw = {} if chan else {"reply_to": j["src_topic"]}
        async for m in client.iter_messages(src, reverse=True, min_id=j["last_id"], **kw):
            if flags.get(jid): raise Stop
            if isinstance(m, MessageService): continue
            if not chan and j.get("files_only") and not (m.document or m.photo): continue
            if cur and not (m.grouped_id and m.grouped_id == cur[-1].grouped_id):  # альбом не разрываем
                await take(cur); cur = []
            cur.append(m)
        await take(cur); await flush()
        if failed:
            await log(f"повторяю пропущенные: {len(failed)}", jid); await nap(jid, 20)
            for m, t in failed:
                try: await fwd([m], t)
                except (Stop, errors.ChatForwardsRestrictedError): raise
                except Exception as e:
                    tot["err"] += 1; rep["errs"].append(f"id {m.id}: {repr(e)[:60]}"); await log(f"ошибка id {m.id}: {e!r}", jid, "error")
                else: await record([(m, t)])
        await upd(jid, status="done")
        await log(f"готово: переслано {done - j['forwarded']}, дублей пропущено {tot['dup']}, ошибок {tot['err']}", jid)
    except Stop: return
    finally: await report()

async def do_audit(src_s, dst_s):
    try:
        src = await client.get_input_entity(peer(await resolve(parse_group(src_s)))); dst = await client.get_input_entity(peer(await resolve(parse_group(dst_s))))
        dmap = {t: i for i, t in await topics_of(dst)}
        await log("сверка: старт")
        for tid, title in await topics_of(src):
            try:
                n = (await client.get_messages(src, reply_to=tid, filter=InputMessagesFilterDocument, limit=0)).total
                m = (await client.get_messages(dst, reply_to=dmap[title], filter=InputMessagesFilterDocument, limit=0)).total if title in dmap else 0
                await log(f"{'⚠' if m < n else '✓'} «{title}»: в источнике {n}, у нас {m}" + (f", не хватает {n - m}" if m < n else ""), None, "warn" if m < n else "info")
            except Exception as e: await log(f"сверка «{title}»: {e!r}", None, "error")
        await log("сверка: готово. Часть разницы могут быть пропущенные дубликаты (название+размер). Недостающее досылается повторным запуском «Переслать всё».")
    except Exception as e: await log(f"ошибка сверки: {e!r}", None, "error")

async def do_mirror(src_s, dst_s, batch, pause, start_at):
    try:
        sc = await resolve(parse_group(src_s)); src = await client.get_input_entity(peer(sc))
        try: dc, dtopic = parse(dst_s)
        except ValueError: dc, dtopic = await resolve(parse_group(dst_s)), None
        dst = await client.get_input_entity(peer(dc))
        have = {t: i for i, t in await topics_of(dst)} if dtopic is None else {}
        rows = []; mstate["stop"] = False
        for tid, title in await topics_of(src):
            if mstate["stop"]: await log("создание веток отменено", None, "warn"); return
            if dtopic is None:
                if title not in have:
                    r = await client(functions.messages.CreateForumTopicRequest(peer=dst, title=title[:128], random_id=random.randrange(2**31)))
                    have[title] = next(u.message.id for u in r.updates if hasattr(u, "message") and hasattr(u.message, "id"))
                    await log(f"создана ветка «{title}» в группе назначения"); await asyncio.sleep(1)
            rows.append({"src_chat": sc, "src_topic": tid, "dst_chat": dc, "dst_topic": dtopic or have[title], "files_only": True,
                         "batch": batch, "pause_s": pause, "start_at": start_at})
        out = await db("POST", "jobs", body=rows, prefer="return=representation")
        await log(f"«Переслать всё»: создано задач: {len(out)}, пачка {batch}")
    except Exception as e: await log(f"ошибка «Переслать всё»: {e!r}", None, "error")

async def do_scan(chat):
    scan.update(run=True, msg="старт")
    try:
        ent = await client.get_input_entity(peer(chat)); n = 0
        for tid, title in await topics_of(ent):
            scan["msg"] = f"сканирую: {title}"
            await db("POST", "topics", {"on_conflict": "id"}, [{"id": tid, "title": title}], "resolution=merge-duplicates,return=minimal")
            rows = []
            try:
                async for m in client.iter_messages(ent, reply_to=tid):
                    if fname(m): rows.append(frow(m, fname(m), tid, title)); n += 1
                    if len(rows) >= 200: await add_files(rows); rows = []
                await add_files(rows)
            except Exception as e: await log(f"скан «{title}»: {e!r}", None, "warn")
        scan["msg"] = f"готово, файлов найдено: {n}"
    except Exception as e: scan["msg"] = f"ошибка: {e!r}"
    scan["run"] = False

async def runner():
    while True:
        try:
            rows = await db("GET", "jobs", {"status": "eq.queued", "or": f"(start_at.is.null,start_at.lte.{now()})", "order": "id.asc", "limit": "1"})
            if not rows: await asyncio.sleep(3); continue
            j = rows[0]; jid = j["id"]; flags.pop(jid, None)
            try:
                await run_job(j)
                f = flags.get(jid)
                if f:
                    await upd(jid, status="paused" if f == "pause" else "stopped")
                    await log("пауза" if f == "pause" else "остановлено", jid)
            except Exception as e:
                hint = " (в группе запрещена пересылка)" if isinstance(e, errors.ChatForwardsRestrictedError) else ""
                await upd(jid, status="error", error=repr(e)); await log(f"ошибка: {e!r}{hint}", jid, "error")
                await notify(f"{em('🆘')} <b>Задача #{jid} остановлена с ошибкой:</b> {html.escape(repr(e)[:300])}")
        except Exception as e:
            print("runner:", e); await asyncio.sleep(5)

async def selfping():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    while url:
        await asyncio.sleep(600)
        try:
            async with httpx.AsyncClient() as x: await x.get(url + "/health")
        except Exception: pass

async def translate(q):
    """Перевод запроса (ru/uk -> en, латиница -> ru) через бесплатный MyMemory; при сбое просто пусто."""
    if q in tcache: return tcache[q]
    low = q.lower()
    lp = ("uk|en" if re.search("[іїєґ]", low) else "ru|en") if re.search("[а-яёіїєґ]", low) else "en|ru"
    out = ""
    try:
        r = await tx.get("https://api.mymemory.translated.net/get", params={"q": q, "langpair": lp})
        t = (r.json().get("responseData") or {}).get("translatedText") or ""
        if t and t.lower() != low and "MYMEMORY" not in t.upper(): out = t.strip()
    except Exception: pass
    if len(tcache) > 500: tcache.clear()
    tcache[q] = out; return out

async def variants(q):
    vs = [q]; t = await translate(q)
    if t: vs.append(t)
    lat = "".join(TR.get(c, c) for c in q.lower())
    if lat != q.lower() and re.fullmatch(r"[a-z0-9 ._-]+", lat): vs.append(lat)
    seen = set()
    return [v for v in vs if not (v.lower() in seen or seen.add(v.lower()))]

async def reindex():
    """Заполняет поисковую колонку norm у файлов, где её ещё нет."""
    try:
        while True:
            rows = await db("GET", "files", {"norm": "is.null", "select": "key,name", "limit": "500"})
            if not rows: return
            await db("POST", "files", {"on_conflict": "key"}, [{"key": x["key"], "name": x["name"], "norm": norm(x["name"])} for x in rows],
                     "resolution=merge-duplicates,return=minimal")
    except Exception as e: print("reindex:", e)

@asynccontextmanager
async def lifespan(app):
    global client, h, bot, tx
    h = httpx.AsyncClient(base_url=C.SUPABASE_URL.rstrip("/") + "/rest/v1", headers={"apikey": C.SUPABASE_KEY}, timeout=30)
    bot = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{C.BOT_TOKEN}", timeout=20)
    tx = httpx.AsyncClient(timeout=6)
    client = TelegramClient(StringSession(C.TG_SESSION), int(C.API_ID), C.API_HASH)
    await client.connect()
    if not await client.is_user_authorized(): raise RuntimeError("TG_SESSION не авторизована")
    await client.get_dialogs()
    await db("PATCH", "jobs", {"status": "in.(running,limit)"}, {"status": "queued"})
    await log("сервис запущен")
    tasks = [asyncio.create_task(runner()), asyncio.create_task(selfping()), asyncio.create_task(reindex())]
    yield
    for t in tasks: t.cancel()
    await client.disconnect(); await h.aclose(); await bot.aclose(); await tx.aclose()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

@app.exception_handler(Exception)
async def boom(req, exc):
    print("error:", repr(exc)); return JSONResponse({"detail": "Ошибка сервера, подробности в логах Render"}, 500)

def iso(s):
    if not s: return None
    try: return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError: raise HTTPException(400, "неверная дата старта")
SECRET = hashlib.sha256((C.ADMIN_PASSWORD + C.SUPABASE_KEY).encode()).digest()
fails: dict = {}

@app.middleware("http")
async def headers(req: Request, call_next):
    resp = await call_next(req)
    resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Strict-Transport-Security": "max-age=31536000", "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"})
    return resp

def sign(exp): return hmac.new(SECRET, str(exp).encode(), "sha256").hexdigest()

def guard(req: Request):
    t = req.cookies.get("s", "")
    exp, _, sig = t.partition(".")
    if not (exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, sign(exp))): raise HTTPException(401)
    if req.method != "GET" and req.headers.get("x-req") != "1": raise HTTPException(403)  # защита от CSRF

class Login(BaseModel): user: str; password: str

@app.post("/api/login")
async def login(b: Login, req: Request, resp: Response):
    ip = req.headers.get("x-forwarded-for", "").split(",")[0].strip() or req.client.host
    if len(fails) > 500: fails.clear()
    n, until = fails.get(ip, (0, 0))
    if until > time.time(): raise HTTPException(429, "Слишком много попыток. Подожди 15 минут.")
    if not (hmac.compare_digest(b.user.encode(), C.ADMIN_USER.encode()) and hmac.compare_digest(b.password.encode(), C.ADMIN_PASSWORD.encode())):
        fails[ip] = (n + 1, time.time() + 900 if n + 1 >= 5 else 0)
        await asyncio.sleep(1); raise HTTPException(401, "Неверный логин или пароль")
    fails.pop(ip, None)
    exp = int(time.time()) + 7 * 86400
    resp.set_cookie("s", f"{exp}.{sign(exp)}", max_age=7 * 86400, httponly=True, samesite="strict", secure=req.headers.get("x-forwarded-proto") == "https")
    return {"ok": True}

@app.post("/api/logout")
async def logout(resp: Response): resp.delete_cookie("s"); return {"ok": True}

@app.get("/health")
async def health(): return {"ok": True}

@app.get("/")
async def index(): return FileResponse(os.path.join(HERE, "static", "index.html"))

r = APIRouter(prefix="/api", dependencies=[Depends(guard)])

class New(BaseModel):
    lines: str; pause_s: int = 2; start_at: Optional[str] = None

@r.get("/me")
async def me(): return {"ok": True}

@r.get("/state")
async def state():
    jobs = await db("GET", "jobs", {"order": "id.desc", "limit": "200"})
    return {"jobs": jobs, "today": await sent_today(), "cap": await get_cap(), "scan": scan}

@r.get("/stats")
async def stats(): return await db("POST", "rpc/stats", body={})

@r.get("/search")
async def search(q: str):
    q = q.strip()[:100]
    if not q: return {"variants": [], "results": []}
    vs = await variants(q)
    esc_ = lambda s: s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    words = [w for w in re.split(r"\W+", q) if len(w) >= 4]
    nl = list({x for x in (norm(v) for v in vs + words) if len(x) >= 3})
    rows = await db("POST", "rpc/search_files", body={"q": [esc_(v) for v in vs if len(v) >= 2], "n": nl, "lim": 200})
    return {"variants": vs, "results": rows}

@r.get("/logs")
async def logs(after: int = 0):
    if after: return await db("GET", "logs", {"id": f"gt.{after}", "order": "id.asc", "limit": "500"})
    return (await db("GET", "logs", {"order": "id.desc", "limit": "200"}))[::-1]

@r.post("/jobs")
async def new_job(b: New):
    rows = []
    ls = [x.strip() for x in b.lines.splitlines() if x.strip()]
    sep = r"\s*(?:\||->|→)\s*"
    if ls and not any(re.search(sep, x) for x in ls) and len(ls) % 2 == 0:  # две строки подряд: откуда, потом куда
        pairs = [(ls[i], ls[i + 1]) for i in range(0, len(ls), 2)]
    else:
        pairs = []
        for ln in ls:
            p = re.split(sep, ln)
            if len(p) != 2: raise HTTPException(400, f"строка должна быть вида «откуда | куда»: {ln}")
            pairs.append((p[0], p[1]))
    for s, d in pairs:
        try: (sc, st), (dc, dt) = parse(s), parse(d)
        except ValueError as e: raise HTTPException(400, str(e))
        rows.append({"src_chat": sc, "src_topic": st, "dst_chat": dc, "dst_topic": dt, "pause_s": max(1, b.pause_s), "batch": 1, "start_at": iso(b.start_at)})
    if not rows: raise HTTPException(400, "пусто")
    out = await db("POST", "jobs", body=rows, prefer="return=representation")
    for x in out: await log(f"задача создана: {x['src_chat']}/{x['src_topic']} → {x['dst_chat']}/{x['dst_topic']}", x["id"])
    return {"ids": [x["id"] for x in out]}

@r.post("/jobs/{jid}/{act}")
async def job_act(jid: int, act: str):
    if act in ("pause", "stop"):
        flags[jid] = act
        await db("PATCH", "jobs", {"id": f"eq.{jid}", "status": "eq.queued"}, {"status": "paused" if act == "pause" else "stopped"})
    elif act == "resume":
        flags.pop(jid, None)
        await db("PATCH", "jobs", {"id": f"eq.{jid}", "status": "in.(paused,stopped,error)"}, {"status": "queued"})
    else: raise HTTPException(400)
    return {"ok": True}

class Cap(BaseModel): cap: int

@r.post("/cap")
async def set_cap(b: Cap):
    await db("PATCH", "settings", {"key": "eq.daily_cap"}, {"value": str(max(1, b.cap))}); return {"ok": True}

class Mirror(BaseModel):
    src: str; dst: str; batch: int = 50; pause_s: int = 1; start_at: Optional[str] = None

@r.post("/mirror")
async def mirror(b: Mirror):
    try: parse_group(b.src); check_dst(b.dst)
    except ValueError as e: raise HTTPException(400, str(e))
    asyncio.create_task(do_mirror(b.src, b.dst, max(1, min(b.batch, 90)), max(1, b.pause_s), iso(b.start_at))); return {"ok": True}

@r.post("/stop_all")
async def stop_all():
    mstate["stop"] = True
    out = await db("PATCH", "jobs", {"status": "in.(queued,paused)"}, {"status": "stopped"}, "return=representation")
    run = await db("GET", "jobs", {"status": "in.(running,limit)", "select": "id"})
    for x in run: flags[x["id"]] = "stop"
    await log(f"остановлено всё: в очереди {len(out)}, активных {len(run)}", None, "warn")
    return {"ok": True}

class Chan(BaseModel):
    src: str; dst: str; batch: int = 50; pause_s: int = 1; start_at: Optional[str] = None

@r.post("/channel")
async def channel(b: Chan):
    try: sc, dc = await resolve(parse_group(b.src)), await resolve(parse_group(b.dst))
    except Exception as e: raise HTTPException(400, str(e))
    row = (await db("POST", "jobs", body={"src_chat": sc, "src_topic": 0, "dst_chat": dc, "dst_topic": 0, "mode": "channel", "files_only": False,
           "batch": max(1, min(b.batch, 90)), "pause_s": max(1, b.pause_s), "start_at": iso(b.start_at)}, prefer="return=representation"))[0]
    await log("задача «из канала» создана", row["id"]); return {"id": row["id"]}

class Audit(BaseModel): src: str; dst: str

@r.post("/audit")
async def audit(b: Audit):
    try: parse_group(b.src); parse_group(b.dst)
    except ValueError as e: raise HTTPException(400, str(e))
    asyncio.create_task(do_audit(b.src, b.dst)); return {"ok": True}

@r.post("/scan")
async def start_scan():
    if scan["run"]: raise HTTPException(409, "скан уже идёт")
    last = await db("GET", "jobs", {"order": "id.desc", "limit": "1", "select": "dst_chat"})
    if not last: raise HTTPException(400, "сначала создай задачу: группа назначения берётся из неё")
    asyncio.create_task(do_scan(last[0]["dst_chat"])); return {"ok": True}

app.include_router(r)
