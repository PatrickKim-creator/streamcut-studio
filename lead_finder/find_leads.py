#!/usr/bin/env python3
"""
Streamer Lead Finder (official Twitch Helix API)
================================================
Finds small/mid live streamers in chosen game categories who are good prospects
for a done-for-you Shorts clipping service, and saves them to leads.csv with
only the contact info they PUBLICLY list in their own Twitch bio.

Usage:
  python find_leads.py --setup                 # save your Twitch app Client ID + Secret once
  python find_leads.py                         # scan default categories, update leads.csv
  python find_leads.py --game "Grand Theft Auto VI" --min 30 --max 1500
  python find_leads.py --top 25                # print best uncontacted leads

Run it 1-2x a day for a week: streamers seen live on several different days are
consistent streamers (the best clients), and their score goes up automatically.

Rules baked in:
  - Official Twitch API only (no scraping, no CAPTCHA-gated data).
  - Only plain emails the streamer published in their own bio are recorded.
  - Bios saying "no business / no DMs / no solicitation" are skipped.
  - Anyone in do_not_contact.txt is never added again.
"""

import argparse
import csv
import getpass
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
LEADS_CSV = HERE / "leads.csv"
DNC_FILE = HERE / "do_not_contact.txt"

HELIX = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"

DEFAULT_GAMES = ["Grand Theft Auto VI", "Grand Theft Auto V"]

FIELDS = [
    "score", "login", "display_name", "twitch_url", "game",
    "viewers_last", "viewers_max", "days_seen", "first_seen", "last_seen",
    "broadcaster_type", "account_created", "public_email",
    "youtube", "tiktok", "x_twitter", "instagram", "discord",
    "bio", "last_title", "status", "notes",
]
# Columns you edit by hand; never overwritten by re-runs
MANUAL_FIELDS = ("status", "notes")

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SOCIAL_PATTERNS = {
    "youtube": re.compile(r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:@|c/|channel/|user/)[\w.\-]+|youtu\.be/[\w\-]+)", re.I),
    "tiktok": re.compile(r"(?:https?://)?(?:www\.)?tiktok\.com/@[\w.\-]+", re.I),
    "x_twitter": re.compile(r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/[\w]+", re.I),
    "instagram": re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/[\w.\-]+", re.I),
    "discord": re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord\.com/invite)/[\w\-]+", re.I),
}
OPT_OUT_RE = re.compile(
    r"no\s+(?:business|biz|promo(?:tion)?s?|solicit\w*|advertis\w*|dms?|offers|sponsor\w*)"
    r"|do\s*n[o']?t\s+(?:dm|message|contact|email)"
    r"|not\s+(?:looking|interested)\s+(?:for|in)\s+(?:editors?|services|offers)",
    re.I,
)


# --------------------------------------------------------------------- config
def load_env() -> Dict[str, str]:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for k in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def run_setup() -> None:
    print("Create a free app at https://dev.twitch.tv/console/apps")
    print("  Name: anything | OAuth Redirect URL: http://localhost | Category: Analytics Tool | Client Type: Confidential\n")
    cid = input("Twitch Client ID: ").strip()
    secret = getpass.getpass("Twitch Client Secret (hidden): ").strip()
    if not cid or not secret:
        sys.exit("Both values are required.")
    ENV_FILE.write_text(f"TWITCH_CLIENT_ID={cid}\nTWITCH_CLIENT_SECRET={secret}\n", encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)
    print(f"Saved to {ENV_FILE} (readable only by you). Now run: python find_leads.py")


