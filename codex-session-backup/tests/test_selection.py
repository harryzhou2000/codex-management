"""Guards for tree resolution: unique parentage, no double attachment, no infinite walk."""

from __future__ import annotations

import unittest

from codex_session_backup import selection

OLDER = "2026-08-31"
MIN = "1GiB"


def row(session_id, *, source="subagent", parent="", issue="", size=0, age="2026-08-01 00:00", **extra):
    base = {
        "session_id": session_id,
        "thread_source": source,
        "parent_thread_id": parent,
        "parent_issue": issue,
        "role_label": "main" if source == "user" else "sub",
        "thread_depth": 0,
        "size_bytes": size,
        "size_human": "0 B",
        "exists": True,
        "orphan": False,
        "archived": 0,
        "recently_active": False,
        "last_activity": age,
        "last_activity_epoch": 1_750_000_000.0,
        "display_name": session_id,
    }
    base.update(extra)
    return base


class TreeResolutionTests(unittest.TestCase):
    def test_unique_chain_attaches_every_descendant_once(self):
        rows = [
            row("main", source="user", size=2 * 1024**3),
            row("a", parent="main", size=10),
            row("b", parent="a", size=10),
            row("c", parent="main", size=10),
        ]
        selected, warnings = selection.apply_filters(rows, older_than=OLDER, min_size=MIN)
        ids = [item["session_id"] for item in selected]
        self.assertEqual(sorted(ids), ["a", "b", "c", "main"])
        self.assertEqual(len(ids), len(set(ids)), "no session may appear twice")
        depths = {item["session_id"]: item["tree_depth"] for item in selected}
        self.assertEqual(depths, {"main": 0, "a": 1, "b": 2, "c": 1})
        self.assertEqual({item["tree_root"] for item in selected}, {"main"})
        self.assertEqual(warnings, [])

    def test_ambiguous_or_conflicting_parent_is_excluded_with_warning(self):
        rows = [
            row("main", source="user", size=2 * 1024**3),
            row("good", parent="main"),
            row("ambiguous", parent="main", issue="ambiguous-parent-path"),
            row("conflict", parent="main", issue="parent-conflict"),
            row("orphanish", parent="ghost"),          # parent missing from the catalog
            row("grandchild-of-ambiguous", parent="ambiguous"),
        ]
        selected, warnings = selection.apply_filters(rows, older_than=OLDER, min_size=MIN)
        ids = {item["session_id"] for item in selected}
        self.assertEqual(ids, {"main", "good"}, "rows with a parent issue must stay out")
        self.assertTrue(warnings, "excluded rows must be reported")
        self.assertTrue(any("parent" in warning for warning in warnings))

    def test_disconnected_cycle_is_ignored_and_walk_terminates(self):
        # A single parent per row means a reachable cycle is impossible; a disconnected one must
        # still not hang the walk or leak into the plan.
        rows = [
            row("main", source="user", size=2 * 1024**3),
            row("x", parent="y"),
            row("y", parent="x"),
            row("selfie", parent="selfie"),
        ]
        selected, _ = selection.apply_filters(rows, older_than=OLDER, min_size=MIN)
        self.assertEqual([item["session_id"] for item in selected], ["main"])

    def test_subagents_none_selects_only_matching_rows(self):
        rows = [
            row("main", source="user", size=2 * 1024**3),
            row("a", parent="main"),
        ]
        selected, warnings = selection.apply_filters(
            rows, older_than=OLDER, min_size=MIN, subagents="none"
        )
        self.assertEqual([item["session_id"] for item in selected], ["main"])
        self.assertEqual(warnings, [])

    def test_subagent_rows_are_not_selected_by_default(self):
        rows = [
            row("main", source="user", size=2 * 1024**3),
            row("big-subagent", parent="main", size=5 * 1024**3),
        ]
        selected, _ = selection.apply_filters(rows, older_than=OLDER, min_size=MIN)
        self.assertEqual({item["selection"] for item in selected}, {"criteria", "subtree"})
        criteria = [item for item in selected if item["selection"] == "criteria"]
        self.assertEqual([item["session_id"] for item in criteria], ["main"])

    def test_multi_root_shared_child_attaches_to_one_root_only(self):
        rows = [
            row("main1", source="user", size=2 * 1024**3),
            row("main2", source="user", size=2 * 1024**3),
        ]
        # A child can only be reached through one parent chain, so it must land under one root.
        rows.append(row("shared", parent="main1"))
        selected, _ = selection.apply_filters(rows, older_than=OLDER, min_size=MIN)
        shared = [item for item in selected if item["session_id"] == "shared"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["tree_root"], "main1")

    def test_guardian_threads_excluded_unless_requested(self):
        rows = [
            row("main", source="user", size=2 * 1024**3),
            row("guardian", source="guardian_review", size=5 * 1024**3),
        ]
        selected, _ = selection.apply_filters(rows, older_than=OLDER, min_size=MIN)
        self.assertEqual([item["session_id"] for item in selected], ["main"])
        selected, _ = selection.apply_filters(
            rows, older_than=OLDER, min_size=MIN, thread_sources=("guardian_review",)
        )
        self.assertEqual([item["session_id"] for item in selected], ["guardian"])


if __name__ == "__main__":
    unittest.main()
