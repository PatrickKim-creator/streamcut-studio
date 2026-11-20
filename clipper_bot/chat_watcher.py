#!/usr/bin/env python3
"""
Zero-Key Live Twitch Chat Reaction Detector
===========================================
Connects anonymously to Twitch IRC over WebSocket (wss://irc-ws.chat.twitch.tv:443)
and measures *genuine audience reactions* rather than raw message volume:

  * Ignores chat bots (Nightbot, StreamElements, *bot accounts), !commands, links,
    and /me ACTION announcements.
  * Caps each viewer at 2 messages per window so one spammer can't fake a spike.
  * Detects copypasta / giveaway-keyword floods (e.g. everyone typing "Vice City"
    to enter a giveaway) and discounts them.
  * Suppresses chat signal right after gift-sub bombs and raids (W-spam floods).
  * Scores unique reacting viewers (KEKW, LMAO, NO WAY, 💀, ???) against a robust
    median/MAD baseline of the same channel, so 500-viewer and 100k-viewer
    streams are both judged relative to their own normal chat speed.
"""

import asyncio
import random
import re
import statistics
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

import websockets

TWITCH_IRC_WS = "wss://irc-ws.chat.twitch.tv:443"

# Real audience *reaction* phrases (deliberately excludes game names, "W" spam, etc.)
HYPE_PATTERNS = [
    r"\bclip\b", r"\bclip\s*(that|it)\b", r"\blmao+\b", r"\blmfao+\b", r"\bomg+\b",
    r"\bno\s*way+\b", r"\bwtf+\b", r"\bwhat+\b", r"\bcrazy\b", r"\binsane\b",
    r"\bkekw\b", r"\bomegalul\b", r"\blul\b", r"\bicant\b", r"\bpog+(ers)?\b",
    r"\bdead\b", r"\bain'?t\s*no\s*way\b", r"\bhaha+\b", r"\bnah+\b", r"\bbro+\b",
    r"\?\?+", r"!!+", r"[💀😭😂🤣😱🤯🔥]",
]
_HYPE_RE = re.compile("|".join(HYPE_PATTERNS), re.IGNORECASE)

KNOWN_BOTS = {
    "nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot",
    "soundalerts", "sery_bot", "botisimo", "pretzelrocks", "commanderroot",
    "kofistreambot", "blerp", "own3d", "tangiabot", "creatisbot", "streamstickers",
    "lumiastream", "frostytoolsdotcom", "buttsbot", "playwithviewersbot", "pokemoncommunitygame",
}
_URL_RE = re.compile(r"https?://|www\.|\.(com|tv|gg|ly|net|org)/", re.IGNORECASE)
_INVISIBLE_RE = re.compile("[\u034f\u200b-\u200f\u2060-\u2064\ufeff\U000e0000-\U000e007f]")
_REPEAT_RE = re.compile(r"(.)\1{2,}")
_NON_WORD_RE = re.compile(r"[^\w\s]")

_PRIVMSG_RE = re.compile(r"^:([^!\s]+)![^\s]+\s+PRIVMSG\s+#[^\s]+\s+:(.*)$")
_USERNOTICE_RE = re.compile(r"^:[^\s]+\s+USERNOTICE\s+#[^\s]+")

# Only suppress genuine raids or large gift-sub bombs (never normal sub trickles on 50k+ streams!)
SUPPRESS_EVENTS = {"submysterygift": 15.0, "raid": 18.0}
GIFT_NOTICES = {"subgift"}


def normalize_message(text: str) -> str:
    """Lowercase, strip invisible anti-duplicate characters, collapse letter spam."""
    t = _INVISIBLE_RE.sub("", text).lower()
    t = _NON_WORD_RE.sub(" ", t)
    t = _REPEAT_RE.sub(r"\1\1", t)
    return " ".join(t.split())


def is_bot_account(user: Optional[str]) -> bool:
    if not user:
        return False
    u = user.lower()
    return u in KNOWN_BOTS or u.endswith("bot")


@dataclass
class ChatEvent:
    ts: float
    user: str
    norm: str
    text: str
    is_hype: bool


@dataclass
class ChatSnapshot:
    connected: bool
    current_mps: float
    baseline_mps: float
    hype_ratio: float
    chat_z: float
    total_messages: int
    top_messages: List[str]
    unique_users: int = 0
    reaction_users: int = 0
    spam_share: float = 0.0
    suppressed: bool = False
    peak_offset_sec: float = -1.0
    filtered_messages: int = 0


@dataclass
class _WindowMetrics:
    n: int = 0
    effective_msgs: int = 0
    unique_users: int = 0
    reaction_users: int = 0
    spam_share: float = 0.0
    rate: float = 0.0
    raw_mps: float = 0.0
    hype_ratio: float = 0.0
    top_messages: List[str] = field(default_factory=list)
    peak_offset_sec: float = -1.0


def _robust_z(value: float, history: List[float]) -> float:
    med = statistics.median(history)
    mad = statistics.median([abs(x - med) for x in history]) * 1.4826
    std = max(0.3, 0.2 * med, mad)
    return (value - med) / std


