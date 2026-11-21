"""Maths Vertex WhatsApp agent on the official WhatsApp Business Cloud API.

Inbound-first: a parent messages the business number, Meta calls POST /webhook,
and the agent replies. It answers from the FAQ sheet, qualifies the lead
(Grade, Board, challenges), books demo classes from the Slots sheet and keeps
the Leads sheet up to date. Runs anywhere that can run a Docker container.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import gspread
import httpx
from anthropic import AsyncAnthropic
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mathsvertex")

# -------------------------
# Configuration (environment variables, see .env.example)
# -------------------------
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v23.0")
MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
IST = timezone(timedelta(hours=5, minutes=30))
UAE = timezone(timedelta(hours=4))
HUMAN_PAUSE_SECONDS = 7200  # pause the bot in a chat for 2h when a person on the team replies by hand
STALE_AFTER_SECONDS = int(os.environ.get("STALE_AFTER_SECONDS", "1800"))  # skip old messages Meta redelivers after downtime
HISTORY_TURNS = 16
HISTORY_TTL = 30 * 86400
LEAD_COLS = ["Phone", "Parent Name", "Child Name", "Grade", "Board", "Challenges", "Status", "Demo Slot", "Last Message", "Updated At", "Notes"]
OPEN_STATUSES = {"", "open", "available", "free"}
HUMAN_STATUSES = {"human", "human handling", "team handling"}  # set in the Leads sheet to pause the bot for that parent
FALLBACK_REPLY = "Thanks for messaging Maths Vertex! Our team will get back to you shortly."
NON_TEXT_REPLY = "Hi, I can only read text messages here. Could you type your question?"
IGNORED_TYPES = {"reaction", "system", "unsupported", "unknown", "request_welcome", "ephemeral"}


def cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# -------------------------
# Small helpers
# -------------------------
def digits(value: Any) -> str:
    return "".join(c for c in str(value or "") if c.isdigit())


def pad(row: list, n: int) -> list:
    return (list(row) + [""] * n)[:n]


# -------------------------
# Cache: Redis when REDIS_URL is set, otherwise in-process memory (lost on restart)
# -------------------------
class Store:
    def __init__(self) -> None:
        self.mem: dict[str, tuple[str, float | None]] = {}
        self.redis = None
        url = cfg("REDIS_URL")
        if url:
            import redis.asyncio as aioredis

            self.redis = aioredis.from_url(url, decode_responses=True)

    def _purge(self, key: str) -> None:
        item = self.mem.get(key)
        if item and item[1] is not None and item[1] < time.time():
            del self.mem[key]

    async def get(self, key: str) -> str | None:
        key = "mv:" + key
        if self.redis:
            return await self.redis.get(key)
        self._purge(key)
        item = self.mem.get(key)
        return item[0] if item else None

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        key = "mv:" + key
        if self.redis:
            return bool(await self.redis.set(key, value, ex=ex, nx=nx))
        self._purge(key)
        if nx and key in self.mem:
            return False
        self.mem[key] = (value, time.time() + ex if ex else None)
        return True

    async def delete(self, key: str) -> None:
        key = "mv:" + key
        if self.redis:
            await self.redis.delete(key)
        else:
            self.mem.pop(key, None)


store = Store()


# -------------------------
# WhatsApp Cloud API
# -------------------------
def graph_url(path: str) -> str:
    return f"https://graph.facebook.com/{GRAPH_VERSION}/{cfg('WHATSAPP_PHONE_NUMBER_ID')}/{path}"


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg('WHATSAPP_TOKEN')}"}


async def send_whatsapp_reply(phone: str, message: str) -> None:
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": phone, "type": "text", "text": {"preview_url": False, "body": message[:4000]}}
    async with httpx.AsyncClient(timeout=20) as client:
        for attempt in range(2):
            resp = await client.post(graph_url("messages"), json=payload, headers=auth_headers())
            if resp.status_code < 500:
                break
            await asyncio.sleep(2)
    if resp.status_code >= 400:
        raise RuntimeError(f"WhatsApp send failed {resp.status_code}: {resp.text[:300]}")
    try:
        sent_id = resp.json()["messages"][0]["id"]
        # remember our own message ids so a message echo is never mistaken for a person on the team replying
        await store.set(f"sent:{sent_id}", "1", ex=86400)
    except (KeyError, IndexError, ValueError):
        pass


async def mark_read(message_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(graph_url("messages"), json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id}, headers=auth_headers())
    except httpx.HTTPError:
        log.info("Could not mark the message as read, continuing")


# -------------------------
# Google Sheets (service account)
# -------------------------
class Sheets:
    def __init__(self) -> None:
        self._sh = None

    def _open(self):
        if self._sh is None:
            raw = cfg("GOOGLE_SERVICE_ACCOUNT_JSON")
            if raw:
                if not raw.startswith("{"):
                    raw = base64.b64decode(raw).decode()
                gc = gspread.service_account_from_dict(json.loads(raw))
            else:
                gc = gspread.service_account(filename=cfg("GOOGLE_APPLICATION_CREDENTIALS"))
            self._sh = gc.open_by_key(cfg("SHEET_ID"))
        return self._sh

    @staticmethod
    async def _call(fn, *args, **kwargs):
        for attempt in range(4):
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except gspread.exceptions.APIError as exc:
                code = getattr(exc.response, "status_code", None)
                if code not in (429, 500, 502, 503) or attempt == 3:
                    raise
                delay = 3 * (2**attempt)
                log.warning("Google Sheets %s, retrying in %ss", code, delay)
                await asyncio.sleep(delay)

    async def read(self, ranges: list[str]) -> list[list[list[str]]]:
        result = await self._call(lambda: self._open().values_batch_get(ranges))
        value_ranges = result.get("valueRanges") or []
        out = [(vr.get("values") or []) for vr in value_ranges]
        return out + [[]] * max(0, len(ranges) - len(out))

    async def write(self, sheet: str, first_cell: str, values: list[list[Any]]) -> None:
        await self._call(lambda: self._open().values_update(f"{sheet}!{first_cell}", params={"valueInputOption": "USER_ENTERED"}, body={"values": values}))

    async def append(self, sheet: str, values: list[list[Any]]) -> None:
        await self._call(
            lambda: self._open().values_append(f"{sheet}!A1", params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}, body={"values": values})
        )


sheets = Sheets()


# -------------------------
# Lead / slot / FAQ session
# -------------------------
class Session:
    """One inbound message's view of the sheet, plus the actions the agent may take."""

    flag_reason: str = ""

    def __init__(self, phone: str, from_name: str, leads: list, slots: list, faq: list):
        self.phone = phone
        self.row_no: int | None = None
        self.lead: dict[str, str] = {c: "" for c in LEAD_COLS}
        self.lead["Phone"] = phone
        self.lead["Status"] = "New"
        self.lead["Parent Name"] = from_name
        for i, row in enumerate(leads):
            padded = pad(row, len(LEAD_COLS))
            if digits(padded[0]) == phone:
                self.row_no = i + 2
                self.lead = {c: str(v) for c, v in zip(LEAD_COLS, padded)}
                self.lead["Phone"] = phone
                if not self.lead["Status"]:
                    self.lead["Status"] = "New"
                break
        self.slots = []
        for i, row in enumerate(slots):
            date, time_, status, _booked_by = pad(row, 4)
            if str(date).strip() and str(time_).strip():
                self.slots.append({"row": i + 2, "date": str(date).strip(), "time": str(time_).strip(), "status": str(status).strip().lower()})
        faq_lines = []
        for row in faq:
            q, a = pad(row, 2)
            if str(q).strip() and str(a).strip():
                faq_lines.append(f"Q: {str(q).strip()}\nA: {str(a).strip()}")
        self.faq_text = "\n\n".join(faq_lines)[:12000]

    async def persist(self, last_message: str) -> None:
        self.lead["Last Message"] = last_message[:300]
        self.lead["Updated At"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
        row = ["'" + self.phone] + [self.lead[c] for c in LEAD_COLS[1:]]
        if self.row_no:
            await sheets.write("Leads", f"A{self.row_no}", [row])
        else:
            await sheets.append("Leads", [row])

    async def run_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "save_lead":
                return self.save_lead(args)
            if name == "list_open_slots":
                return self.list_open_slots()
            if name == "book_demo":
                return await self.book_demo(args.get("slot_id"))
            if name == "flag_for_team":
                self.lead["Status"] = "Needs Follow-up"
                self.flag_reason = str(args.get("reason", ""))[:200]
                self.add_note("Needs team follow-up: " + self.flag_reason)
                return {"ok": True}
            if name == "add_note":
                self.add_note(str(args.get("note", "")))
                return {"ok": True}
            return {"error": f"unknown tool {name}"}
        except Exception:  # tool failures go back to the model, never crash the reply
            log.exception("Tool %s failed", name)
            return {"error": "tool failed, tell the parent the team will confirm shortly"}

    def add_note(self, note: str) -> None:
        """Append a dated one-line note to the lead's Notes cell, keeping the most recent 2000 characters."""
        note = note.strip()
        if not note:
            return
        entry = f"[{datetime.now(UAE).strftime('%d %b %Y')}] {note[:300]}"
        self.lead["Notes"] = (self.lead["Notes"] + "\n" + entry).strip()[-2000:]

    def save_lead(self, args: dict[str, Any]) -> dict[str, Any]:
        mapping = {"parent_name": "Parent Name", "child_name": "Child Name", "grade": "Grade", "board": "Board", "challenges": "Challenges"}
        for key, col in mapping.items():
            value = args.get(key)
            if value is not None and str(value).strip():
                self.lead[col] = str(value).strip()[:300]
        if self.lead["Status"] not in ("Demo Booked", "Needs Follow-up", "Opted Out"):
            complete = all(self.lead[c] for c in ("Grade", "Board", "Challenges"))
            self.lead["Status"] = "Qualified" if complete else "Qualifying"
        return {"ok": True, "lead": {c: self.lead[c] for c in ("Parent Name", "Child Name", "Grade", "Board", "Challenges", "Status")}}

    def list_open_slots(self) -> dict[str, Any]:
        # Several teachers share each time; show each date+time once, keep the first free teacher's row.
        seen: set[tuple[str, str]] = set()
        open_slots = []
        for s in self.slots:
            key = (s["date"], s["time"])
            if s["status"] in OPEN_STATUSES and key not in seen:
                seen.add(key)
                open_slots.append(s)
            if len(open_slots) >= 10:
                break
        return {"open_slots": [{"slot_id": s["row"], "date": s["date"], "time": s["time"]} for s in open_slots]}

    async def book_demo(self, slot_id: Any) -> dict[str, Any]:
        slot = next((s for s in self.slots if s["row"] == slot_id and s["status"] in OPEN_STATUSES), None)
        if slot is None:
            return {"ok": False, "error": "that slot is not available, call list_open_slots again"}
        # re-read the row so two parents cannot book the same slot
        fresh_rows = (await sheets.read([f"Slots!A{slot['row']}:D{slot['row']}"]))[0]
        fresh = pad(fresh_rows[0] if fresh_rows else [], 4)
        if str(fresh[2]).strip().lower() not in OPEN_STATUSES:
            return {"ok": False, "error": "that slot was just taken, call list_open_slots again"}
        await sheets.write("Slots", f"C{slot['row']}", [["Booked", "'" + self.phone]])
        slot["status"] = "booked"
        self.lead["Demo Slot"] = f"{slot['date']} {slot['time']}"
        self.lead["Status"] = "Demo Booked"
        return {"ok": True, "booked": self.lead["Demo Slot"]}


# -------------------------
# Claude agent
# -------------------------
TOOLS = [
    {
        "name": "add_note",
        "description": "Record one short line worth remembering for next time: a concern, a preference, a decision, or what the parent said about the demo. Use sparingly and never record anything sensitive.",
        "input_schema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
    },
    {
        "name": "save_lead",
        "description": "Save details the parent has just told you. Call it as soon as you learn any of them. Only pass fields you actually learned.",
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_name": {"type": "string"},
                "child_name": {"type": "string"},
                "grade": {"type": "string", "description": "Class/grade the child studies in, e.g. 8"},
                "board": {"type": "string", "description": "CBSE, ICSE, state board name, IB, etc."},
                "challenges": {"type": "string", "description": "What the child struggles with in maths, in the parent's words"},
            },
        },
    },
    {"name": "list_open_slots", "description": "Get the currently open demo class slots.", "input_schema": {"type": "object", "properties": {}}},
    {
        "name": "book_demo",
        "description": "Book the demo class in an open slot the parent chose. slot_id must come from list_open_slots.",
        "input_schema": {"type": "object", "properties": {"slot_id": {"type": "integer"}}, "required": ["slot_id"]},
    },
    {
        "name": "flag_for_team",
        "description": "Mark this chat for a human to follow up: use when the answer is not in the FAQ, the parent is upset, asks for something outside your scope, or no slots are open.",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
]


