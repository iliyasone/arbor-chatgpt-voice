# /// script
# requires-python = ">=3.11"
# dependencies = ["curl_cffi>=0.7", "starlette>=0.37", "uvicorn>=0.30"]
# ///
"""ChatGPT Read Aloud voice (Arbor = internal id `fathom`) behind an OpenAI-compatible endpoint.

ChatGPT only voices assistant messages, so every chunk of text is sent to a throwaway chat
with a "repeat verbatim" prompt, the reply is synthesized with /backend-api/synthesize and
the chat is deleted. No browser: TLS is impersonated with curl_cffi, proof-of-work is solved here.

    POST /v1/audio/speech  {"input": "...", "voice": "arbor", "response_format": "mp3", "spell_numbers": false}
"""
import asyncio, base64, contextlib, hashlib, json, logging, os, random, re, time, uuid
from curl_cffi.requests import AsyncSession
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

B = "https://chatgpt.com"
CRED = os.path.expanduser(os.environ.get("ARBOR_CREDS", "~/.config/chatgpt-arbor/creds.json"))
API_KEY = os.environ.get("ARBOR_TTS_KEY")
VOICES = {"arbor": "fathom", "spruce": "orbit", "sol": "glimmer"}
FORMATS = {"mp3", "aac", "opus"}
CHUNK = 1200
PROMPT = ("Повтори дословно, без изменений и без комментариев, текст между тегами. "
          "Выведи только сам текст.\n<text>\n{}\n</text>")
# Read Aloud stumbles over digits ("0.1.2", "24.10", "№ 482", "1."), so with spell_numbers the chat
# rewrites them as words first; the rest of the text stays as it is.
SPELL_PROMPT = ("Повтори дословно, без изменений и без комментариев, текст между тегами. Не переформулируй, "
                "не пропускай предложения, ничего не добавляй и не переводи. Единственное исключение: запиши "
                "словами, в правильном роде и падеже, каждое число и каждый знак валюты или единицы, даже если "
                "рядом нет числа (₽, $, €, %, №, ~). Примеры: «15 мин» — «пятнадцать минут»; «1. Названия» — "
                "«Первое. Названия»; «22.10» — «двадцать второе октября»; «в 21:00» — «в девять вечера»; "
                "«2,5–3 кВт» — «два с половиной — три киловатта»; «7500 ₽» — «семь с половиной тысяч рублей»; "
                "«знак ₽» — «знак рубля»; «PR #440» — «PR четыреста сорок»; версия «0.1.2» — «ноль один два»; "
                "«Opus 5.5» — «Opus пять и пять»; видеокарта «3070» — «тридцать семьдесят»; «~1800px» — «около "
                "тысячи восьмисот пикселей»; длинный код «912300121» — «девять один два, триста, сто двадцать один». "
                "Чисел цифрами в ответе остаться не должно, только цифры внутри названий вроде t3-voice или Qwen3. "
                "Выведи только сам текст."
                "\n<text>\n{}\n</text>")