class ChatSpikeTracker:
    """Thread-safe tracker for genuine chat reaction spikes."""

    def __init__(self, window_sec: float = 6.0, history_buckets: int = 24, retention_sec: float = 600.0):
        self.window_sec = window_sec
        self.retention_sec = retention_sec
        self._lock = threading.Lock()
        self._events: Deque[ChatEvent] = deque()
        self._baseline_samples: Deque[float] = deque(maxlen=history_buckets)
        self._segment_rates: Deque[float] = deque(maxlen=36)
        self._last_sample_ts: Optional[float] = None
        self._suppress_from: float = 0.0
        self._suppress_until: float = 0.0
        self._sub_notice_ts: Deque[float] = deque()
        self._anon_counter = 0
        self._total_messages = 0
        self.filtered_messages = 0
        self.connected: bool = False

    # ------------------------------------------------------------------ ingest
    def record_message(self, text: str, ts: Optional[float] = None, user: Optional[str] = None) -> bool:
        """Record a chat message. Returns False if it was filtered as bot/command/link spam."""
        now = ts if ts is not None else time.time()
        raw = text.strip()
        if raw.startswith("\x01ACTION"):
            raw = raw[len("\x01ACTION"):].strip("\x01 ").strip()
        with self._lock:
            if (
                not raw
                or raw.startswith("!")
                or is_bot_account(user)
                or _URL_RE.search(raw)
                or len(raw) > 220
            ):
                self.filtered_messages += 1
                return False
            if not user:
                self._anon_counter += 1
                user = f"anon{self._anon_counter}"
            ev = ChatEvent(
                ts=now,
                user=user.lower(),
                norm=normalize_message(raw) or raw.lower(),
                text=raw[:120],
                is_hype=bool(_HYPE_RE.search(raw)),
            )
            self._total_messages += 1
            self._events.append(ev)
            self._prune_locked(now)

            if self._last_sample_ts is None:
                self._last_sample_ts = now
            elif now - self._last_sample_ts >= self.window_sec:
                m = self._metrics_locked(now - self.window_sec, now)
                if not self._is_suppressed_locked(now - self.window_sec, now):
                    self._baseline_samples.append(m.rate)
                self._last_sample_ts = now
        return True

    def record_event(self, kind: str, ts: Optional[float] = None, mass_count: int = 10) -> None:
        """Record Twitch USERNOTICE: suppress only raids & large 10+ gift-sub bombs (not normal subs)."""
        now = ts if ts is not None else time.time()
        with self._lock:
            if kind == "raid":
                self._suppress_locked(now, now + SUPPRESS_EVENTS["raid"])
            elif kind == "submysterygift" and mass_count >= 10:
                self._suppress_locked(now, now + SUPPRESS_EVENTS["submysterygift"])
            elif kind in GIFT_NOTICES:
                self._sub_notice_ts.append(now)
                while self._sub_notice_ts and self._sub_notice_ts[0] < now - 6.0:
                    self._sub_notice_ts.popleft()
                if len(self._sub_notice_ts) >= 12:
                    self._suppress_locked(now, now + 14.0)

    def _suppress_locked(self, start: float, end: float) -> None:
        if self._suppress_until >= start:
            self._suppress_until = max(self._suppress_until, end)
        else:
            self._suppress_from, self._suppress_until = start, end

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self.retention_sec
        while self._events and self._events[0].ts < cutoff:
            self._events.popleft()

    def _is_suppressed_locked(self, start: float, end: float) -> bool:
        return self._suppress_until > start and self._suppress_from < end

    # ----------------------------------------------------------------- metrics
    def _metrics_locked(self, start: float, end: float) -> _WindowMetrics:
        evs = [e for e in self._events if start <= e.ts <= end]
        m = _WindowMetrics(n=len(evs))
        dur = max(1.0, end - start)
        if not evs:
            return m

        per_user = Counter(e.user for e in evs)
        m.effective_msgs = sum(min(2, c) for c in per_user.values())
        m.unique_users = len(per_user)
        reactors = {e.user for e in evs if e.is_hype}
        m.reaction_users = len(reactors)
        m.hype_ratio = m.reaction_users / max(1, m.unique_users)
        m.raw_mps = len(evs) / dur

        # Copypasta / giveaway-keyword floods: one identical non-reaction message dominating
        non_hype = Counter(e.norm for e in evs if not e.is_hype)
        if non_hype and len(evs) >= 6:
            m.spam_share = non_hype.most_common(1)[0][1] / len(evs)

        rate = (m.effective_msgs + 1.5 * m.reaction_users) / dur
        if m.spam_share >= 0.30:
            rate *= max(0.15, 1.0 - m.spam_share * 1.5)
        m.rate = rate

        # Top distinct reaction lines (ranked by how many different viewers said them)
        groups: Dict[str, List[ChatEvent]] = {}
        for e in evs:
            if e.is_hype:
                groups.setdefault(e.norm, []).append(e)
        ranked = sorted(groups.values(), key=lambda g: (len({x.user for x in g}), g[-1].ts), reverse=True)
        m.top_messages = [g[0].text for g in ranked[:6]]
        if not m.top_messages:
            seen = set()
            for e in reversed(evs):
                if e.norm not in seen:
                    seen.add(e.norm)
                    m.top_messages.append(e.text)
                if len(m.top_messages) >= 4:
                    break

        # Densest 2-second burst of reactions -> where the moment actually landed
        src = [e for e in evs if e.is_hype] or evs
        n_bins = max(1, int(dur))
        bins = [0] * n_bins
        for e in src:
            bins[min(n_bins - 1, max(0, int(e.ts - start)))] += 1
        best_i, best_v = 0, -1
        for i in range(n_bins):
            v = bins[i] + (bins[i + 1] if i + 1 < n_bins else 0)
            if v > best_v:
                best_i, best_v = i, v
        m.peak_offset_sec = float(best_i + 1.0)
        return m

    def _build_snapshot(self, m: _WindowMetrics, z: float, baseline: float, suppressed: bool) -> ChatSnapshot:
        if m.effective_msgs < 4 or m.unique_users < 3:
            z = min(z, 0.8)  # tiny chats can't produce a viral spike
        if suppressed:
            z = min(z, 1.0)
        return ChatSnapshot(
            connected=self.connected,
            current_mps=round(m.raw_mps, 2),
            baseline_mps=round(baseline, 2),
            hype_ratio=round(m.hype_ratio, 3),
            chat_z=round(max(-2.0, min(10.0, z)), 2),
            total_messages=self._total_messages,
            top_messages=m.top_messages,
            unique_users=m.unique_users,
            reaction_users=m.reaction_users,
            spam_share=round(m.spam_share, 2),
            suppressed=suppressed,
            peak_offset_sec=m.peak_offset_sec,
            filtered_messages=self.filtered_messages,
        )

    def snapshot(self, ts: Optional[float] = None) -> ChatSnapshot:
        """Live view of the most recent window (used by dashboard telemetry & simulations)."""
        now = ts if ts is not None else time.time()
        with self._lock:
            m = self._metrics_locked(now - self.window_sec, now)
            suppressed = self._is_suppressed_locked(now - self.window_sec, now)
            hist = list(self._baseline_samples)
            if len(hist) >= 3:
                z = _robust_z(m.rate, hist)
                base = statistics.median(hist)
            else:
                z, base = 0.0, m.rate  # warm-up: learn this channel's normal chat speed first
            return self._build_snapshot(m, z, base, suppressed)

    def window_snapshot(self, start: float, end: float) -> ChatSnapshot:
        """
        Score chat inside an exact wall-clock window (aligned to one video segment
        plus the audience reaction delay) against previous segment windows.
        """
        with self._lock:
            m = self._metrics_locked(start, end)
            suppressed = self._is_suppressed_locked(start, end)
            hist = list(self._segment_rates)
            if len(hist) >= 4:
                z = _robust_z(m.rate, hist)
                base = statistics.median(hist)
            else:
                z, base = 0.0, m.rate
            if not suppressed:
                self._segment_rates.append(m.rate)
            return self._build_snapshot(m, z, base, suppressed)