def demo_hint(demo_slot: str) -> str:
    match = re.search(r"(\d{1,2}) ([A-Za-z]{3}) (\d{4})", demo_slot or "")
    if not match:
        return ""
    try:
        demo_date = datetime.strptime(" ".join(match.groups()), "%d %b %Y").date()
    except ValueError:
        return ""
    today = datetime.now(UAE).date()
    if demo_date < today:
        return "Note: this parent's demo date has already passed."
    if demo_date == today:
        return "Note: this parent's demo is today."
    return "Note: this parent's demo is still upcoming."


def build_system(sess: Session) -> str:
    now = datetime.now(UAE).strftime("%A, %d %B %Y, %I:%M %p UAE time")
    known = {c: sess.lead[c] for c in ("Parent Name", "Child Name", "Grade", "Board", "Challenges", "Status", "Demo Slot", "Notes", "Last Message", "Updated At")}
    parts = [
        "You are the WhatsApp assistant for Maths Vertex, a maths tutoring service. You chat with parents.",
        "Current date and time: " + now + ".",
        "",
        "STYLE",
        "- Warm, short WhatsApp messages (under 60 words). Plain text, no markdown headings or tables.",
        "- Reply in the language the parent writes in: English, Hindi, or Hinglish (Roman script if they use Roman script).",
        "- Ask only ONE question per message.",
        "- Greeting: when a parent first says hello, begin your reply with just 'Hi' (no name, no 'Hi there', no 'Welcome to...'), then go straight to helping.",
        "- Never address the parent by name, including the name shown on their WhatsApp profile. Use 'you' instead.",
        "",
        "FACTS",
        "- Answer questions about fees, syllabus, batches, timings ONLY from the FAQ below. Never invent prices, timings or promises.",
        "- If the FAQ does not cover the question, say the team will confirm shortly and call flag_for_team.",
        "",
        "QUALIFICATION (do this before booking a demo)",
        "- Learn, in this order, skipping anything already known: the child's Grade, the Board (CBSE, ICSE, state board, etc.), and what the child struggles with in maths.",
        "- Call save_lead as soon as you learn each detail.",
        "- Also try to learn the child's name and the parent's name naturally.",
        "- The grades Maths Vertex teaches are stated in the FAQ below. If the child's grade is outside that range, say so kindly and call flag_for_team instead of booking a demo. If the FAQ does not state the grades, do not guess: call flag_for_team.",
        "- Class and demo times are in UAE time. Always say 'UAE time' when you mention a time, and never offer a slot that is already in the past.",
        "",
        "DEMO BOOKING",
        "- Once Grade, Board and challenges are known, offer a free demo class and ask what time suits them.",
        "- Call list_open_slots, offer up to 3 slots that fit, and when the parent picks one call book_demo with its slot_id, then confirm the date and time.",
        "- If no slots are open, say the team will share timings and call flag_for_team.",
        "- Never claim a demo is booked unless book_demo returned ok.",
        "",
        "RETURNING PARENTS (the sheet and the conversation so far may already know them)",
        "- Read KNOWN ABOUT THIS PARENT below and the earlier messages in this conversation before replying. If Status says a demo was done, or the demo date has passed, this parent is coming back after their demo: do not start over.",
        "- Never re-ask Grade, Board or challenges you already have, and do not offer a demo again unless they ask for another one.",
        "- Acknowledge the demo naturally and use specifics from Notes and the conversation (child's name, what was covered, what the team already told them). Never invent what happened in the demo or what the teacher said. If you have no details, simply ask how their child found it.",
        "- Respond to what the parent actually said, like a thoughtful person on the team would. No stock scripts, vary your wording, match their tone and language, keep it short, and do not repeat what was already said earlier in the chat.",
        "- If they want to enrol, pay, or ask something the FAQ does not cover, call flag_for_team. In your reply, acknowledge their specific situation in your own words (for example what they were waiting for and that the team had said they would share it), say a person from the team will get back to them, and do not promise a time.",
        "- Do not end every message with a generic question like 'anything else?'. Ask only when you need something to help them.",
        "- Use add_note to record anything worth remembering for next time (their concern, preference or decision) in one short line. Never claim to remember anything that is not in the sheet or the chat.",
        "",
        "KNOWN ABOUT THIS PARENT (from the sheet; may have been edited by the team)",
        json.dumps(known, ensure_ascii=False),
        demo_hint(sess.lead["Demo Slot"]),
        "",
        "FAQ",
        sess.faq_text or "(FAQ is empty: flag_for_team for any question about fees, batches or syllabus.)",
    ]
    return "\n".join(parts)


