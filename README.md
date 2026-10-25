# arbor-chatgpt-voice

There is no way to get OpenAI arbor voice personality from OpenAI API.
However, I consider it the best one.

Arbor is the voice ChatGPT uses for **Read Aloud** when you pick *Arbor* in settings. In Russian
(and other non-English languages) it sounds like a machine with a strong British accent, which is
exactly the point. The public API does not have it: `/v1/audio/speech`, `gpt-4o-mini-tts` and the
Realtime models reject `arbor`, and the Realtime `quicksilver` voices are only reachable from
ChatGPT itself.

This is a small server that gives you Arbor behind an OpenAI-compatible endpoint:

```sh
curl localhost:8811/v1/audio/speech -H 'Content-Type: application/json' \
  -d '{"input":"Hello from Arbor.","voice":"arbor"}' -o out.mp3
```

The response is a streamed MP3, so any OpenAI client works if you point `base_url` at it.

It powers [t3-code-voice-skill](https://github.com/iliyasone/t3-code-voice-skill): coding agents
in T3 Code that answer out loud.

## How it works

ChatGPT only voices *assistant* messages, through
`GET /backend-api/synthesize?message_id=…&conversation_id=…&voice=fathom` (`fathom` is Arbor's
internal id). So for every chunk of text the server:

1. sends it to a new ChatGPT chat with a "repeat verbatim" prompt,
2. checks the reply matches the input (one retry if it doesn't),
3. streams `/synthesize` for that reply,
4. deletes the chat.

No browser is involved: TLS is impersonated with [curl_cffi](https://github.com/lexiforest/curl_cffi)
and the sentinel proof-of-work is solved locally. Long text is split into ~1200-character chunks
that are prepared in parallel and played in order.

Latency is about 5–10 s to the first audio. 2800 characters (≈3 minutes of speech) take ~45 s in
total.

## Setup

1. Log into ChatGPT in a browser with a **spare** account (a free one is enough), select the Arbor
   voice, then open `https://chatgpt.com/api/auth/session` and save:

   ```json
   {"a": "<accessToken>", "s": "<sessionToken>", "ua": "<your browser's navigator.userAgent>"}
   ```

   to `~/.config/chatgpt-arbor/creds.json` (`chmod 600`).

2. Run it with [uv](https://docs.astral.sh/uv/):

   ```sh
   uv run arbor_tts.py          # listens on 127.0.0.1:8811
   ```

   or install `arbor-tts.service` as a systemd unit.

The access token lives 10 days; the session lasts 90 days and slides forward on every refresh. The
server refreshes the session every 12 hours and writes the rotated cookies back to the creds file,
so it keeps working as long as it runs at least once in a while. ChatGPT hands out a new access
token only after the old one has expired, so the server also refreshes right after expiry and
before any request that finds the token expired.

## API

`POST /v1/audio/speech`

- `input`: text to speak.
- `voice`: `arbor` (default), `spruce`, `sol`, or any internal ChatGPT voice id (`vale`, `breeze`,
  `cove`, `juniper`, `ember`, `maple`).
- `response_format`: `mp3` (default), `aac`, `opus`.
- `spell_numbers`: `false` by default. Read Aloud reads digits badly (versions, dates, list numbers,
  long codes), so with `true` the chat rewrites every number as words first ("22.10" becomes
  "двадцать второе октября", "1." becomes "Первое."), keeping the rest of the text word for word.
  The prompt is Russian; it adds no latency, since the chat turn happens anyway.

`GET /health` returns how many days the access token has left.

Environment: `PORT` (8811), `HOST` (127.0.0.1), `ARBOR_CREDS` (path to creds), `ARBOR_TTS_KEY`
(if set, requests need `Authorization: Bearer <key>`).

## Caveats

- This uses ChatGPT's private web API. It is against OpenAI's terms of use and the account can be
  banned. Use a spare account.
- It works because ChatGPT currently accepts messages with the proof-of-work token alone. If it
  starts enforcing its Turnstile/fingerprint checks, sending will break.
- The free plan has message limits.
- The model occasionally "fixes" the text instead of repeating it; mismatches are retried once and
  logged.
