#!/usr/bin/env python3
"""Offline tests for find_leads.py (no Twitch credentials needed)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import find_leads as fl


class TestLeadFinder(unittest.TestCase):
    def test_parse_bio_extracts_public_contacts(self):
        bio = ("GTA RP every night! Business: clips4me@gmail.com | youtube.com/@CoolGuyTV "
               "https://x.com/coolguy tiktok.com/@coolguy.gg discord.gg/abc123")
        info = fl.parse_bio(bio)
        self.assertEqual(info["public_email"], "clips4me@gmail.com")
        self.assertEqual(info["youtube"], "https://youtube.com/@CoolGuyTV")
        self.assertEqual(info["x_twitter"], "https://x.com/coolguy")
        self.assertEqual(info["tiktok"], "https://tiktok.com/@coolguy.gg")
        self.assertEqual(info["discord"], "https://discord.gg/abc123")
        self.assertEqual(fl.parse_bio("just vibes")["public_email"], "")

    def test_opt_out_detection(self):
        for bio in ("No business inquiries please", "DON'T DM me for promos", "no solicitation", "Not interested in editors"):
            self.assertTrue(fl.is_opted_out(bio), bio)
        for bio in ("Business: me@mail.com", "Streaming GTA 6 daily", ""):
            self.assertFalse(fl.is_opted_out(bio), bio)

    def test_scoring_prefers_small_consistent_affiliates(self):
        sweet = fl.score_lead({"viewers_max": 180, "broadcaster_type": "affiliate", "days_seen": 4,
                               "public_email": "a@b.co", "youtube": "y", "x_twitter": "x"})
        huge = fl.score_lead({"viewers_max": 1900, "broadcaster_type": "partner", "days_seen": 1})
        self.assertGreater(sweet, huge)
        self.assertLessEqual(sweet, 100)

    def test_merge_save_roundtrip_preserves_manual_columns(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(fl, "LEADS_CSV", Path(td) / "leads.csv"):
                user = {"id": "1", "login": "CoolGuy", "display_name": "CoolGuy", "broadcaster_type": "affiliate",
                        "created_at": "2022-01-01T00:00:00Z", "description": "biz: cg@mail.com youtube.com/@cg"}
                stream = {"user_id": "1", "viewer_count": 140, "game_name": "Grand Theft Auto VI", "title": "Vice City chaos"}

                leads = {}
                self.assertTrue(fl.merge_stream(leads, stream, user, "2026-11-19"))
                self.assertFalse(fl.merge_stream(leads, {**stream, "viewer_count": 90}, user, "2026-11-19"))
                self.assertEqual(leads["coolguy"]["days_seen"], 1)          # same day doesn't double count
                self.assertEqual(leads["coolguy"]["viewers_max"], 140)
                leads["coolguy"]["status"] = "DM sent"
                leads["coolguy"]["notes"] = "replied on X"
                fl.save_leads(leads)

                reloaded = fl.load_leads()
                fl.merge_stream(reloaded, {**stream, "viewer_count": 220}, user, "2026-11-20")
                row = reloaded["coolguy"]
                self.assertEqual(int(row["days_seen"]), 2)
                self.assertEqual(row["viewers_max"], 220)
                self.assertEqual(row["status"], "DM sent")
                self.assertEqual(row["notes"], "replied on X")
                self.assertEqual(row["public_email"], "cg@mail.com")
                self.assertEqual(row["twitch_url"], "https://twitch.tv/coolguy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