# ---------------------------------------------------------------- twitch api
class Helix:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        r = requests.post(TOKEN_URL, params={
            "client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials",
        }, timeout=15)
        if r.status_code != 200:
            sys.exit(f"Twitch auth failed ({r.status_code}): {r.text[:200]}\nRe-run with --setup and check your Client ID/Secret.")
        self.token = r.json()["access_token"]

    def get(self, path: str, params) -> Dict:
        for attempt in range(4):
            r = requests.get(f"{HELIX}/{path}", params=params, timeout=15, headers={
                "Client-ID": self.client_id, "Authorization": f"Bearer {self.token}",
            })
            if r.status_code == 429:
                reset = float(r.headers.get("Ratelimit-Reset", time.time() + 2))
                time.sleep(max(1.0, reset - time.time()))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Rate-limited repeatedly on {path}")

    def game_id(self, name: str) -> Optional[str]:
        data = self.get("games", {"name": name}).get("data") or []
        return data[0]["id"] if data else None

    def live_streams(self, game_id: str, language: str, min_v: int, max_v: int, max_pages: int = 40) -> List[Dict]:
        """Streams come back sorted by viewers (desc); stop once we drop below min_v."""
        out, cursor = [], None
        for _ in range(max_pages):
            params = {"game_id": game_id, "first": 100}
            if language:
                params["language"] = language
            if cursor:
                params["after"] = cursor
            page = self.get("streams", params)
            rows = page.get("data") or []
            for s in rows:
                if min_v <= int(s.get("viewer_count", 0)) <= max_v:
                    out.append(s)
            cursor = (page.get("pagination") or {}).get("cursor")
            if not rows or not cursor or int(rows[-1].get("viewer_count", 0)) < min_v:
                break
        return out

    def users(self, ids: Iterable[str]) -> Dict[str, Dict]:
        ids = list(ids)
        found = {}
        for i in range(0, len(ids), 100):
            params = [("id", x) for x in ids[i:i + 100]]
            for u in self.get("users", params).get("data") or []:
                found[u["id"]] = u
        return found


# ------------------------------------------------------------- lead analysis
def parse_bio(bio: str) -> Dict[str, str]:
    bio = bio or ""
    info = {k: "" for k in ("public_email", *SOCIAL_PATTERNS)}
    m = EMAIL_RE.search(bio)
    if m:
        info["public_email"] = m.group(0)
    for key, pat in SOCIAL_PATTERNS.items():
        hit = pat.search(bio)
        if hit:
            url = hit.group(0)
            info[key] = url if url.lower().startswith("http") else f"https://{url}"
    return info


def is_opted_out(bio: str) -> bool:
    return bool(OPT_OUT_RE.search(bio or ""))


def score_lead(row: Dict) -> int:
    """0-100: how likely this streamer is to pay for a Shorts clipping service."""
    v = int(row.get("viewers_max") or row.get("viewers_last") or 0)
    s = 0
    if 50 <= v <= 600:
        s += 35          # sweet spot: real audience, no full-time editor yet
    elif 30 <= v < 50 or 600 < v <= 1200:
        s += 22
    elif v > 1200:
        s += 12          # likely already has editors
    bt = row.get("broadcaster_type") or ""
    s += {"affiliate": 15, "partner": 8}.get(bt, 4)
    s += min(25, 8 * max(0, int(row.get("days_seen") or 1) - 1))   # consistency across days
    if row.get("public_email"):
        s += 10
    if row.get("youtube") or row.get("tiktok"):
        s += 10          # already cares about short-form
    if row.get("x_twitter") or row.get("instagram") or row.get("discord"):
        s += 5
    return min(100, s)


# --------------------------------------------------------------- csv storage
def load_leads() -> Dict[str, Dict]:
    if not LEADS_CSV.exists():
        return {}
    with LEADS_CSV.open(newline="", encoding="utf-8") as fh:
        return {r["login"]: r for r in csv.DictReader(fh) if r.get("login")}


def save_leads(leads: Dict[str, Dict]) -> None:
    rows = sorted(leads.values(), key=lambda r: (-int(r.get("score") or 0), r["login"]))
    tmp = LEADS_CSV.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(LEADS_CSV)