def merge_turns(turns: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    for turn in turns:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"] += "\n" + turn["content"]
        else:
            merged.append(dict(turn))
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged


async def run_agent(sess: Session, history: list[dict[str, str]], text: str) -> str:
    client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY
    messages: list[dict[str, Any]] = merge_turns(history + [{"role": "user", "content": text}])
    system = build_system(sess)
    nudged = False
    for _ in range(6):
        response = await client.messages.create(model=MODEL, max_tokens=700, system=system, messages=messages, tools=TOOLS)
        if response.stop_reason != "tool_use":
            text_out = "".join(b.text for b in response.content if b.type == "text").strip()
            if text_out or nudged:
                return text_out
            # The model sometimes ends with no text after saving details: ask once for the parent-facing reply.
            nudged = True
            nudge = {"type": "text", "text": "Now write your WhatsApp reply to the parent."}
            last = messages[-1]
            if isinstance(last["content"], list):
                last["content"].append(nudge)
            else:
                last["content"] = str(last["content"]) + "\n\n" + nudge["text"]
            continue
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = await sess.run_tool(block.name, dict(block.input))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(output, ensure_ascii=False)})
        messages.append({"role": "user", "content": results})
    return FALLBACK_REPLY


# -------------------------
# Message handling
# -------------------------
_locks: dict[str, asyncio.Lock] = {}


