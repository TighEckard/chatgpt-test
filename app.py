
import os
import sys
import json
import base64
import logging
from datetime import datetime
import asyncio
import requests
import websockets
from io import BytesIO
from fastapi import Depends, Header, HTTPException


from fastapi import FastAPI, WebSocket, Request, BackgroundTasks
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

import openai
from twilio.rest import Client



import re
from difflib import get_close_matches

def _norm(text: str) -> str:
    """lower-case, strip spaces, remove punctuation → for fuzzy compare"""
    return re.sub(r"[^\w]", "", text or "").lower()




# ── VOICE NORMALISATION HELPER ────────────────────────────
# 2025-06: these are the only voices OpenAI accepts now
VALID_VOICES = {"alloy", "ash", "coral", "echo", "sage", "shimmer"}

# map *unsupported* or old labels → nearest valid voice
VOICE_MAP = {
    "nova":  "alloy",     # old docs said "nova" – now use "ash"
    "onyx":  "alloy",   # pick something similar-ish
    "fable": "alloy",  # “story-telling” voice ≈ ballad
    # leave ash / coral / echo / sage / shimmer as-is
}

def normalise_voice(raw: str | None, default: str = "shimmer") -> str:
    """
    Trim & lowercase, map legacy names, then ensure it's valid.
    """
    if not raw:
        return default
    v = raw.strip().lower()
    v = VOICE_MAP.get(v, v)          # translate if needed
    return v if v in VALID_VOICES else default
# ───────────────────────────────────────────────────────────



# -------------------------------------------------------------------
# GLOBAL LOGGING: dump EVERYTHING at DEBUG level
# -------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,                 # was INFO
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True                           # overrides any previous config
)



# ── Trim the binary-payload spam coming from websockets.client ─────────────
noisy_logger = logging.getLogger("websockets.client")

class _WsNoiseFilter(logging.Filter):
    """Drop DEBUG lines that only show huge base-64 audio payloads."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # keep PING/PONG and anything that isn’t the raw audio buffer
        return '"input_audio_buffer.append"' not in msg

noisy_logger.addFilter(_WsNoiseFilter())
# you can still re-enable full trace by changing to DEBUG at deploy time
noisy_logger.setLevel(logging.INFO)          # INFO keeps connect/handshake lines










# ---------- CONCURRENCY-SAFE STATE ----------
from typing import Dict, Any

# callSid  →  {prompt, streamSid, hostname, flags…}
contexts: Dict[str, Any] = {}



















# ======================================================================
#  ULTRA-POLISH TRANSCRIPT REBUILDER
#  Goal: perfect USER / AI turns, no stutters, no duplicates, sentences
#        end with punctuation, and “It seems like It seems like” is gone.
# ======================================================================
import re, unicodedata, itertools, string, difflib

_END     = set(".!?。！？")                     # sentence stops
_PUNCT   = str.maketrans("", "", string.punctuation + "。、「」、．，！？」’“”’")
_RE_SPC  = re.compile(r"\s+")
_STUTTER = re.compile(r'\b(\w{1,4})( \1\b)+', flags=re.I)          # he he he

def _norm(txt: str) -> str:
    folded = unicodedata.normalize("NFKD", txt).encode("ascii","ignore").decode()
    folded = folded.translate(_PUNCT).lower()
    return _RE_SPC.sub(" ", folded).strip()

def _collapse_stutter(txt: str) -> str:
    """
    Remove single-word or 2–3-word stutters inside one sentence:
       'It seems like it seems like' → 'It seems like'AAAAAAAAAA
    """
    # step once for 1-word loops (‘the the’)
    txt = _STUTTER.sub(r'\1', txt)

    # step again for 2–3-word loops
    seq_pat = re.compile(r'\b((?:\w+\s+){1,3}\w+)(?:\s+\1\b)+', flags=re.I)
    return seq_pat.sub(r'\1', txt)

def _merge(lines):
    """join consecutive chunks from same speaker & stitch until sentence end"""
    out, buf, cur_spk = [], "", None
    for seg in lines:
        spk, piece = seg["speaker"], seg["text"].strip()
        if not piece:
            continue
        if spk != cur_spk:
            if buf:
                out.append({"speaker": cur_spk, "text": buf.strip()})
            cur_spk, buf = spk, piece
        else:
            buf += " " + piece
        if buf and buf[-1] in _END:
            out.append({"speaker": cur_spk, "text": buf.strip()})
            buf = ""
    if buf:
        out.append({"speaker": cur_spk, "text": buf.strip()})
    return out

def _dedupe(lines, thresh=0.90):
    """drop near-duplicate utterances, keep the longer copy"""
    buckets, cleaned = {"user":[], "ai":[]}, []
    for seg in lines:
        cand  = seg["text"]
        canon = _norm(cand)
        bucket = buckets[seg["speaker"]]
        dupe = next((old for old in bucket
                     if difflib.SequenceMatcher(None, canon, _norm(old["text"])).ratio()>=thresh),
                    None)
        if dupe:
            if len(cand) > len(dupe["text"]):
                dupe["text"] = cand
        else:
            bucket.append(seg)
            cleaned.append(seg)
    # ---------------------------------------------------------------
    # 4) sanity-check the ends:
    #    • if the very first fragment came from the AI, it was
    #      almost certainly the answer to the *preceding* question
    #      (the caller spoke first, it was transcribed a hair later).
    #      In that case we drop that opening AI line so the dialog
    #      always starts with the USER.
    #    • if the very last fragment is from the USER (the caller
    #      hung up before the AI could reply), drop that dangling
    #      question so we don’t finish on an unanswered line.
    # ---------------------------------------------------------------
    if len(cleaned) >= 2 and cleaned[0]["speaker"] == "ai" and cleaned[1]["speaker"] == "user":
        # rotate left: move the leading AI answer *after* its question
        cleaned = cleaned[1:] + cleaned[:1]

    # after rotation, if we *still* start with AI, it's a stray fragment
    if cleaned and cleaned[0]["speaker"] == "ai":
        cleaned.pop(0)

    # drop trailing dangling USER with no AI reply
    if cleaned and cleaned[-1]["speaker"] == "user":
        cleaned.pop()

    return cleaned




def _enforce_turns(lines):
    """guarantee perfect alternation; if AA or UU repeat, keep latest only"""
    out=[]
    for seg in lines:
        seg["text"]=_collapse_stutter(seg["text"])
        if out and out[-1]["speaker"]==seg["speaker"]:
            out[-1]["text"]=seg["text"]          # overwrite previous
        else:
            out.append(seg)
    return out

def _polish_transcript(raw: list[dict]) -> list[dict]:
    """
    Master pipeline:
      1) merge -> 2) dedupe -> 3) enforce alternation & de-stutter
    """
    step1 = _merge(raw)
    step2 = _dedupe(step1)
    final = _enforce_turns(step2)
    return final
















# OpenAI API key (replace with your actual key)
OPENAI_API_KEY = "SECRET"
PORT = 5050
openai.api_key = OPENAI_API_KEY



#############################
# CONFIGURE WORDPRESS SITE #
#############################
WORDPRESS_SITE_URL = "https://app.kalimba.world"
# If your WordPress endpoint needs a nonce or auth header, set it here:
# WP_API_NONCE = "SOME_NONCE_IF_NEEDED"



# Twilio credentials (use your real credentials!)
TWILIO_ACCOUNT_SID = "SECRET"
TWILIO_AUTH_TOKEN = "SECRET"


######################################
# (OLD) System prompt (NO LONGER USED)
# SYSTEM_MESSAGE = """
# You are Tigh Eckards personal receptionist...
# """

