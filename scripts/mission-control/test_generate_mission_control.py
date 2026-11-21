#!/usr/bin/env python3
"""Focused stdlib unittests for the Mission Control generator.

Synthetic data only: nothing here touches the network, reads credentials
or depends on GitHub being up. Each test pins one honesty rule of the
data layer: the 365-day window, the flight-log collapse and sanitization
rules, streak handling, pagination, and public-only labeling.

Run from the repository root:

    python3 -m unittest discover -s scripts/mission-control -t scripts/mission-control -v

or directly:

    python3 scripts/mission-control/test_generate_mission_control.py -v
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_mission_control as gmc

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
END_DATE = "2026-11-21"


def mkday(date: str, count: int, weekday: int) -> dict:
    return {"date": date, "contributionCount": count, "weekday": weekday}


def days_from_counts(counts, end_date=END_DATE):
    """counts is ordered oldest → newest; dates end at end_date."""
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    n = len(counts)
    out = []
    for i, c in enumerate(counts):
        d = end - timedelta(days=n - 1 - i)
        out.append(mkday(d.isoformat(), c, d.isoweekday() % 7))
    return out


def push(ts, repo, size=1, branch="master", msg="do work"):
    return {
        "type": "PushEvent",
        "created_at": ts,
        "repo": {"name": repo},
        "payload": {
            "size": size,
            "ref": f"refs/heads/{branch}",
            "commits": [{"message": msg}],
        },
    }


def pr_event(ts, repo, action, number, title="A title", merged=False):
    return {
        "type": "PullRequestEvent",
        "created_at": ts,
        "repo": {"name": repo},
        "payload": {
            "action": action,
            "merged": merged,
            "pull_request": {"number": number, "title": title, "merged": merged},
        },
    }


def issue_event(ts, repo, action, number, title="An issue"):
    return {
        "type": "IssuesEvent",
        "created_at": ts,
        "repo": {"name": repo},
        "payload": {"action": action, "issue": {"number": number, "title": title}},
    }


class TestWindow(unittest.TestCase):
    def test_exactly_365_dates_inclusive_of_today(self):
        dates = gmc.window_dates(NOW)
        self.assertEqual(len(dates), 365)
        self.assertEqual(dates[-1], "2026-11-21")  # today, inclusive
        self.assertEqual(dates[0], "2025-11-22")  # 364 days back
        self.assertEqual(len(set(dates)), 365)

    def test_dates_are_consecutive(self):
        dates = gmc.window_dates(NOW)
        parsed = [datetime.strptime(d, "%Y-%m-%d").date() for d in dates]
        self.assertEqual(parsed[-1] - parsed[0], timedelta(days=364))
        for a, b in zip(parsed, parsed[1:]):
            self.assertEqual(b - a, timedelta(days=1))

    def test_window_includes_leap_day(self):
        dates = gmc.window_dates(datetime(2024, 3, 1, tzinfo=timezone.utc))
        self.assertIn("2024-02-29", dates)
        self.assertEqual(len(dates), 365)

    def test_draft_bug_is_fixed_366_day_window_rejected(self):
        # The draft asked for 365 days back plus today: 366 dates. The
        # fixed window must be one day shorter.
        dates = gmc.window_dates(NOW)
        self.assertEqual(
            dates[0],
            (NOW.date() - timedelta(days=364)).isoformat(),
        )
        self.assertNotEqual(dates[0], (NOW.date() - timedelta(days=365)).isoformat())


class TestNormalizeDays(unittest.TestCase):
    def setUp(self):
        self.window = gmc.window_dates(NOW)

    def test_trims_padding_to_exact_window(self):
        # API-style padding: six extra days before the window.
        older = [(NOW.date() - timedelta(days=365 + k)).isoformat() for k in range(6)]
        padded = [mkday(d, 9, 0) for d in older]
        padded += [mkday(d, i % 5, i % 7) for i, d in enumerate(self.window)]
        out = gmc.normalize_days(padded, self.window)
        self.assertEqual(len(out), 365)
        self.assertEqual(out[0]["date"], self.window[0])
        self.assertEqual(out[-1]["date"], self.window[-1])
        self.assertEqual(out[0]["contributionCount"], 0)

    def test_missing_date_raises_instead_of_inventing_zero(self):
        missing = [mkday(d, 1, 0) for d in self.window if d != self.window[100]]
        with self.assertRaises(RuntimeError) as ctx:
            gmc.normalize_days(missing, self.window)
        self.assertIn(self.window[100], str(ctx.exception))

    def test_counts_are_coerced_to_int(self):
        raw = [mkday(d, "3", "2") for d in self.window]
        out = gmc.normalize_days(raw, self.window)
        self.assertEqual(out[0]["contributionCount"], 3)
        self.assertEqual(out[0]["weekday"], 2)


class TestStreaks(unittest.TestCase):
    def test_active_today_ends_today(self):
        days = days_from_counts([0, 0, 1, 1, 1])
        length, start, end = gmc.current_streak(days)
        self.assertEqual(length, 3)
        self.assertEqual(end, END_DATE)

    def test_zero_today_but_active_yesterday_ends_yesterday(self):
        days = days_from_counts([0, 1, 1, 1, 0])
        length, start, end = gmc.current_streak(days)
        self.assertEqual(length, 3)
        self.assertEqual(end, "2026-11-20")

    def test_two_zero_days_means_no_streak(self):
        days = days_from_counts([1, 1, 0, 0])
        length, start, end = gmc.current_streak(days)
        self.assertEqual(length, 0)
        self.assertEqual(start, "")
        # No crash and no fabricated dates downstream.
        self.assertIn("no active streak", gmc.streak_phrase(length, start, end))

    def test_empty_days(self):
        length, start, end = gmc.current_streak([])
        self.assertEqual((length, start, end), (0, "", ""))

    def test_all_zero_days(self):
        length, start, end = gmc.current_streak(days_from_counts([0, 0, 0]))
        self.assertEqual(length, 0)

    def test_longest_streak_across_gap(self):
        days = days_from_counts([1, 1, 1, 0, 1, 1, 0, 1])
        best, start, end = gmc.longest_streak(days)
        self.assertEqual(best, 3)
        self.assertEqual(end, (NOW.date() - timedelta(days=5)).isoformat())

    def test_longest_streak_all_zero(self):
        best, start, end = gmc.longest_streak(days_from_counts([0, 0]))
        self.assertEqual((best, start, end), (0, "", ""))

    def test_streak_phrase_zero_is_safe(self):
        self.assertEqual(gmc.streak_phrase(0, "", ""), "no active streak")

    def test_streak_phrase_with_dates(self):
        phrase = gmc.streak_phrase(3, "2026-11-19", "2026-11-21")
        self.assertIn("3 days", phrase)
        self.assertIn("19 NOV 2026", phrase)
        self.assertIn("21 NOV 2026", phrase)


class TestWeekColumns(unittest.TestCase):
    @staticmethod
    def build(start, counts):
        sd = datetime.strptime(start, "%Y-%m-%d").date()
        return [mkday((sd + timedelta(days=i)).isoformat(), c,
                      (sd + timedelta(days=i)).isoweekday() % 7)
                for i, c in enumerate(counts)]

    def test_saturday_window_partial_first_column(self):
        # The live window (2025-11-22 → 2026-11-21) starts on a Saturday.
        days = self.build("2025-11-22", [i % 3 for i in range(365)])
        self.assertEqual(days[0]["weekday"], 6)  # sanity: Saturday
        columns = gmc.week_columns(days)
        self.assertEqual(len(columns), 53)
        self.assertEqual([len(c) for c in columns], [1] + [7] * 52)
        # Dates ascend visually Sun → Sat inside every full column.
        for col in columns[1:]:
            self.assertEqual([d["weekday"] for d in col], [0, 1, 2, 3, 4, 5, 6])
            self.assertEqual([d["date"] for d in col], sorted(d["date"] for d in col))
        self.assertEqual(columns[1][0]["date"], "2025-11-23")  # first Sunday
        self.assertEqual(columns[-1][-1]["date"], "2026-11-21")

    def test_midweek_start_partial_first_and_last(self):
        days = self.build("2026-01-07", [1] * 365)  # 2026-01-07 is a Wednesday
        self.assertEqual(days[0]["weekday"], 3)
        columns = gmc.week_columns(days)
        self.assertEqual(len(columns), 53)
        self.assertEqual([len(c) for c in columns], [4] + [7] * 51 + [4])
        self.assertEqual([d["weekday"] for d in columns[0]], [3, 4, 5, 6])
        self.assertEqual([d["weekday"] for d in columns[-1]], [0, 1, 2, 3])
        for col in columns[1:-1]:
            self.assertEqual([d["weekday"] for d in col], [0, 1, 2, 3, 4, 5, 6])

    def test_sunday_start_partial_last_column(self):
        days = self.build("2025-11-23", [1] * 365)  # a Sunday
        self.assertEqual(days[0]["weekday"], 0)
        columns = gmc.week_columns(days)
        self.assertEqual(len(columns), 53)
        self.assertEqual([len(c) for c in columns], [7] * 52 + [1])
        self.assertEqual(columns[-1][0]["weekday"], 0)

    def test_every_day_lands_exactly_once_in_order(self):
        days = self.build("2025-11-22", [i % 3 for i in range(365)])
        flat = [d for col in gmc.week_columns(days) for d in col]
        self.assertEqual(len(flat), 365)
        self.assertEqual([d["date"] for d in flat], [d["date"] for d in days])


class TestJuneQuiet(unittest.TestCase):
    @staticmethod
    def window_with(june_overrides=None, start="2025-11-22", n=365):
        """A synthetic window (start is a Saturday) with June overrides."""
        overrides = june_overrides or {}
        sd = datetime.strptime(start, "%Y-%m-%d").date()
        out = []
        for i in range(n):
            day = sd + timedelta(days=i)
            out.append(mkday(day.isoformat(), overrides.get(day.isoformat(), 1),
                             day.isoweekday() % 7))
        return out

    def test_real_zero_week_in_june_is_quiet(self):
        overrides = {f"2026-06-{day:02d}": 0 for day in range(10, 18)}  # 8 zero days
        evidence = gmc.june_quiet_evidence(self.window_with(overrides))
        self.assertTrue(evidence["quiet"])
        self.assertEqual(evidence["start"], "2026-06-10")
        self.assertEqual(evidence["end"], "2026-06-17")
        self.assertEqual(evidence["zero_days"], 8)

    def test_scattered_zeros_are_not_quiet(self):
        overrides = {f"2026-06-{day:02d}": 0 for day in (2, 3, 4, 20, 21, 22)}
        evidence = gmc.june_quiet_evidence(self.window_with(overrides))
        self.assertFalse(evidence["quiet"])
        self.assertEqual(evidence["zero_days"], 3)

    def test_june_partially_outside_window_is_ignored(self):
        # The window starts mid-June 2026, so June is not fully covered
        # and no quiet claim may be made from it.
        overrides = {f"2026-06-{day:02d}": 0 for day in range(15, 31)}
        days = self.window_with(overrides, start="2026-06-15")
        self.assertFalse(gmc.june_quiet_evidence(days)["quiet"])

    def test_run_spilling_into_july_is_clipped_to_june(self):
        overrides = {}
        for day in range(28, 31):  # 28–30 June
            overrides[f"2026-06-{day:02d}"] = 0
        for day in range(1, 6):  # the run continues into July
            overrides[f"2026-07-{day:02d}"] = 0
        evidence = gmc.june_quiet_evidence(self.window_with(overrides))
        self.assertFalse(evidence["quiet"])  # only 3 June days: not a week
        self.assertEqual(evidence["zero_days"], 3)
        self.assertEqual(evidence["end"], "2026-06-30")

    def test_run_at_end_of_june_counts(self):
        overrides = {f"2026-06-{day:02d}": 0 for day in range(24, 31)}  # 7 days
        evidence = gmc.june_quiet_evidence(self.window_with(overrides))
        self.assertTrue(evidence["quiet"])
        self.assertEqual(evidence["start"], "2026-06-24")
        self.assertEqual(evidence["end"], "2026-06-30")

    def test_active_june_is_not_quiet(self):
        evidence = gmc.june_quiet_evidence(self.window_with())
        self.assertFalse(evidence["quiet"])


class TestFlightLogCollapse(unittest.TestCase):
    def test_pr_opened_vs_merged_never_collapsed(self):
        # Regression: the draft dropped the OPENED row when a MERGED row
        # of the same PR sat next to it. Both events must appear.
        events = [
            pr_event("2026-11-21T10:00:00Z", "crisesarmiento/useroute", "closed", 5, merged=True),
            pr_event("2026-11-21T09:00:00Z", "crisesarmiento/useroute", "opened", 5),
        ]
        rows = gmc.build_flight_log(events)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["verb"] for r in rows}, {"PR MERGED", "PR OPENED"})

    def test_identical_pushes_same_day_collapsed_across_interleaving(self):
        events = [
            push("2026-11-21T10:00:00Z", "crisesarmiento/cstack", size=2, branch="main", msg="a"),
            push("2026-11-21T09:00:00Z", "crisesarmiento/useroute", size=1, msg="b"),
            push("2026-11-21T08:00:00Z", "crisesarmiento/cstack", size=2, branch="main", msg="c"),
        ]
        rows = gmc.build_flight_log(events)
        cstack = [r for r in rows if r["repo"] == "crisesarmiento/cstack"]
        self.assertEqual(len(cstack), 1)
        self.assertEqual(cstack[0]["count"], 2)
        # The other repo is a separate row.
        self.assertEqual(len(rows), 2)

    def test_pushes_on_different_branches_not_collapsed(self):
        events = [
            push("2026-11-21T10:00:00Z", "crisesarmiento/cstack", branch="feature-x"),
            push("2026-11-21T09:00:00Z", "crisesarmiento/cstack", branch="feature-y"),
        ]
        rows = gmc.build_flight_log(events)
        self.assertEqual(len(rows), 2)

    def test_identical_pr_opened_collapsed(self):
        events = [
            pr_event("2026-11-21T10:00:00Z", "crisesarmiento/cstack", "opened", 7, title=""),
            pr_event("2026-11-21T09:00:00Z", "crisesarmiento/cstack", "opened", 7, title="Real title"),
        ]
        rows = gmc.build_flight_log(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 2)
        # Title backfilled from the older duplicate when the newest had none.
        self.assertEqual(rows[0]["title"], "Real title")

    def test_different_pr_numbers_not_collapsed(self):
        events = [
            pr_event("2026-11-21T10:00:00Z", "crisesarmiento/cstack", "opened", 5),
            pr_event("2026-11-21T09:00:00Z", "crisesarmiento/cstack", "opened", 6),
        ]
        rows = gmc.build_flight_log(events)
        self.assertEqual(len(rows), 2)

    def test_issue_opened_vs_closed_not_collapsed(self):
        events = [
            issue_event("2026-11-21T10:00:00Z", "crisesarmiento/cstack", "closed", 3),
            issue_event("2026-11-21T09:00:00Z", "crisesarmiento/cstack", "opened", 3),
        ]
        rows = gmc.build_flight_log(events)
        self.assertEqual({r["verb"] for r in rows}, {"ISSUE OPENED", "ISSUE CLOSED"})

    def test_same_events_different_days_not_collapsed(self):
        events = [
            push("2026-11-21T10:00:00Z", "crisesarmiento/cstack", size=2, branch="main"),
            push("2026-11-20T10:00:00Z", "crisesarmiento/cstack", size=2, branch="main"),
        ]
        rows = gmc.build_flight_log(events)
        self.assertEqual(len(rows), 2)


class TestFlightLogOrderAndLimit(unittest.TestCase):
    def test_newest_first_regardless_of_input_order(self):
        events = [
            push("2026-11-19T10:00:00Z", "crisesarmiento/cstack"),
            push("2026-11-21T10:00:00Z", "crisesarmiento/useroute"),
            push("2026-11-20T10:00:00Z", "crisesarmiento/cstack"),
        ]
        rows = gmc.build_flight_log(events)
        self.assertEqual([r["ts"] for r in rows],
                         ["2026-11-21T10:00:00Z", "2026-11-20T10:00:00Z", "2026-11-19T10:00:00Z"])

    def test_limit_is_applied_after_collapse(self):
        events = [
            push(f"2026-11-{21 - i // 3:02d}T10:00:00Z", f"crisesarmiento/repo{i}", msg=f"e{i}")
            for i in range(15)
        ]
        rows = gmc.build_flight_log(events, limit=11)
        self.assertEqual(len(rows), 11)

    def test_event_without_timestamp_is_skipped(self):
        events = [push("", "crisesarmiento/cstack")]
        self.assertEqual(gmc.build_flight_log(events), [])


class TestFlightLogFilters(unittest.TestCase):
    def test_excluded_repos_case_insensitive(self):
        events = [
            push("2026-11-21T10:00:00Z", "Palehour/anything"),
            push("2026-11-21T10:00:00Z", "palehour/other"),
            push("2026-11-21T10:00:00Z", "PALEHOUR/mixed-case"),
            push("2026-11-21T10:00:00Z", "SomeUser/Londinium"),
            push("2026-11-21T10:00:00Z", "someuser/londinium"),
            push("2026-11-21T10:00:00Z", "crisesarmiento/cstack"),
        ]
        rows = gmc.build_flight_log(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repo"], "crisesarmiento/cstack")

    def test_suffix_boundary_respected(self):
        # "londinium2" is a different repo name and must survive.
        self.assertFalse(gmc.excluded_repo("crisesarmiento/londinium2"))
        self.assertTrue(gmc.excluded_repo("crisesarmiento/Londinium"))

    def test_forbidden_repo_text_drops_event(self):
        events = [push("2026-11-21T10:00:00Z", "someone/horrorsynth-tools")]
        self.assertEqual(gmc.build_flight_log(events), [])

    def test_unsafe_title_dropped_event_kept_for_route(self):
        events = [push("2026-11-21T10:00:00Z", "crisesarmiento/useroute",
                       size=3, branch="main", msg="deploy useroute.dev now")]
        rows = gmc.build_flight_log(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "")
        self.assertEqual(rows[0]["label"], "Route")
        self.assertEqual(rows[0]["verb"], "PUSH")

    def test_unsafe_title_dropped_for_issue(self):
        events = [issue_event("2026-11-21T10:00:00Z", "crisesarmiento/cstack",
                              "opened", 9, title="bergagna private notes")]
        rows = gmc.build_flight_log(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "")
        self.assertEqual(rows[0]["extra"], "#9")

    def test_unsafe_branch_dropped_from_extra_event_kept(self):
        events = [push("2026-11-21T10:00:00Z", "crisesarmiento/cstack",
                       size=2, branch="bergagna-secret", msg="ok")]
        rows = gmc.build_flight_log(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["extra"], "")
        self.assertEqual(rows[0]["title"], "ok")

    def test_unsafe_pr_title_dropped_number_kept(self):
        events = [pr_event("2026-11-21T10:00:00Z", "crisesarmiento/cstack",
                           "opened", 12, title="mailto:someone@example.com")]
        rows = gmc.build_flight_log(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "")
        self.assertEqual(rows[0]["extra"], "#12")

    def test_arbitrary_email_title_dropped_event_kept(self):
        events = [push("2026-11-21T10:00:00Z", "crisesarmiento/cstack",
                       msg="reach me at cristian@example.com for details")]
        rows = gmc.build_flight_log(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "")
        self.assertEqual(rows[0]["verb"], "PUSH")

    def test_merged_pull_request_verb(self):
        events = [pr_event("2026-11-21T10:00:00Z", "crisesarmiento/cstack",
                           "closed", 5, merged=True)]
        rows = gmc.build_flight_log(events)
        self.assertEqual(rows[0]["verb"], "PR MERGED")

    def test_unknown_action_pr_skipped(self):
        events = [pr_event("2026-11-21T10:00:00Z", "crisesarmiento/cstack", "synchronize", 5)]
        self.assertEqual(gmc.build_flight_log(events), [])


class TestSanitization(unittest.TestCase):
    def test_forbidden_text_is_case_insensitive(self):
        for bad in ("useroute.dev", "LONDINIUM", "Cristian Bergagna",
                    "HORRORSYNTH", "Mailto:x@y.z", "@TESTSENSE.AI"):
            self.assertTrue(gmc.forbidden_text(bad), bad)

    def test_safe_text_passes(self):
        for ok in ("useroute", "Route", "Mission Control", "cstack main",
                   "testsense core", "Palehour"):
            self.assertFalse(gmc.forbidden_text(ok), ok)

    def test_arbitrary_email_addresses_are_forbidden(self):
        for bad in ("cristian@example.com", "foo.bar+baz@sub.domain.org",
                    "ping x_y-z%1@test-site.io", "a@b.co",
                    "41898282+actions@users.noreply.github.com"):
            self.assertTrue(gmc.forbidden_text(bad), bad)

    def test_mentions_without_domains_are_not_emails(self):
        for ok in ("@crisesarmiento", "see @testsense notes", "issue #12", "v1.2.3"):
            self.assertFalse(gmc.forbidden_text(ok), ok)

    def test_esc_escapes_markup(self):
        self.assertEqual(gmc.esc('<a href="x">&\'</a>'),
                         "&lt;a href=&quot;x&quot;&gt;&amp;&#x27;&lt;/a&gt;")

    def test_display_repo(self):
        self.assertEqual(gmc.display_repo("crisesarmiento/useroute"), "Route")
        self.assertEqual(gmc.display_repo("crisesarmiento/foo"), "foo")
        self.assertEqual(gmc.display_repo("bare"), "bare")

    def test_excluded_repo_direct(self):
        self.assertTrue(gmc.excluded_repo("Palehour/londinium"))
        self.assertTrue(gmc.excluded_repo("palehour/londinium"))
        self.assertFalse(gmc.excluded_repo("crisesarmiento/cstack"))


class TestVisibilityNotes(unittest.TestCase):
    def test_private_evidence_notes_mention_private_total(self):
        notes = gmc.visibility_notes(True, 34)
        self.assertTrue(notes["sees_private"])
        self.assertIn("34", notes["year"])
        self.assertIn("private", notes["year"].lower())
        self.assertIn("public", notes["streak"])
        self.assertIn("private", notes["history"].lower())

    def test_no_evidence_notes_never_claim_zero_private(self):
        notes = gmc.visibility_notes(False, 0)
        self.assertFalse(notes["sees_private"])
        self.assertIn("not shown as zero", notes["year"])
        self.assertIn("not shown as zero", notes["history"])
        self.assertIn("public", notes["year"].lower())
        self.assertIn("visible", notes["streak"])
        # No fabricated private figure may appear anywhere.
        for value in notes.values():
            if isinstance(value, str):
                self.assertNotIn("0 private", value)

    def test_history_note_differs_by_mode(self):
        self.assertNotEqual(gmc.visibility_notes(True, 5)["history"],
                            gmc.visibility_notes(False, 5)["history"])


class TestLanguageData(unittest.TestCase):
    def test_spectrum_aggregates_bytes_across_repos(self):
        nodes = [
            {"languages": {"edges": [
                {"size": 100, "node": {"name": "Python"}},
                {"size": 40, "node": {"name": "Rust"}},
            ]}},
            {"languages": {"edges": [
                {"size": 80, "node": {"name": "Rust"}},
                {"size": 20, "node": {"name": "Shell"}},
            ]}},
        ]
        bands = gmc.language_spectrum(nodes, top=3)
        self.assertEqual(bands[0][0], "Rust")
        self.assertEqual(bands[0][1], 120)
        self.assertAlmostEqual(sum(b[2] for b in bands), 1.0)

    def test_complete_repo_languages_follows_every_page(self):
        node = {"name": "repo-a",
                "languages": {"edges": [{"size": 10, "node": {"name": "Python"}}],
                              "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}
        pages = [
            {"repository": {"languages": {
                "edges": [{"size": 20, "node": {"name": "Rust"}}],
                "pageInfo": {"hasNextPage": True, "endCursor": "c2"}}}},
            {"repository": {"languages": {
                "edges": [{"size": 30, "node": {"name": "Shell"}}],
                "pageInfo": {"hasNextPage": False, "endCursor": "c3"}}}},
        ]
        calls = []

        def fake(query):
            calls.append(query)
            self.assertIn("repository(owner:", query)
            self.assertIn('"repo-a"', query)
            self.assertIn('"c%d"' % (len(calls)), query)
            return pages[len(calls) - 1]

        gmc.complete_repo_languages(node, fetch_page=fake)
        self.assertEqual(len(calls), 2)
        self.assertEqual([e["node"]["name"] for e in node["languages"]["edges"]],
                         ["Python", "Rust", "Shell"])
        self.assertFalse(node["languages"]["pageInfo"]["hasNextPage"])

    def test_complete_repo_languages_single_page_no_calls(self):
        node = {"name": "repo-b",
                "languages": {"edges": [{"size": 1, "node": {"name": "Python"}}],
                              "pageInfo": {"hasNextPage": False, "endCursor": None}}}

        def fail(query):
            raise AssertionError("no page fetch expected")

        gmc.complete_repo_languages(node, fetch_page=fail)
        self.assertEqual(len(node["languages"]["edges"]), 1)

    def test_complete_repo_languages_converges_or_raises(self):
        node = {"name": "repo-c",
                "languages": {"edges": [],
                              "pageInfo": {"hasNextPage": True, "endCursor": "always"}}}
        spin = {"repository": {"languages": {
            "edges": [{"size": 1, "node": {"name": "X"}}],
            "pageInfo": {"hasNextPage": True, "endCursor": "always"}}}}

        def fake(query):
            return spin

        with self.assertRaises(RuntimeError):
            gmc.complete_repo_languages(node, fetch_page=fake)


class TestRepositoryPagination(unittest.TestCase):
    def test_collect_repositories_walks_all_pages(self):
        first_block = {
            "totalCount": 2,
            "pageInfo": {"hasNextPage": True, "endCursor": "r1"},
            "nodes": [
                {"name": "repo-a",
                 "languages": {"edges": [{"size": 5, "node": {"name": "Python"}}],
                                "pageInfo": {"hasNextPage": True, "endCursor": "l1"}}},
            ],
        }
        second_page = {
            "user": {"repositories": {
                "pageInfo": {"hasNextPage": False, "endCursor": "r2"},
                "nodes": [
                    {"name": "repo-b",
                     "languages": {"edges": [{"size": 7, "node": {"name": "Rust"}}],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None}}},
                ]}},
        }
        languages_page = {
            "repository": {"languages": {
                "edges": [{"size": 9, "node": {"name": "Shell"}}],
                "pageInfo": {"hasNextPage": False, "endCursor": "l2"}}},
        }

        def fake(query):
            if "repository(owner:" in query:
                return languages_page
            if "after:" in query:
                return second_page
            raise AssertionError("unexpected query: " + query)

        nodes = gmc.collect_repositories(first_block, fetch_page=fake)
        self.assertEqual([n["name"] for n in nodes], ["repo-a", "repo-b"])
        # repo-a's language edges were completed past the first page.
        self.assertEqual([e["node"]["name"] for e in nodes[0]["languages"]["edges"]],
                         ["Python", "Shell"])

    def test_collect_repositories_empty_page_raises(self):
        first_block = {"pageInfo": {"hasNextPage": True, "endCursor": "r1"}, "nodes": []}

        def fake(query):
            return {"user": {}}

        with self.assertRaises(RuntimeError):
            gmc.collect_repositories(first_block, fetch_page=fake)

    def test_collect_repositories_lost_cursor_raises(self):
        first_block = {"pageInfo": {"hasNextPage": True, "endCursor": None}, "nodes": []}
        with self.assertRaises(RuntimeError):
            gmc.collect_repositories(first_block, fetch_page=lambda q: {})


class TestYearHistory(unittest.TestCase):
    def test_year_history_parses_aliased_blocks(self):
        def fake(query):
            self.assertIn("y2025:", query)
            self.assertIn("y2026:", query)
            self.assertIn("restrictedContributionsCount", query)
            return {"user": {
                "y2025": {"contributionCalendar": {"totalContributions": 100},
                          "restrictedContributionsCount": 40},
                "y2026": {"contributionCalendar": {"totalContributions": 60},
                          "restrictedContributionsCount": 0},
            }}

        years = gmc.fetch_year_history("2025-05-05T00:00:00Z", "2026-11-21T23:59:59Z", 2026,
                                        fetch_page=fake)
        self.assertEqual(years, [
            {"year": 2025, "total": 100, "private": 40, "public": 60, "ytd": False},
            {"year": 2026, "total": 60, "private": 0, "public": 60, "ytd": True},
        ])

    def test_year_history_missing_created_falls_back_to_single_year(self):
        def fake(query):
            self.assertNotIn("y2025:", query)
            return {"user": {"y2026": {"contributionCalendar": {"totalContributions": 5},
                                        "restrictedContributionsCount": 1}}}

        years = gmc.fetch_year_history("", "2026-11-21T23:59:59Z", 2026, fetch_page=fake)
        self.assertEqual(len(years), 1)
        self.assertEqual(years[0]["public"], 4)
        self.assertTrue(years[0]["ytd"])


class TestSeesPrivateTotals(unittest.TestCase):
    def test_nonzero_history_private_is_evidence(self):
        years = [{"year": 2025, "total": 100, "private": 40, "public": 60, "ytd": False},
                 {"year": 2026, "total": 60, "private": 0, "public": 60, "ytd": True}]
        self.assertTrue(gmc.sees_private_totals(years, 0))

    def test_nonzero_ytd_private_is_evidence(self):
        years = [{"year": 2026, "total": 60, "private": 0, "public": 60, "ytd": True}]
        self.assertTrue(gmc.sees_private_totals(years, 34))

    def test_all_zero_restricted_is_not_evidence(self):
        # An owner with genuinely all-zero restricted counts cannot prove
        # private access; captions must stay public-only. The bars are
        # never drawn from an unproven private split.
        years = [{"year": 2026, "total": 60, "private": 0, "public": 60, "ytd": True}]
        self.assertFalse(gmc.sees_private_totals(years, 0))

    def test_empty_history_is_not_evidence(self):
        self.assertFalse(gmc.sees_private_totals([], 0))


class TestGhErrorsAreConcise(unittest.TestCase):
    def test_gh_failure_keeps_only_the_summary_line(self):
        err = subprocess.CalledProcessError(
            1, ["gh"],
            stderr="warning: something\nSCARY RAW DUMP with request details\ngh: Bad credentials",
        )
        with mock.patch.object(gmc.subprocess, "run", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                gmc.run_gh("api", "user")
        msg = str(ctx.exception)
        self.assertIn("Bad credentials", msg)
        self.assertNotIn("SCARY RAW DUMP", msg)

    def test_gh_missing_binary(self):
        with mock.patch.object(gmc.subprocess, "run", side_effect=FileNotFoundError("no gh")):
            with self.assertRaises(RuntimeError) as ctx:
                gmc.run_gh("api", "user")
        self.assertIn("gh CLI not found", str(ctx.exception))

    def test_graphql_non_json(self):
        with mock.patch.object(gmc, "run_gh", return_value="not json"):
            with self.assertRaises(RuntimeError) as ctx:
                gmc.gh_graphql("{ x }")
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_graphql_error_reports_first_message_only(self):
        payload = '{"errors": [{"message": "First problem"}, {"message": "Second problem"}]}'
        with mock.patch.object(gmc, "run_gh", return_value=payload):
            with self.assertRaises(RuntimeError) as ctx:
                gmc.gh_graphql("{ x }")
        msg = str(ctx.exception)
        self.assertIn("First problem", msg)
        self.assertNotIn("Second problem", msg)

    def test_graphql_missing_data(self):
        with mock.patch.object(gmc, "run_gh", return_value="{}"):
            with self.assertRaises(RuntimeError):
                gmc.gh_graphql("{ x }")


class TestHeatAndFormats(unittest.TestCase):
    def test_heat_color_zero_and_zero_max(self):
        t = gmc.THEMES["dark"]
        self.assertEqual(gmc.heat_color(0, 10, t), t["heat0"])
        self.assertEqual(gmc.heat_color(5, 0, t), t["heat0"])

    def test_heat_color_buckets(self):
        t = gmc.THEMES["dark"]
        self.assertEqual(gmc.heat_color(1, 100, t), t["heat1"])
        self.assertEqual(gmc.heat_color(20, 100, t), t["heat2"])
        self.assertEqual(gmc.heat_color(50, 100, t), t["heat3"])
        self.assertEqual(gmc.heat_color(90, 100, t), t["heat4"])

    def test_fmt_helpers(self):
        self.assertEqual(gmc.fmt_int(1234567), "1,234,567")
        self.assertEqual(gmc.fmt_day("2026-03-05"), "5 MAR 2026")
        self.assertEqual(gmc.fmt_day(""), "—")
        self.assertEqual(gmc.fmt_short("2026-03-21T18:10:16Z"), "21 MAR 2026")

    def test_out_dir_defaults_relative_to_repo_root(self):
        self.assertEqual(gmc.REPO_ROOT, Path(__file__).resolve().parent.parent.parent)
        self.assertEqual(gmc.OUT_DIR, gmc.REPO_ROOT / "assets" / "mission-control")


if __name__ == "__main__":
    unittest.main()