async def handle_message(contact: str, from_name: str, text: str | None, msg_id: str, timestamp: str) -> str:
    """Every gate runs before any LLM call. text=None means a non-text message (image, voice note...)."""
    if msg_id and not await store.set(f"seen:{msg_id}", "1", nx=True, ex=86400):
        return "duplicate delivery"
    try:
        if timestamp and time.time() - int(timestamp) > STALE_AFTER_SECONDS:
            return "stale message"
    except ValueError:
        pass
    if await store.get(f"human_mode:{contact}"):
        return "human takeover active"

    lock = _locks.setdefault(contact, asyncio.Lock())
    async with lock:  # one reply at a time per parent, so rapid messages stay in order
        opted_out = await store.get(f"optout:{contact}")
        keyword = (text or "").lower().strip(" .!")
        if keyword in {"stop", "unsubscribe", "remove me"}:
            await store.set(f"optout:{contact}", "1")
            await send_whatsapp_reply(contact, "You've been removed. Reply START to chat with Maths Vertex again.")
            return "opted out"
        if keyword == "start" and opted_out:
            await store.delete(f"optout:{contact}")
            opted_out = None
        if opted_out:
            return "opted out, skipped"

        if msg_id:
            await mark_read(msg_id)

        if text is None:
            await send_whatsapp_reply(contact, NON_TEXT_REPLY)
            return "non-text reply sent"
        text = text[:2000]

        raw_history = await store.get(f"hist:{contact}")
        history: list[dict[str, str]] = json.loads(raw_history) if raw_history else []

        reply = FALLBACK_REPLY
        sess: Session | None = None
        try:
            leads, slots, faq = await sheets.read(["Leads!A2:K", "Slots!A2:D", "FAQ!A2:B"])
            sess = Session(contact, from_name, leads, slots, faq)
            if sess.lead["Status"].strip().lower() in HUMAN_STATUSES:
                await sess.persist(text)  # let the team see the new message in the sheet
                return "human handling (status in sheet)"
            reply = await run_agent(sess, history, text) or FALLBACK_REPLY
        except Exception:
            # A parent must never be left on read; the full error is recorded for the operator.
            log.exception("Agent failed, sending a holding reply instead")

        if sess is not None:
            try:
                await sess.persist(text)
            except Exception:
                log.exception("Could not save the lead to Google Sheets")

        await send_whatsapp_reply(contact, reply)
        history = (history + [{"role": "user", "content": text}, {"role": "assistant", "content": reply}])[-HISTORY_TURNS:]
        await store.set(f"hist:{contact}", json.dumps(history, ensure_ascii=False), ex=HISTORY_TTL)
        return "sent"