##################
# NEW GLOBALS
##################
# We'll store a user's custom prompt in a global so handle_media_stream can access it.
# This is a simple approach – for high concurrency, you'd store it by call SID instead.



#global_hostname = None
#global_user_prompt = None


# Voices -----------------------------------------------------------------
DEFAULT_VOICE = "alloy"   # new accounts start here
ALLOWED_VOICES = {"alloy", "shimmer", "echo", "coral", "sage", "ash"}


TEMPERATURE = 0.9





# Initialize Twilio client and FastAPI app
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
app = FastAPI()

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.kalimba.world",
        "https://kalimba.world",
        "https://glacial-lake-09133-1b024ab03664.herokuapp.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],   # OPTIONS, GET, POST…
    allow_headers=["*"],
)





@app.get("/initial-audio/{phone_number}")
async def serve_initial_audio(phone_number: str):
    """
    Return the user-saved greeting as audio/mpeg.
    If none exists, return 1-s of μ-law silence so Twilio does not disconnect.
    """
    # ── 1. Try WordPress for a saved greeting ────────────────────────────────
    try:
        wp_resp = requests.get(
            f"{WORDPRESS_SITE_URL}/wp-json/ai-reception/v1/get-initial-audio",
            params={"phone": phone_number},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if wp_resp.status_code == 200:
            b64 = wp_resp.json().get("audio", "")
            if b64:
                audio_bytes = base64.b64decode(b64)
                return Response(content=audio_bytes, media_type="audio/mpeg")
    except Exception as e:
        logging.warning(f"WP greeting lookup failed: {e}")

    # ── 2. Fallback: 1-second μ-law silence (0xFF) ───────────────────────────
    # Twilio supports raw 8-kHz μ-law when Content-Type is audio/ulaw
    silence_bytes = b"\xFF" * 8000          # 8000 samples ≈ 1 s
    return Response(content=silence_bytes, media_type="audio/ulaw")








def get_user_prompt_by_phone(phone: str) -> str:
    logging.debug(f"[WP:get_prompt] phone={phone}")
    try:
        endpoint = f"{WORDPRESS_SITE_URL}/wp-json/ai-reception/v1/user-from-phone"
        resp = requests.post(
            endpoint,
            json={"phone": phone},
            timeout=10
        )

        resp.raise_for_status()
        data = resp.json()
        logging.debug(f"[WP:get_prompt] HTTP {resp.status_code}")
        logging.debug(f"[WP:get_prompt] BODY {data!r}")
        return data.get("prompt", "")
    except Exception as e:
        logging.exception(f"[WP:get_prompt] EXCEPTION {e}")
    return ""


def get_user_voice_by_phone(phone: str) -> str:
    logging.debug(f"[WP:get_voice] phone={phone}")
    try:
        endpoint = f"{WORDPRESS_SITE_URL}/wp-json/ai-reception/v1/user-from-phone"
        resp = requests.post(
            endpoint,
            json={"phone": phone},
            timeout=10
        )

        resp.raise_for_status()
        data = resp.json()
        logging.debug(f"[WP:get_voice] HTTP {resp.status_code}")
        logging.debug(f"[WP:get_voice] BODY {data!r}")

        # pull the raw label from WP
        raw = data.get("voice")
        # normalise → map custom labels & enforce allowed list
        voice = normalise_voice(raw, default=DEFAULT_VOICE)
        logging.debug(f"[WP:get_voice] normalised voice = {voice}")
        return voice

    except Exception as e:
        logging.exception(f"[WP:get_voice] EXCEPTION {e}")
    # in case of any error, fallback
    return DEFAULT_VOICE












############################
# DUMMY AUTH for script editing
############################
def get_current_user():
    # For testing only. In production, do real auth.
    class User:
        id = 1
        email = "user@example.com"
    return User()

############################
# ENDPOINTS
############################

@app.get("/")
async def root():
    return JSONResponse({"status": "OK"})

@app.head("/incoming-call")
async def head_incoming_call(request: Request):
    return Response(status_code=200)











# ------------------------------
# 1) /incoming-call endpoint
# ------------------------------
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request, background_tasks: BackgroundTasks):
    form_data = await request.form()
    to_number = form_data.get("To", "")
    call_sid  = form_data.get("CallSid", "")
    hostname  = request.url.hostname

    logging.debug(f"[INCOMING] raw form_data = {dict(form_data)}")

    prompt = get_user_prompt_by_phone(to_number) or \
             "Default prompt: You are an AI receptionist. Answer calls professionally."
    voice  = get_user_voice_by_phone(to_number)

    logging.debug(f"[INCOMING] final prompt(≈{len(prompt)}ch) = {prompt[:120]!r}")
    logging.debug(f"[INCOMING] final voice = {voice}")

    contexts[call_sid] = {
    "prompt"  : prompt,
    "voice"   : voice,
    "hostname": hostname,
    "phone"   : to_number          # ← lets the WS know whose destinations to use
    }

    logging.debug(f"[INCOMING] contexts[{call_sid}] => {contexts[call_sid]}")

    greeting_url = f"https://{hostname}/initial-audio/{to_number}"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <!-- DEBUG-VOICE: {voice} -->
    <Response>
    <Play>{greeting_url}</Play>
    <Connect>
        <Stream url="wss://{hostname}/media-stream">
        <Parameter name="callSid"   value="{{CallSid}}"/>
        <Parameter name="acctPhone" value="{to_number}"/>
        <Parameter name="hostname"  value="{hostname}"/>   <!-- NEW -->
        </Stream>
    </Connect>
    </Response>"""

    return Response(content=twiml, media_type="text/xml")







@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """
    Twilio pushes raw µ-law audio to this endpoint; we proxy it to the
    OpenAI Realtime WS and stream GPT-TTS audio back.  Declaring `call_sid`
    up-front prevents “cannot access local variable call_sid” errors when
    Twilio hangs up before the SID arrives.
    """
    # ── Accept Twilio’s WebSocket ────────────────────────────────
    await websocket.accept()

    # ── Per-call state containers ───────────────────────────────
    transcript: list[dict] = []      # accumulated USER / AI turns
    call_ctx: dict         = {}      # prompt, voice, hostname, phone, call_sid …
    call_sid: str | None   = None    # defined early so inner coroutines can capture it
    keep_alive_task        = None    # will hold the ping task we start later
  # will hold the ping task we start later
    openai_ws              = None    # placeholder so nested coroutines can see it
    openai_task           = None   # will hold process_openai_responses()



    try:















            # Twilio’s “start” event will give us the SID for this audio stream
            stream_sid: str | None = None

            # Runtime flags
            ai_is_speaking      = False
            last_audio_received = None
            last_barge_in       = 0.0
            redirect_triggered  = False

            # ------------------------------------------------------------------
            # ↓↓↓ KEEP THE REST OF YOUR EXISTING HELPERS / PUMPS BELOW THIS LINE
            # ------------------------------------------------------------------


            # ---------- helpers ----------
            async def send_keep_alive():
                while True:
                    await asyncio.sleep(10)
                    if stream_sid:
                        await websocket.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": ""}
                        })

            keep_alive_task = asyncio.create_task(send_keep_alive())

            async def send_initial_voice():
                if not stream_sid:
                    return
                silence = base64.b64encode(b"\x00" * 2400).decode()   # 300 ms μ-law silence
                await websocket.send_json({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": silence}
                })

            #
            async def idle_watchdog():
                """
                Close both websockets only after we have received at least one
                audio chunk AND the line has been silent for IDLE seconds.
                """
                IDLE = 60          # seconds of real silence before hang-up
                while True:
                    await asyncio.sleep(IDLE)
                    now = asyncio.get_event_loop().time()

                    # Wait until some audio has been heard before starting the timer.
                    if last_audio_received is None:
                        continue

                    quiet_for_a_while = (now - last_audio_received) > IDLE

                    if quiet_for_a_while and not ai_is_speaking:
                        logging.info("[WATCHDOG] idle timeout – closing sockets")
                        try:
                            await openai_ws.close(code=1000, reason="idle timeout")
                        except Exception:
                            pass
                        try:
                            await websocket.close()
                        except Exception:
                            pass
                        break

            # ---------- Twilio → OpenAI pump ----------
            async def receive_from_twilio():
                nonlocal stream_sid, call_sid, openai_ws, last_barge_in


                async for raw in websocket.iter_text():
                    pkt = json.loads(raw)

                    # ─── START EVENT ─────────────────────────
                    if pkt.get("event") == "start":
                        # 1) grab stream SID and custom parameters
                        stream_sid = pkt["start"]["streamSid"]
                        custom     = pkt["start"].get("customParameters") or {}

                        call_sid = (
                            pkt["start"].get("callSid")      # only on the first event
                            or custom.get("callSid")
                            or custom.get("callsid")
                        )

                        # 2) ensure prompt + voice are loaded
                        acct_phone = custom.get("acctPhone", "")
                        entry      = contexts.get(call_sid, {})

                        if acct_phone and not entry.get("prompt"):
                            entry["prompt"] = get_user_prompt_by_phone(acct_phone) or \
                                            "Default prompt: You are an AI receptionist."
                        if acct_phone and not entry.get("voice"):
                            entry["voice"]  = get_user_voice_by_phone(acct_phone)

                        call_ctx.update(entry)
                        call_ctx["call_sid"] = call_sid
                        voice = call_ctx.get("voice", "alloy")  # fallback


                        # 3) pick model based on voice
                        model_name = (
                            "gpt-4o-mini-realtime-preview-2024-12-17"
                            if voice == "alloy"
                            else "gpt-4o-realtime-preview-2024-12-17"
                        )
                        ws_url = (
                            "wss://api.openai.com/v1/realtime"
                            f"?model={model_name}"
                            f"&voice={voice}"
                        )

                        # 4) OPEN the OpenAI realtime WebSocket
                        openai_ws = await websockets.connect(
                            ws_url,
                            extra_headers={
                                "Authorization": f"Bearer {OPENAI_API_KEY}",
                                "OpenAI-Beta":   "realtime=v1"
                            }
                        )

                        # 5) send the session.update (prompt + destinations + voice)
                        # 5) send the session.update (prompt + destinations + voice)
                        await send_session_update(
                            openai_ws,
                            prompt=entry["prompt"],
                            voice=voice,
                            phone=entry.get("phone", "")
                        )

                        # 6) NOW that the OpenAI socket exists, start the downstream pump
                        nonlocal openai_task               # declared near the top of handle_media_stream
                        if openai_task is None:            # launch only once
                            openai_task = asyncio.create_task(process_openai_responses())

                        # 7) give Twilio 300 ms of silence so it knows we’re alive
                        asyncio.create_task(send_initial_voice())



                    # ─── MEDIA EVENT ─────────────────────────
                    elif pkt.get("event") == "media":
                        if openai_ws is None:               # socket not ready yet
                            continue                        # ignore early packets

                        # If the AI is mid-sentence, stop the TTS stream so callers can barge in
                        if ai_is_speaking:
                            logging.info("[INTERRUPT] caller spoke while AI talking – cancelling response")
                            await send_stop_audio(openai_ws)
                            ai_is_speaking = False
                            last_barge_in = asyncio.get_event_loop().time()

                        await openai_ws.send(json.dumps({
                            "type":  "input_audio_buffer.append",
                            "audio": pkt["media"]["payload"]
                        }))



                    # ─── STOP EVENT ──────────────────────────
                    elif pkt.get("event") == "stop":
                        logging.info("[TWILIO] received stop – scheduling WP save and closing sockets")

                        # **1) schedule your WordPress save immediately**
                        if transcript:
                            asyncio.create_task(
                                #
                                save_call_to_wp(
                                    transcript=transcript,
                                    prompt=call_ctx.get("prompt", ""),
                                    call_sid=call_ctx.get("call_sid", ""),
                                    started_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
                                    phone=call_ctx.get("phone", "")
                                )

                            )

                        # **2) cleanly close both websockets** so this coroutine can return
                        try:
                            await openai_ws.close(code=1000, reason="twilio stop")
                        except Exception:
                            pass

                        try:
                            await websocket.close()
                        except Exception:
                            pass

                        break











            async def maybe_redirect(label: str | None, number: str | None):
                """
                Dial either a saved destination by label, or a raw +E.164 number.
                """
                nonlocal redirect_triggered, ai_is_speaking, stream_sid, call_sid

                if redirect_triggered or not call_sid:
                    return                         # already done or we don’t know the call yet

                # choose what to pass to /redirecting-call
                if label:
                    qp = f"label={requests.utils.quote(label)}"
                elif number:
                    qp = f"to={requests.utils.quote(number)}"
                else:
                    return

                # first try the current turn's hostname,
                # then fall back to the one we saved in contexts[call_sid]['hostname']
                # ──────────────────────────────────────────────────────────────
                # Find a hostname in *every* reasonable place before bailing
                # ──────────────────────────────────────────────────────────────
                # ──────────────────────────────────────────────────────────────
                # Look for a hostname in *every* possible place*
                # *including contexts that match either phone OR call-sid*
                # ──────────────────────────────────────────────────────────────
                host = (
                    call_ctx.get("hostname")                                   # 1) <Parameter hostname="…">
                    or (call_sid and contexts.get(call_sid, {}).get("hostname"))  # 2) contexts[callSid]
                )

                # 3) Search all live contexts – by phone *or* by Twilio call-sid
                if not host:
                    for ctx in contexts.values():
                        same_phone = call_ctx.get("phone") and ctx.get("phone") == call_ctx["phone"]
                        same_sid   = call_sid and ctx.get("call_sid") == call_sid
                        if (same_phone or same_sid) and ctx.get("hostname"):
                            host = ctx["hostname"]
                            break

                # 4) Environment variable fallback
                # 4) Environment-variable and hard-coded fallbacks
                host = (
                    host
                    or os.getenv("PUBLIC_HOST")                           # set this in Heroku → same for all calls
                    or "glacial-lake-09133-1b024ab03664.herokuapp.com"    # ALWAYS your FastAPI app
                    # never fall back to the WordPress domain – it can’t serve /redirecting-call
                )


                # host is now guaranteed to be non-empty







                url = f"https://{host}/redirecting-call?{qp}&phone={requests.utils.quote(call_ctx['phone'])}"



                logging.info(f"[REDIRECT] updating live call → {url}")

                try:
                    twilio_client.calls(call_sid).update(url=url, method="GET")
                    redirect_triggered = True
                    ai_is_speaking = False          # stop sending audio
                    await send_stop_audio(openai_ws) # politely cancel TTS
                except Exception as exc:
                    logging.error(f"[REDIRECT] Call.update failed: {exc}")
















            # ─────────── OpenAI → Twilio pump ─────────────────
            # ------------------------------------------------------------------
            #  OpenAI → Twilio pump  (handles GPT outputs + caller transcripts)
            # ------------------------------------------------------------------
            # --------------------------------------------------------------
            #  OpenAI ➜ Twilio pump  (GPT output + caller transcript)
            # --------------------------------------------------------------
            async def process_openai_responses():
                nonlocal ai_is_speaking, last_audio_received, redirect_triggered, stream_sid, last_barge_in

                async for raw in openai_ws:
                    # stop everything once we’ve transferred the call
                    if redirect_triggered:
                        break

                    try:
                        msg  = json.loads(raw)
                        kind = msg.get("type", "")

                        # ── 1) GPT calls redirect_call() ────────────────────




                                        
                        # ── 1) GPT calls redirect_call() ────────────────────
                        # OpenAI WS can label tool calls as:
                        #   • "response.function_call"
                        #   • "assistant.function_call"
                        #   • "response.tool_calls"  (delta style)
                        # so we catch anything that *mentions* function_call/tool_calls.
                        # ── 1) GPT calls redirect_call() ────────────────────
                        # Catch any shape of function/tool call frame
                        is_tool_frame = (
                            "function_call" in kind
                            or kind.endswith("tool_calls")
                            or kind.endswith(".function_call")
                            or kind.endswith(".tool_calls")
                        )
                        if is_tool_frame:
                            # Try to detect the tool name first
                            tool_name = (
                                msg.get("name")
                                or (msg.get("function_call") or {}).get("name")
                                or (((msg.get("tool_calls") or [{}])[0]).get("name") if msg.get("tool_calls") else None)
                            )
                            if tool_name != "redirect_call":
                                # Not our tool – ignore
                                pass
                            else:
                                logging.debug(f"[REDIRECT] raw function_call payload → {msg}")

                                # unified grab of arguments (handle JSON string & dict)
                                try:
                                    if "arguments" in msg:
                                        args = msg["arguments"]
                                        if isinstance(args, str):
                                            args = json.loads(args or "{}")
                                    elif "function_call" in msg and "arguments" in msg["function_call"]:
                                        raw_args = msg["function_call"]["arguments"]
                                        args = json.loads(raw_args or "{}") if isinstance(raw_args, str) else (raw_args or {})
                                    else:
                                        tool = (msg.get("tool_calls") or [{}])[0]
                                        args = tool.get("arguments", {})
                                except Exception as exc:
                                    logging.error(f"[REDIRECT] could not parse arguments: {exc}")
                                    args = {}

                                label  = args.get("label")
                                number = args.get("number")

                                # validate label for THIS caller before trying to redirect
                                caller_phone = call_ctx.get("phone")
                                if label and not number and not _find_dest(caller_phone, label):
                                    logging.info(f"[REDIRECT] label '{label}' not found for {caller_phone}")
                                    # ignore wrong label; do NOT break (let convo continue)
                                else:
                                    await maybe_redirect(label=label, number=number)
                                    break  # once we transfer, stop pumping further GPT audio








                        # ── 2) Caller speech transcript (Whisper) ───────────
                        elif "input_audio_transcript" in kind:
                            text = (msg.get("delta") or msg.get("transcript") or "").strip()
                            if text:
                                transcript.append({"speaker": "user", "text": text})
                                logging.info(f"[CALLER] {text}")

                        # ── 3) AI-TTS transcript (for logs) ────────────────
                        elif "response.audio_transcript" in kind:
                            text = (msg.get("delta") or msg.get("transcript") or "").strip()
                            if text:
                                transcript.append({"speaker": "ai", "text": text})
                                logging.info(f"[AI-TTS] {text}")

                        # ── 4) Assistant text deltas (visible) ─────────────
                        elif kind.startswith("response.text") or "content_part" in kind:
                            text = (msg.get("delta") or msg.get("text") or "").strip()
                            if text:
                                transcript.append({"speaker": "ai", "text": text})
                                logging.info(f"[AI-TEXT] {text}")

                        # ── 5) Audio chunks to Twilio ──────────────────────
                        elif kind.startswith("response.audio"):
                            audio_payload = msg.get("delta") or msg.get("audio")
                            if audio_payload:
                                now = asyncio.get_event_loop().time()

                                # Drop any leftover frames that arrive right after a barge-in
                                if last_barge_in and (now - last_barge_in) < 1.0:
                                    logging.debug("[INTERRUPT] skipping stale TTS frame after barge-in")
                                    continue

                                last_audio_received = now
                                ai_is_speaking = True
                                await websocket.send_json({
                                    "event":     "media",
                                    "streamSid": stream_sid,
                                    "media":     {"payload": audio_payload}
                                })

                        # ── 6) End-of-response markers ─────────────────────
                        elif kind in {"response.completed", "response.canceled", "response.stopped"}:
                            # OpenAI signals that the assistant finished speaking;
                            # clear the speaking flag so new caller audio doesn’t get treated
                            # as a late barge-in.
                            ai_is_speaking = False
                            last_audio_received = None

                        # ── 7) Errors from the OpenAI stream ───────────────
                        elif kind == "error":
                            logging.error(f"[OPENAI-ERR] {msg}")


                    except Exception as exc:
                        logging.error(f"[OPENAI-PARSE] {exc}")

                    # reset speaking flag if no audio for 10 s
                    if last_audio_received and (
                        asyncio.get_event_loop().time() - last_audio_received > 10
                    ):
                        ai_is_speaking = False
                        last_audio_received = None











            # run both pumps until one finishes
            # run all tasks – any one finishing ends the call
            # run all concurrent jobs until ANY of them finishes
            # run our core tasks; include the AI-pump only after it exists
            tasks = [
                asyncio.create_task(receive_from_twilio()),
                asyncio.create_task(idle_watchdog()),
            ]

            # openai_task is created inside receive_from_twilio ⇢ “start” event
            # wait for it as soon as it appears
            while True:
                if openai_task is not None and openai_task not in tasks:
                    tasks.append(openai_task)
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                # if any pending tasks remain (e.g. openai_task started later), keep waiting
                if not pending:
                    break
                tasks = list(pending)





    except Exception as e:
        logging.error(f"[MEDIA] fatal: {e}")




    finally:
        # ── stop the keep-alive, if running ─────────────────────────────
        if keep_alive_task:
            keep_alive_task.cancel()

        # ── save the transcript to WordPress (only if we have anything) ─
        if transcript:
            try:
                logging.debug("[MEDIA] FINALLY: about to save_call_to_wp()")
                await asyncio.wait_for(
                    save_call_to_wp(
                        transcript=transcript,             # ← raw list, no extra formatting
                        prompt=call_ctx.get("prompt", ""),
                        call_sid=call_ctx.get("call_sid", ""),
                        started_at=datetime.utcnow()
                                           .isoformat(timespec="seconds") + "Z"
                    ),
                    timeout=20                            # give it max 20 s before Heroku kills us
                )
                logging.info("[MEDIA] FINALLY: WP save completed")
            except asyncio.TimeoutError:
                logging.error("[MEDIA] FINALLY: WP save TIMED-OUT (20 s)")
            except Exception as exc:
                logging.error(f"[MEDIA] FINALLY: WP save raised {exc!r}")
        else:
            logging.warning("[MEDIA] FINALLY: transcript empty – skipping WP save")

        # ── always try to close the Twilio WebSocket ───────────────────
        try:
            await websocket.close()
        except Exception:
            pass

        logging.info("[MEDIA] WebSocket closed")

















# ======================================================================
#  REDIRECTING CALL  –  dynamic destinations (2025-06-19 rev-D)
# ======================================================================

import time
from xml.sax.saxutils import escape as _xml_escape

# --- simple in-memory cache (refresh every 5 min) --------------------
# ------------ per-user destination cache ----------------------------
# key   = account phone number  (e.g. "+15551234567")
# value = list[dict]            (destinations for that account)
_DEST_CACHE: dict[str, list] = {}
_DEST_TIME : dict[str, float] = {}
_DEST_TTL  = 300   # seconds
# --------------------------------------------------------------------

def _fetch_destinations(phone: str) -> list[dict]:
    """
    Ask WordPress for *this* account’s list:
      GET /destinations?phone=+1555…
    (WordPress handler returns only rows owned by that user.)
    """
    url = f"{WORDPRESS_SITE_URL}/wp-json/ai-reception/v1/destinations-by-phone"
    try:
        r = requests.get(url, params={"phone": phone}, timeout=10)
        r.raise_for_status()
        data = r.json() or []
        logging.debug(f"[DEST] {phone}: fetched {len(data)} rows")
        return data
    except Exception as exc:
        logging.error(f"[DEST] fetch failed for {phone}: {exc}")
        return []

def _destinations(phone: str) -> list[dict]:
    now = time.time()
    if (phone not in _DEST_CACHE) or (now - _DEST_TIME.get(phone, 0) > _DEST_TTL):
        _DEST_CACHE[phone] = _fetch_destinations(phone)
        _DEST_TIME[phone]  = now
    return _DEST_CACHE[phone]

def _find_dest(phone: str, label: str) -> dict | None:
    """case-insensitive match inside that user’s list"""
    label_lc = label.strip().lower()
    return next((d for d in _destinations(phone)
                 if d.get("label", "").lower() == label_lc), None)


















# ──────────────────────────────────────────────────────────────
#  Helper: build the <Dial> TwiML that Twilio needs
# ──────────────────────────────────────────────────────────────
from xml.sax.saxutils import escape as _xml_escape     # already imported earlier

def _twiml_dial(number: str, ext: str = "") -> str:
    """
    Return a minimal <Response><Dial>… XML.

    Parameters
    ----------
    number : str
        Destination in +E.164.
    ext    : str
        Optional 1-6 digit extension to send _after_ the call answers.
        We insert “ww” (½-s pauses) in front so the PBX has time to pick up.

    Example
    -------
        _twiml_dial("+18125551234", "4321") →
          <?xml version="1.0" encoding="UTF-8"?>
          <Response>
            <Dial>
              <Number sendDigits="ww4321#">+18125551234</Number>
            </Dial>
          </Response>
    """
    number = _xml_escape(number)

    if ext and ext.isdigit():
        #  ‘ww’ = 1-s pause; trailing ‘#’ tells many IVRs “done”
        send = "ww" + ext + "#"
        inner = f'<Number sendDigits="{send}">{number}</Number>'
    else:
        inner = number

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Dial>' + inner + '</Dial></Response>'
    )



















# ---------- /redirecting-call  ---------------------------------------
@app.api_route("/redirecting-call", methods=["GET", "POST"])
async def handle_redirecting_call(request: Request):


    """
    Accepts either…
      • ?to=+15551234567        – dial that number
      • ?label=HR               – look up saved destination
    Optional:
      • ?ext=1234               – override or supply post-answer DTMF
    """
    qs      = request.query_params
    phone   = request.query_params.get("phone", "")
    raw_to  = qs.get("to", "").strip()
    raw_lbl = qs.get("label", "").strip()
    ext_qs  = qs.get("ext", "").strip()

    target_num = ""
    target_ext = ext_qs

    # direct number
    if raw_to:
        target_num = raw_to

    # label lookup
    elif raw_lbl:
        
        # ---------- tolerant destination lookup ----------
        desired = _norm(raw_lbl)

        # 1) exact match (case- / space- / punctuation-insensitive)
        dest = next((d for d in _destinations(phone)
                    if _norm(d.get("label")) == desired), None)

        # 2) close-match fallback (handles small typos)
        if not dest:
            choices   = {_norm(d.get("label")): d for d in _destinations(phone)}
            match_key = next(iter(get_close_matches(desired, choices.keys(), n=1, cutoff=0.7)), None)
            dest      = choices.get(match_key)

        # 3) still nothing → polite apology instead of 404 / crash
        if not dest:
            logging.warning(f"[REDIRECT] label '{raw_lbl}' not found for {phone}")
            return Response(
                content=(
                    "<?xml version='1.0' encoding='UTF-8'?>"
                    "<Response><Say voice='Polly.Amy'>"
                    "Sorry, I couldn’t reach that department.</Say></Response>"
                ),
                media_type="text/xml",
                status_code=200,
            )

        # success → extract number / extension
        target_num = dest.get("number", "")
        target_ext = dest.get("ext", "") or ext_qs


    # sanity-check
    if not target_num.startswith("+"):
        logging.error("[REDIRECT] missing or bad target number")
        return Response(status_code=400)

    twiml = _twiml_dial(target_num, target_ext)
    return Response(content=twiml, media_type="text/xml")



















############################
# PROVISIONING ENDPOINTS
############################
from twilio.base.exceptions import TwilioRestException
from twilio.base.page import Page
from fastapi.responses import JSONResponse
from functools import lru_cache

def _collect_all(pages: Page) -> list:
    """Helper: iterate through a Twilio paging generator into one list."""
    out = []
    for n in pages:
        out.append(n.phone_number)
    return out

@lru_cache(maxsize=256)        # cache results for identical queries
def _local_prefix(prefix6: str, full: str) -> list[str]:
    """
    Ask Twilio for every local number containing `prefix6`
    (max 1 000 – Twilio hard limit) then return only those
    whose national part starts with `full`.
    """
    nums = []
    try:
        pages = twilio_client.available_phone_numbers("US") \
                             .local.list(contains=prefix6,
                                          limit=1000,   # highest allowed
                                          page_size=100)
        for pn in pages:
            nat = pn.phone_number.lstrip("+1")
            if nat.startswith(full):
                nums.append(pn.phone_number)
    except TwilioRestException as e:
        logging.warning(f"Local search error: {e.status} {e.msg}")
    return nums


@app.post("/api/search-numbers")
async def search_numbers(request: Request, x_wp_nonce: str = Header(None)):
    """
    POST { "query": "digits" }  ->  { "numbers": [ "+1…" ] }
      • 3 digits  -> area-code search
      • 4-10      -> prefix search (uses first 6 digits on Twilio, local filter)
    """
    try:
        body   = await request.json()
        digits = body.get("query", "").strip()

        if not digits.isdigit() or len(digits) < 3:
            return JSONResponse({"error": "Enter at least 3 digits"}, 400)
        if len(digits) > 10:
            return JSONResponse({"error": "No more than 10 digits"}, 400)

        # ---------- area-code (exactly 3) ----------
        if len(digits) == 3:
            try:
                ac = twilio_client.available_phone_numbers("US") \
                                   .local.list(area_code=int(digits), limit=1000)
                numbers = [n.phone_number for n in ac]
            except TwilioRestException as e:
                logging.error(f"Area-code search error: {e.status} {e.msg}")
                numbers = []
            return JSONResponse({"numbers": numbers}, 200)

        # ---------- prefix (4-10) ----------
        prefix6 = digits[:6]
        numbers = _local_prefix(prefix6, digits)

        # optional: toll-free when ≤7 digits
        if len(digits) <= 7:
            try:
                tf_pages = twilio_client.available_phone_numbers("US") \
                                        .toll_free.list(contains=digits,
                                                        limit=100,
                                                        page_size=100)
                for pn in tf_pages:
                    nat = pn.phone_number.lstrip("+1")
                    if nat.startswith(digits):
                        numbers.append(pn.phone_number)
            except TwilioRestException as e:
                logging.info(f"Toll-free skipped: {e.status} {e.msg}")

        return JSONResponse({"numbers": numbers}, 200)

    except Exception as e:
        logging.error(f"search_numbers fatal: {e}")
        return JSONResponse({"error": "Server error"}, 500)





























 
@app.post("/api/provision-number")
async def provision_number(request: Request, x_wp_nonce: str = Header(None)):
    from fastapi.responses import JSONResponse

    """
    Purchase a Twilio phone number and configure it to forward incoming calls
    to your AI receptionist endpoint.
    """
    try:
        body_bytes = await request.body()
        if not body_bytes:
            return JSONResponse({"error": "No JSON data provided."}, status_code=400)

        data = json.loads(body_bytes.decode("utf-8"))
        selected_number = data.get("selected_number")
        if not selected_number:
            return JSONResponse({"error": "selected_number is required"}, status_code=400)

        # Purchase the number and set its incoming-call webhook
        purchased_number = twilio_client.incoming_phone_numbers.create(
            phone_number=selected_number,
            voice_url="https://glacial-lake-09133-1b024ab03664.herokuapp.com/incoming-call",
            voice_method="POST",
        )

        return JSONResponse(
            {"phone_number": purchased_number.phone_number},
            status_code=200
        )

    except Exception as e:
        logging.error(f"Error purchasing number from Twilio: {e}")
        return JSONResponse({"error": str(e)}, status_code=400)








############################
# USER SCRIPT EDITING (existing code)
############################
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(BASE_DIR, "user_scripts")

@app.get("/script")
async def get_script(current_user=Depends(get_current_user)):
    user_id = current_user.id
    file_path = os.path.join(SCRIPTS_DIR, f"user_{user_id}_app.py")
    phone_file_path = os.path.join(SCRIPTS_DIR, f"user_{user_id}_phone.txt")
    logging.info(f"User {user_id} ({current_user.email}) is requesting their script from: {file_path}")
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    try:
        with open(file_path, "r") as f:
            code = f.read()
        logging.info(f"Successfully loaded script for user {user_id}.")
    except FileNotFoundError as e:
        logging.error(f"File not found: {file_path}. Exception: {e}")
        code = "# Default AI Receptionist Script\nprint('Hello from your AI receptionist!')\n"
        try:
            with open(file_path, "w") as f:
                f.write(code)
            logging.info(f"Default script created for user {user_id}.")
        except Exception as write_error:
            logging.error(f"Failed to create default script file for user {user_id}: {write_error}")
            raise HTTPException(status_code=500, detail=str(write_error))
    except Exception as e:
        logging.error(f"Error reading file for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Load phone number
    try:
        with open(phone_file_path, "r") as pf:
            phone_number = pf.read().strip()
        logging.info(f"Loaded phone number for user {user_id}: {phone_number}")
    except FileNotFoundError:
        logging.warning(f"Phone number file not found for user {user_id}. Using default value.")
        phone_number = "No number provisioned"
    except Exception as e:
        logging.error(f"Error reading phone number file for user {user_id}: {e}")
        phone_number = "Error reading number"

    return {"user_id": user_id, "user_email": current_user.email, "phone_number": phone_number, "code": code}


@app.post("/script")
async def save_script(payload: dict, current_user=Depends(get_current_user)):
    code = payload.get("code", "")
    user_id = current_user.id
    file_path = os.path.join(SCRIPTS_DIR, f"user_{user_id}_app.py")
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    try:
        with open(file_path, "w") as f:
            f.write(code)
    except Exception as e:
        logging.error(f"Error saving file for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Could not save file: {e}")
    return {"status": "success"}

############################
# PROMPT EDITING (existing code)
############################
@app.get("/prompt")
async def get_prompt(current_user=Depends(get_current_user)):
    user_id = current_user.id
    file_path = os.path.join(SCRIPTS_DIR, f"user_{user_id}_prompt.txt")
    logging.info(f"User {user_id} ({current_user.email}) is requesting their prompt from: {file_path}")
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    try:
        with open(file_path, "r") as f:
            prompt = f.read()
        logging.info(f"Successfully loaded prompt for user {user_id}.")
    except FileNotFoundError as e:
        logging.error(f"Prompt file not found: {file_path}. Exception: {e}")
        prompt = "Default prompt: You are an AI receptionist. Answer calls professionally."
        try:
            with open(file_path, "w") as f:
                f.write(prompt)
            logging.info(f"Default prompt created for user {user_id}.")
        except Exception as write_error:
            logging.error(f"Failed to create default prompt file for user {user_id}: {write_error}")
            raise HTTPException(status_code=500, detail=str(write_error))
    except Exception as e:
        logging.error(f"Error reading prompt for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"user_id": user_id, "user_email": current_user.email, "prompt": prompt}


@app.post("/prompt")
async def save_prompt(payload: dict, current_user=Depends(get_current_user)):
    prompt = payload.get("prompt", "")
    user_id = current_user.id
    file_path = os.path.join(SCRIPTS_DIR, f"user_{user_id}_prompt.txt")
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    try:
        with open(file_path, "w") as f:
            f.write(prompt)
        logging.info(f"Prompt saved for user {user_id}.")
    except Exception as e:
        logging.error(f"Error saving prompt for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Could not save prompt: {e}")
    return {"status": "success"}

############################
# NEW send_session_update that uses the custom prompt
############################
# ──────────────────────────────────────────────────────────────
#  Send initial session.update  ➜  OpenAI Realtime WebSocket
#  (now injects the caller’s *own* destination labels)
# ──────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────
#  Send initial session.update  – inject department list
# ──────────────────────────────────────────────────────────
async def send_session_update(
    openai_ws,
    *,
    prompt: str,
    voice: str,
    phone: str,
):
    """
    Build the routing-first instructions, send them as a session.update
    frame, and return the dict.
    """
    # 1) Fetch destinations for this phone
    dests = _destinations(phone) or []
    dest_lines = [
        f"• **{d.get('label','(no label)')}** – {d.get('description', '(no description)')}"
        for d in dests
    ]

    # 2) Compose the instruction text
    sys_parts: list[str] = [prompt.strip()]
    if dests:
        sys_parts += [
            "**You are a call-router. Your primary job is to transfer** the caller to the correct destination as quickly and politely as possible.",
            "### Transfer options\n" + "\n".join(dest_lines),
            "Use the `redirect_call` tool **only** with the `label` that exactly matches one of the bullets above.",
            "When the caller’s request clearly matches one of the departments, ask if they would like you to be transferred. Only call `redirect_call` after they explicitly confirm.",
            "If the caller declines all transfers, then answer questions yourself."
        ]
    else:
        sys_parts.append("⚠️  No transfer destinations are configured – just answer questions.")

    instructions = "\n\n".join(sys_parts)
    logging.debug(f"[SESSION] built instructions for {phone}:\n{instructions}")

    # 3) Build the tools list ONLY if we actually have destinations
    tools = []
    if dests:
        tools.append({
            "name": "redirect_call",
            "type": "function",
            "description": "Connect the live caller to another phone number",
            "parameters": {
                "type": "object",
                "properties": {
                    "label":  {"type": "string", "description": "One of the configured transfer labels"},
                    "number": {"type": "string", "description": "Fallback: E.164 number if no label applies"}
                },
                "required": [],    # let model pick label OR number
            },
        })

    # 4) Build and send frame
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format":  "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": voice,
            "instructions": instructions,
            "modalities": ["text", "audio"],
            "temperature": TEMPERATURE,
            "input_audio_transcription": {"model": "whisper-1"},
            "tools": tools,
        },
    }

    await openai_ws.send(json.dumps(session_update))
    return session_update









async def send_stop_audio(openai_ws):
    try:
        stop_audio = {"type": "response.cancel"}
        await openai_ws.send(json.dumps(stop_audio))
        logging.debug("Sent stop audio command to OpenAI.")
    except Exception as e:
        logging.error(f"Failed to send stop_audio command to OpenAI: {e}")














@app.post("/preview-tts")
async def preview_tts(request: Request):
    body = await request.json()
    text  = (body.get("text") or "").strip()
    voice = body.get("voice", DEFAULT_VOICE)

    if not text:
        raise HTTPException(status_code=400, detail="No text provided")
    if voice not in ALLOWED_VOICES:
        voice = DEFAULT_VOICE

    resp = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type":  "application/json"
        },
        json={
            "model": "tts-1",
            "voice": voice,
            "input": text
        },
        timeout=30
    )

    # 3) Error check
    if resp.status_code != 200:
        logging.error(f"TTS HTTP error {resp.status_code}: {resp.text}")
        return JSONResponse(
            status_code=500,
            content={"error": f"TTS HTTP {resp.status_code}: {resp.text}"}
        )

    # 4) Return raw audio bytes if that's what we got
    ctype = resp.headers.get("Content-Type", "")
    if ctype.startswith("audio/"):
        return StreamingResponse(BytesIO(resp.content), media_type=ctype)

    # 5) Otherwise decode JSON->base64
    data = resp.json()
    audio_b64 = data.get("audio", "")
    if not audio_b64:
        return JSONResponse(
            status_code=500,
            content={"error": "No `audio` field in TTS JSON response"}
        )

    audio_bytes = base64.b64decode(audio_b64)
    return StreamingResponse(BytesIO(audio_bytes), media_type="audio/mpeg")




async def save_call_to_wp(*, transcript, prompt, call_sid, started_at, phone):
    """
    POST the finished call into WordPress as a private `call_log` post.
    """
    # ── NEW: squash the word-by-word deltas ───────────────────────────
    cleaned = _polish_transcript(transcript)
    logging.debug(f"[WP-SAVE] after squash → {len(transcript)} lines")

    logging.debug(f"[WP-SAVE] ENTER save_call_to_wp: call_sid={call_sid!r}, prompt_len={len(prompt)}")
    logging.debug(f"[WP-SAVE] transcript length = {len(transcript)}")

    wp_user = os.environ.get("WP_API_USER")
    wp_pass = os.environ.get("WP_API_APP_PW")
    if not wp_user or not wp_pass:
        logging.error("[WP-SAVE] missing WP_API_USER or WP_API_APP_PW – aborting")
        return

    endpoint = f"{WORDPRESS_SITE_URL}/wp-json/wp/v2/call_log"
    auth_hdr = base64.b64encode(f"{wp_user}:{wp_pass}".encode()).decode()
    headers  = {
        "Authorization": f"Basic {auth_hdr}",
        "Content-Type" : "application/json"
    }
    payload = {
        "title":  f"Call {started_at}",
        "status": "publish",                # ← was  "private"
        "content": json.dumps(cleaned, ensure_ascii=False),
        "meta": {
            "prompt_used": prompt,
            "call_sid":    call_sid,
            "owner_phone": phone or ""
        }
    }

    logging.debug(f"[WP-SAVE] payload = {payload!r}")

    loop = asyncio.get_event_loop()

    def do_post():
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=15)
        logging.debug(f"[WP-SAVE] POST status = {resp.status_code}")
        logging.debug(f"[WP-SAVE] POST body   = {resp.text[:300]} …")
        resp.raise_for_status()
        return resp

    try:
        resp = await loop.run_in_executor(None, do_post)
        logging.info(f"[WP-SAVE] SUCCESS: saved call_log ({len(transcript)} lines), HTTP {resp.status_code}")
    except Exception as exc:
        logging.error(f"[WP-SAVE] failed: {exc!r}")




from fastapi import HTTPException

@app.get("/debug-full-prompt/{phone}")
async def debug_full_prompt(phone: str):
    base = get_user_prompt_by_phone(phone)
    if not base:
        raise HTTPException(404, "No base prompt for that number")
    full = (
        base
        + " If the caller asks to be connected, call redirect_call "
          "with the correct department label or phone number."
    )
    return {"full_prompt": full}













@app.get("/debug-voice/{phone}")
async def debug_voice(phone: str):
    """
    Return exactly whatever WordPress has saved for this number
    (so you can confirm FastAPI sees the right voice).
    """
    voice = get_user_voice_by_phone(phone)
    return {"voice": voice}





############################
# MAIN
############################
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5050))
    uvicorn.run(app, host="0.0.0.0", port=port)