def load_dnc() -> set:
    if not DNC_FILE.exists():
        DNC_FILE.write_text("# One Twitch login per line. These streamers are never added or contacted.\n", encoding="utf-8")
        return set()
    return {ln.strip().lower() for ln in DNC_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def merge_stream(leads: Dict[str, Dict], stream: Dict, user: Dict, today: str) -> bool:
    """Insert/update one lead. Returns True if it's new."""
    login = user["login"].lower()
    bio = " ".join((user.get("description") or "").split())
    contacts = parse_bio(bio)
    viewers = int(stream.get("viewer_count", 0))
    old = leads.get(login)
    is_new = old is None
    row = dict(old) if old else {k: "" for k in FIELDS}

    days_seen = int(row.get("days_seen") or 0)
    if row.get("last_seen") != today:
        days_seen += 1
    row.update({
        "login": login,
        "display_name": user.get("display_name") or login,
        "twitch_url": f"https://twitch.tv/{login}",
        "game": stream.get("game_name", ""),
        "viewers_last": viewers,
        "viewers_max": max(viewers, int(row.get("viewers_max") or 0)),
        "days_seen": days_seen,
        "first_seen": row.get("first_seen") or today,
        "last_seen": today,
        "broadcaster_type": user.get("broadcaster_type") or "none",
        "account_created": (user.get("created_at") or "")[:10],
        "bio": bio[:300],
        "last_title": " ".join((stream.get("title") or "").split())[:140],
        **{k: v or row.get(k, "") for k, v in contacts.items()},
    })
    for k in MANUAL_FIELDS:
        row[k] = (old or {}).get(k, "")
    row["score"] = score_lead(row)
    leads[login] = row
    return is_new


def print_top(leads: Dict[str, Dict], n: int) -> None:
    rows = [r for r in leads.values() if not (r.get("status") or "").strip()]
    rows.sort(key=lambda r: -int(r.get("score") or 0))
    print(f"\nTop {min(n, len(rows))} uncontacted leads (of {len(leads)} total):")
    print(f"{'score':>5}  {'viewers':>7}  {'days':>4}  {'login':<22} contact")
    for r in rows[:n]:
        contact = r.get("public_email") or r.get("x_twitter") or r.get("discord") or r.get("instagram") or "Twitch whisper / chat"
        print(f"{r['score']:>5}  {r['viewers_max']:>7}  {r['days_seen']:>4}  {r['login']:<22} {contact}")


# ---------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="Find streamer leads for a Shorts clipping service (official Twitch API).")
    ap.add_argument("--setup", action="store_true", help="Save your Twitch Client ID/Secret")
    ap.add_argument("--game", action="append", help="Twitch category name (repeatable)")
    ap.add_argument("--min", type=int, default=30, help="Min live viewers (default 30)")
    ap.add_argument("--max", type=int, default=2000, help="Max live viewers (default 2000)")
    ap.add_argument("--language", default="en", help="Stream language code, '' for any (default en)")
    ap.add_argument("--top", type=int, default=20, help="How many top leads to print")
    ap.add_argument("--show-only", action="store_true", help="Just print top leads from leads.csv")
    args = ap.parse_args()

    if args.setup:
        return run_setup()

    leads = load_leads()
    if args.show_only:
        return print_top(leads, args.top)

    env = load_env()
    if not env.get("TWITCH_CLIENT_ID") or not env.get("TWITCH_CLIENT_SECRET"):
        sys.exit("No Twitch credentials yet. Run:  python find_leads.py --setup")

    api = Helix(env["TWITCH_CLIENT_ID"], env["TWITCH_CLIENT_SECRET"])
    dnc = load_dnc()
    today = date.today().isoformat()
    new_count = skipped_optout = 0

    for game in args.game or DEFAULT_GAMES:
        gid = api.game_id(game)
        if not gid:
            print(f"[skip] Category not found on Twitch: {game!r}")
            continue
        streams = api.live_streams(gid, args.language, args.min, args.max)
        users = api.users({s["user_id"] for s in streams})
        print(f"[{game}] {len(streams)} live streams in {args.min}-{args.max} viewers")
        for s in streams:
            u = users.get(s["user_id"])
            if not u or u["login"].lower() in dnc:
                continue
            if is_opted_out(u.get("description") or ""):
                skipped_optout += 1
                continue
            new_count += merge_stream(leads, s, u, today)

    save_leads(leads)
    print(f"\nSaved {len(leads)} leads ({new_count} new, {skipped_optout} skipped for 'no business' bios) -> {LEADS_CSV}")
    print_top(leads, args.top)


if __name__ == "__main__":
    main()