def message_text(msg: dict[str, Any]) -> str | None:
    """Plain text of an inbound message, or None for media and other non-text types."""
    kind = msg.get("type")
    if kind == "text":
        return str((msg.get("text") or {}).get("body", "")).strip() or None
    if kind == "button":
        return str((msg.get("button") or {}).get("text", "")).strip() or None
    if kind == "interactive":
        inter = msg.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return str(reply.get("title", "")).strip() or None
    return None


async def process_payload(body: dict[str, Any]) -> None:
    our_number_id = cfg("WHATSAPP_PHONE_NUMBER_ID")
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            field = change.get("field")
            try:
                if field == "messages":
                    if (value.get("metadata") or {}).get("phone_number_id") not in (None, our_number_id):
                        continue
                    names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts", [])}
                    for msg in value.get("messages", []):
                        if msg.get("type") in IGNORED_TYPES:
                            continue
                        contact = digits(msg.get("from"))
                        if not contact:
                            continue
                        result = await handle_message(contact, names.get(msg.get("from"), ""), message_text(msg), msg.get("id", ""), str(msg.get("timestamp", "")))
                        log.info("Message %s from %s: %s", msg.get("id", "")[-8:], contact[-4:], result)
                elif field == "smb_message_echoes":
                    # Only arrives if the number also runs in the WhatsApp Business app (coexistence): a person typed a reply there.
                    for echo in value.get("message_echoes", []):
                        if await store.get(f"sent:{echo.get('id', '')}"):
                            continue  # our own API message
                        contact = digits(echo.get("to"))
                        if contact:
                            await store.set(f"human_mode:{contact}", "1", ex=HUMAN_PAUSE_SECONDS)
                            log.info("Human reply detected for %s, bot paused for 2h", contact[-4:])
            except Exception:
                log.exception("Failed to process a webhook change")