def _parse_tags(tag_str: str) -> Dict[str, str]:
    out = {}
    for part in tag_str.lstrip("@").split(";"):
        k, _, v = part.partition("=")
        out[k] = v
    return out


class TwitchChatWatcher:
    """Background WebSocket daemon that feeds live Twitch IRC chat into a ChatSpikeTracker."""

    def __init__(self, channel: str):
        self.channel = channel.lower().lstrip("#")
        self.tracker = ChatSpikeTracker()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name=f"irc-{self.channel}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.5)

    def handle_line(self, line: str) -> None:
        tags: Dict[str, str] = {}
        if line.startswith("@"):
            tag_str, _, line = line.partition(" ")
            tags = _parse_tags(tag_str)
        m = _PRIVMSG_RE.match(line)
        if m:
            self.tracker.record_message(m.group(2), user=m.group(1))
            return
        if _USERNOTICE_RE.match(line):
            try:
                cnt = int(tags.get("msg-param-mass-gift-count") or 0)
            except ValueError:
                cnt = 0
            self.tracker.record_event(tags.get("msg-id", ""), mass_count=cnt)

    def _run_loop(self) -> None:
        asyncio.run(self._connect_forever())

    async def _connect_forever(self) -> None:
        backoff = 2.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(TWITCH_IRC_WS, ping_interval=30, ping_timeout=15) as ws:
                    nick = f"justinfan{random.randint(10000, 99999)}"
                    await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
                    await ws.send("PASS SCHMOOPIIE")
                    await ws.send(f"NICK {nick}")
                    await ws.send(f"JOIN #{self.channel}")
                    self.tracker.connected = True
                    backoff = 2.0

                    while not self._stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        for line in str(raw).splitlines():
                            if line.startswith("PING"):
                                await ws.send(line.replace("PING", "PONG", 1))
                            else:
                                self.handle_line(line)
            except Exception:
                self.tracker.connected = False
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(min(30.0, backoff))
                backoff = min(30.0, backoff * 1.6)
        self.tracker.connected = False