log = logging.getLogger("arbor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class ChatGPT:
    def __init__(self):
        self.c = json.load(open(CRED))
        self.c.setdefault("dev", str(uuid.uuid4()))
        self.s = AsyncSession(impersonate="chrome", timeout=120)
        for k, v in self.c.get("cookies", {"__Secure-next-auth.session-token": self.c.get("s")}).items():
            self.s.cookies.set(k, v, domain="chatgpt.com")
        self.sem = asyncio.Semaphore(2)
        self.lock = asyncio.Lock()

    def save(self):
        self.c["cookies"] = {c.name: c.value for c in self.s.cookies.jar if "chatgpt.com" in c.domain}
        tmp = CRED + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(self.c, f)
        os.replace(tmp, CRED)

    def token_days_left(self):
        p = json.loads(base64.urlsafe_b64decode(self.c["a"].split(".")[1] + "=="))
        return (p["exp"] - time.time()) / 86400

    async def ensure_token(self):
        """/api/auth/session keeps returning the old access token until it expires, so the
        12-hour refresher can't renew it in advance: renew it here once it has run out."""
        async with self.lock:
            if self.token_days_left() < 60 / 86400:
                await self.refresh()

    async def refresh(self):
        r = await self.s.get(B + "/api/auth/session", headers={"Referer": B + "/"})
        j = r.json()
        if not j.get("accessToken"):
            raise RuntimeError(f"session refresh failed: {r.status_code} {str(j)[:200]}")
        self.c["a"] = j["accessToken"]
        self.save()
        log.info("refreshed: token %.1f days, session until %s", self.token_days_left(), j.get("expires"))

    def h(self, **extra):
        h = {"Authorization": "Bearer " + self.c["a"], "Origin": B, "Referer": B + "/",
             "oai-language": "ru-RU", "oai-device-id": self.c["dev"]}
        h.update(extra)
        return h

    def _pow_config(self):
        return [4000, time.strftime("%a %b %d %Y %H:%M:%S GMT+0300 (Moscow Standard Time)"), 4294705152, 0,
                self.c["ua"], B + "/backend-api/sentinel/sdk.js", None, "ru-RU", "ru-RU,ru,en-US,en", 0,
                "webdriver−false", "location", "window", time.perf_counter() * 1000, str(uuid.uuid4()), "",
                16, time.time() * 1000]

    def _solve(self, seed, diff):
        cfg, t0 = self._pow_config(), time.time()
        for i in range(3_000_000):
            cfg[3], cfg[9] = i, round((time.time() - t0) * 1000)
            b = base64.b64encode(json.dumps(cfg, separators=(",", ":")).encode()).decode()
            if hashlib.sha3_512((seed + b).encode()).hexdigest()[: len(diff)] <= diff:
                return b
        raise RuntimeError("proof of work not solved")

    async def _requirements(self):
        p = "gAAAAAC" + await asyncio.to_thread(self._solve, str(random.random()), "0fffff")
        r = await self.s.post(B + "/backend-api/sentinel/chat-requirements",
                              headers=self.h(**{"Content-Type": "application/json"}), json={"p": p})
        j = r.json()
        if "token" not in j:
            raise RuntimeError(f"chat-requirements {r.status_code}: {r.text[:300]}")
        hd = {"openai-sentinel-chat-requirements-token": j["token"]}
        pw = j.get("proofofwork") or {}
        if pw.get("required"):
            hd["openai-sentinel-proof-token"] = "gAAAAAB" + await asyncio.to_thread(self._solve, pw["seed"], pw["difficulty"])
        return hd

    async def _ask(self, text, prompt):
        hd = self.h(**{"Content-Type": "application/json", "Accept": "text/event-stream"}, **await self._requirements())
        body = {"action": "next", "parent_message_id": "client-created-root", "model": "auto",
                "messages": [{"id": str(uuid.uuid4()), "author": {"role": "user"}, "create_time": time.time(),
                              "content": {"content_type": "text", "parts": [prompt.format(text)]}, "metadata": {}}],
                "timezone_offset_min": -180, "conversation_mode": {"kind": "primary_assistant"},
                "force_paragen": False, "supports_buffering": True, "system_hints": []}
        r = await self.s.post(B + "/backend-api/f/conversation", headers=hd, json=body)
        if r.status_code != 200:
            raise RuntimeError(f"conversation {r.status_code}: {r.text[:300]}")
        cid = None
        for line in r.text.splitlines():
            if line.startswith("data: {"):
                cid = json.loads(line[6:]).get("conversation_id", cid)
        c = (await self.s.get(f"{B}/backend-api/conversation/{cid}", headers=self.h())).json()
        replies = [(k, n["message"]["content"]["parts"][0]) for k, n in c["mapping"].items()
                   if n.get("message") and n["message"]["author"]["role"] == "assistant"
                   and n["message"]["content"].get("content_type") == "text" and n["message"]["content"]["parts"][0]]
        if not replies:
            raise RuntimeError("no assistant reply")
        return cid, *replies[-1]

    async def prepare(self, text, spell_numbers=False):
        """Create an assistant message that says `text`; returns (conversation_id, message_id).
        With spell_numbers the message is `text` with its numbers written as words."""
        async with self.sem:
            for attempt in range(2):
                cid, mid, said = await self._ask(text, SPELL_PROMPT if spell_numbers else PROMPT)
                ok = spelled(said, text) if spell_numbers else norm(said) == norm(text)
                if ok or attempt:
                    if not ok:
                        log.warning("not %s: %r -> %r", "spelled" if spell_numbers else "verbatim",
                                    text[:80], said[:80])
                    return cid, mid
                await self.delete(cid)

    async def synth(self, cid, mid, voice, fmt):
        url = f"{B}/backend-api/synthesize?message_id={mid}&conversation_id={cid}&voice={voice}&format={fmt}"
        r = await self.s.get(url, headers=self.h(), stream=True)
        if r.status_code != 200:
            raise RuntimeError(f"synthesize {r.status_code}: {(await r.acontent())[:200]}")
        async for ch in r.aiter_content():
            yield ch

    async def delete(self, cid):
        await self.s.patch(f"{B}/backend-api/conversation/{cid}",
                           headers=self.h(**{"Content-Type": "application/json"}), json={"is_visible": False})


def norm(t):
    return re.sub(r"\s+", " ", t).strip().strip("«»\"").strip()


def letters(t):
    return re.sub(r"[^a-zа-яё]", "", t.lower())


def spelled(said, text):
    """A rewrite keeps every word of `text`, only grows where numbers became words and has no bare
    numbers left (digits inside names like t3-voice or Qwen3 may stay)."""
    words = letters(re.sub(r"\S*\d\S*", " ", text))
    bare = re.search(r"(?<![a-zа-яё])\d+(?![a-zа-яё\d])", said, re.I)
    return not bare and len(words) * 0.9 <= len(letters(said)) <= len(letters(text)) + 40 * len(re.findall(r"\d+", text)) + 20


def chunks(text):
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?…])\s+|\n{2,}", text.strip()):
        if cur and len(cur) + len(sent) + 1 > CHUNK:
            parts.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    return parts + [cur] if cur else parts