# -------------------------
# FastAPI application
# -------------------------
app = FastAPI(title="Maths Vertex WhatsApp Agent", version="2.0.0")


@app.get("/")
async def health() -> dict[str, str]:
    return {"status": "Maths Vertex WhatsApp agent is running", "webhook": "/webhook"}


@app.get("/webhook")
async def verify_webhook(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    """Meta calls this once when you save the webhook URL in the app dashboard."""
    expected = cfg("WHATSAPP_VERIFY_TOKEN")
    if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    secret = cfg("META_APP_SECRET")
    if secret:
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", "")):
            raise HTTPException(status_code=403, detail="bad signature")
    elif cfg("ALLOW_UNSIGNED") != "1":
        raise HTTPException(status_code=503, detail="META_APP_SECRET is not set")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON")
    background.add_task(process_payload, body)  # answer Meta straight away; the reply is sent in the background
    return {"status": "received"}


@app.on_event("startup")
async def check_config() -> None:
    required = ["WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN", "META_APP_SECRET", "ANTHROPIC_API_KEY", "SHEET_ID"]
    missing = [n for n in required if not cfg(n)]
    if not (cfg("GOOGLE_SERVICE_ACCOUNT_JSON") or cfg("GOOGLE_APPLICATION_CREDENTIALS")):
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")
    if missing:
        log.error("Missing environment variables: %s", ", ".join(missing))
    if not cfg("REDIS_URL"):
        log.warning("REDIS_URL not set: conversation memory is kept in process memory and is lost on restart")