gpt: ChatGPT


async def speech(req: Request):
    if API_KEY and req.headers.get("authorization") != f"Bearer {API_KEY}":
        return JSONResponse({"error": "unauthorized"}, 401)
    j = await req.json()
    text = (j.get("input") or "").strip()
    voice = VOICES.get(j.get("voice", "arbor"), j.get("voice", "fathom"))
    fmt = j.get("response_format", "mp3")
    spell = bool(j.get("spell_numbers"))
    if not text or fmt not in FORMATS:
        return JSONResponse({"error": f"need input; response_format one of {sorted(FORMATS)}"}, 400)
    try:
        await gpt.ensure_token()
    except Exception as e:
        log.error("speech failed: %s", e)
        return JSONResponse({"error": str(e)}, 502)
    jobs = [asyncio.create_task(gpt.prepare(p, spell)) for p in chunks(text)]
    try:  # fail with an error status before the 200 goes out, not with a cut stream
        await asyncio.shield(jobs[0])
    except Exception as e:
        for job in jobs:
            job.cancel()
        log.error("speech failed: %s", e)
        return JSONResponse({"error": str(e)}, 502)

    async def stream():
        try:
            for job in jobs:
                cid, mid = await job
                try:
                    async for ch in gpt.synth(cid, mid, voice, fmt):
                        yield ch
                finally:
                    await gpt.delete(cid)
        finally:
            for job in jobs:
                job.cancel()

    media = {"mp3": "audio/mpeg", "aac": "audio/aac", "opus": "audio/ogg"}[fmt]
    return StreamingResponse(stream(), media_type=media)


async def health(req: Request):
    return JSONResponse({"ok": True, "token_days_left": round(gpt.token_days_left(), 2)})


async def refresher():
    while True:
        try:
            await gpt.refresh()
        except Exception:
            log.exception("refresh")
        # wake up right after the token expires: that's when a new one is issued
        await asyncio.sleep(max(60, min(12 * 3600, gpt.token_days_left() * 86400 + 60)))


@contextlib.asynccontextmanager
async def lifespan(app):
    global gpt
    gpt = ChatGPT()
    task = asyncio.create_task(refresher())
    yield
    task.cancel()


app = Starlette(routes=[Route("/v1/audio/speech", speech, methods=["POST"]), Route("/health", health)],
                lifespan=lifespan)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8811")))
