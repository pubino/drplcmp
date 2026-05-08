"""Unit and integration tests for revision_finder.py.

Uses tiny mock .sql fixtures under tests/fixtures/. No real database is
touched; the SQL files are parsed directly.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import revision_finder as rf

FIXTURES = Path(__file__).parent / "fixtures"
BACKUP_A = FIXTURES / "backup_a.sql"
BACKUP_B = FIXTURES / "backup_b.sql"


# ---------------------------------------------------------------------------
# Tokenizer / parser
# ---------------------------------------------------------------------------
def test_iter_sql_statements_handles_strings_and_comments():
    # Semicolons inside line comments, block comments, and string literals
    # must NOT terminate a statement. Two real statements should be yielded.
    text = (
        "INSERT INTO `t` VALUES ('he said: ;', 'ok');\n"
        "-- a comment with ; semicolon\n"
        "/* block ; comment */\n"
        "SELECT 1;\n"
    )
    stmts = list(rf.iter_sql_statements(text))
    assert len(stmts) == 2
    assert stmts[0].lstrip().startswith("INSERT INTO")
    # second statement may have the leading comments folded in; assert tail
    assert stmts[1].rstrip(";").rstrip().endswith("SELECT 1")


def test_parse_value_tuples_basic():
    text = "(1,'foo',NULL),(2,'bar\\'s',3.5);"
    out = rf.parse_value_tuples(text)
    assert out == [(1, "foo", None), (2, "bar's", 3.5)]


def test_parse_value_tuples_doubled_apostrophe():
    text = "(1,'it''s fine');"
    assert rf.parse_value_tuples(text) == [(1, "it's fine")]


def test_parse_create_table_extracts_columns():
    stmt = (
        "CREATE TABLE `node_field_data` (\n"
        "  `nid` int unsigned NOT NULL,\n"
        "  `type` varchar(32) NOT NULL,\n"
        "  `title` varchar(255) NOT NULL,\n"
        "  PRIMARY KEY (`nid`)\n"
        ")"
    )
    table, cols = rf.parse_create_table(stmt)
    assert table == "node_field_data"
    assert cols == ["nid", "type", "title"]


def test_parse_insert_uses_columns_map():
    stmt = "INSERT INTO `t` VALUES (1,'a'),(2,'b');"
    table, rows = rf.parse_insert(stmt, {"t": ["id", "name"]})
    assert table == "t"
    assert rows == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


# ---------------------------------------------------------------------------
# Argparse type validators
# ---------------------------------------------------------------------------
def test_validate_machine_name_accepts_drupal_style():
    assert rf.validate_machine_name("article") == "article"
    assert rf.validate_machine_name("blog_post_v2") == "blog_post_v2"


@pytest.mark.parametrize(
    "bad",
    [
        "Article",  # uppercase
        "1starts_numeric",
        "with-dashes",
        "with space",
        "drop;table",  # injection-style
        "'or'1",
        "",
        "x" * 33,
    ],
)
def test_validate_machine_name_rejects_garbage(bad):
    with pytest.raises(Exception):
        rf.validate_machine_name(bad)


def test_validate_sql_file_rejects_non_sql(tmp_path):
    p = tmp_path / "not.txt"
    p.write_text("hi")
    with pytest.raises(Exception):
        rf.validate_sql_file(str(p))


def test_validate_sql_file_rejects_empty(tmp_path):
    p = tmp_path / "empty.sql"
    p.write_text("")
    with pytest.raises(Exception):
        rf.validate_sql_file(str(p))


def test_validate_sql_file_accepts_valid(tmp_path):
    p = tmp_path / "ok.sql"
    p.write_text("-- a sql file\n")
    assert rf.validate_sql_file(str(p)) == p


def test_parse_datetime_arg_handles_multiple_formats():
    a = rf.parse_datetime_arg("2025-01-15T12:30:00")
    b = rf.parse_datetime_arg("2025-01-15 12:30:00")
    c = rf.parse_datetime_arg("2025-01-15")
    assert a.year == b.year == c.year == 2025
    assert a.tzinfo is timezone.utc


def test_parse_datetime_arg_rejects_garbage():
    with pytest.raises(Exception):
        rf.parse_datetime_arg("yesterday")


# ---------------------------------------------------------------------------
# Dump-level
# ---------------------------------------------------------------------------
def test_parse_dump_extracts_node_field_data():
    data = rf.parse_dump(BACKUP_A)
    assert "node_field_data" in data.seen_tables
    rows = data.rows["node_field_data"]
    assert len(rows) == 3
    by_nid = {r["nid"]: r for r in rows}
    assert by_nid[1]["title"] == "Hello"
    assert by_nid[1]["type"] == "article"


def test_looks_like_drupal_accepts_fixture():
    data = rf.parse_dump(BACKUP_A)
    assert rf.looks_like_drupal(data)


def test_looks_like_drupal_accepts_prefixed_tables(tmp_path):
    # Real-world Drupal sites often install with a table prefix such as
    # ``drup_``.  Both Drupal-fingerprint detection and node row collection
    # must work on prefixed tables.
    p = tmp_path / "prefixed.sql"
    p.write_text(
        "CREATE TABLE `drup_node_field_data` (\n"
        "  `nid` int unsigned NOT NULL,\n"
        "  `type` varchar(32) NOT NULL,\n"
        "  `title` varchar(255) NOT NULL,\n"
        "  `changed` int NOT NULL,\n"
        "  PRIMARY KEY (`nid`)\n"
        ");\n"
        "INSERT INTO `drup_node_field_data` VALUES (7,'report','T',1700000000);\n"
        "CREATE TABLE `drup_users_field_data` (`uid` int);\n"
        "CREATE TABLE `drup_key_value` (`name` varchar(128));\n"
    )
    data = rf.parse_dump(p)
    assert rf.looks_like_drupal(data)
    rows = data.rows[rf.NODE_FIELD_DATA]
    assert any(r["type"] == "report" and r["nid"] == 7 for r in rows)


def test_looks_like_drupal_rejects_non_drupal(tmp_path):
    p = tmp_path / "fake.sql"
    p.write_text(
        "CREATE TABLE `widgets` (`id` int);\nINSERT INTO `widgets` VALUES (1);\n"
    )
    data = rf.parse_dump(p)
    assert not rf.looks_like_drupal(data)


def test_dump_completion_marker_parsed():
    data = rf.parse_dump(BACKUP_A)
    assert data.dump_completed_at is not None
    assert data.dump_completed_at.year == 2025


# ---------------------------------------------------------------------------
# Differing-nid detection
# ---------------------------------------------------------------------------
def _window():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 3, tzinfo=timezone.utc)
    return start, end


def test_find_differing_nids_returns_modified_and_added():
    da = rf.parse_dump(BACKUP_A)
    db = rf.parse_dump(BACKUP_B)
    start, end = _window()
    nids, _, _ = rf.find_differing_nids(da, db, "article", start, end)
    assert nids == [1, 4]


def test_find_differing_nids_excludes_other_content_types():
    da = rf.parse_dump(BACKUP_A)
    db = rf.parse_dump(BACKUP_B)
    start, end = _window()
    nids, _, _ = rf.find_differing_nids(da, db, "page", start, end)
    assert nids == []


def test_find_differing_nids_respects_window():
    da = rf.parse_dump(BACKUP_A)
    db = rf.parse_dump(BACKUP_B)
    # Window that excludes both 2025-01-01 and 2025-01-02 changed timestamps.
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    end = datetime(2030, 12, 31, tzinfo=timezone.utc)
    nids, _, _ = rf.find_differing_nids(da, db, "article", start, end)
    assert nids == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_validate_inputs_rejects_same_file():
    with pytest.raises(rf.ValidationError):
        rf.validate_inputs(BACKUP_A, BACKUP_A, "article")


def test_validate_inputs_rejects_identical_content(tmp_path):
    a = tmp_path / "a.sql"
    b = tmp_path / "b.sql"
    shutil.copyfile(BACKUP_A, a)
    shutil.copyfile(BACKUP_A, b)
    with pytest.raises(rf.ValidationError, match="identical"):
        rf.validate_inputs(a, b, "article")


def test_validate_inputs_rejects_missing_content_type():
    with pytest.raises(rf.ValidationError, match="neither backup contains"):
        rf.validate_inputs(BACKUP_A, BACKUP_B, "no_such_type")


def test_validate_inputs_rejects_non_drupal_input(tmp_path):
    bad = tmp_path / "bad.sql"
    bad.write_text("CREATE TABLE `x` (`a` int);\n")
    with pytest.raises(rf.ValidationError, match="does not look like a Drupal"):
        rf.validate_inputs(bad, BACKUP_B, "article")


def test_validate_inputs_accepts_good_pair():
    da, db = rf.validate_inputs(BACKUP_A, BACKUP_B, "article")
    assert da.path == BACKUP_A
    assert db.path == BACKUP_B


# ---------------------------------------------------------------------------
# Diff rendering
# ---------------------------------------------------------------------------
def test_render_diff_no_color_contains_changed_field():
    da = rf.parse_dump(BACKUP_A)
    db = rf.parse_dump(BACKUP_B)
    start, end = _window()
    nids, ma, mb = rf.find_differing_nids(da, db, "article", start, end)
    out = rf.render_diff(1, "A", "B", ma.get(1), mb.get(1), use_color=False)
    assert "node 1" in out
    assert "Hello" in out
    assert "Hello UPDATED" in out
    # Ensure no ANSI codes when use_color=False.
    assert "\033[" not in out


def test_render_diff_with_color_contains_ansi():
    da = rf.parse_dump(BACKUP_A)
    db = rf.parse_dump(BACKUP_B)
    start, end = _window()
    _, ma, mb = rf.find_differing_nids(da, db, "article", start, end)
    out = rf.render_diff(1, "A", "B", ma.get(1), mb.get(1), use_color=True)
    assert "\033[" in out


def test_render_diff_handles_added_node():
    da = rf.parse_dump(BACKUP_A)
    db = rf.parse_dump(BACKUP_B)
    _, ma, mb = rf.find_differing_nids(
        da, db, "article", *_window()
    )
    out = rf.render_diff(4, "A", "B", ma.get(4), mb.get(4), use_color=False)
    assert "absent" in out
    assert "New" in out


# ---------------------------------------------------------------------------
# CLI integration via main()
# ---------------------------------------------------------------------------
def test_cli_lists_differing_ids(capsys):
    code = rf.main(
        [
            str(BACKUP_A),
            str(BACKUP_B),
            "--type",
            "article",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-03",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Differing node ids" in out
    assert "1" in out
    assert "4" in out


def test_cli_limit_truncates(capsys):
    code = rf.main(
        [
            str(BACKUP_A),
            str(BACKUP_B),
            "--type",
            "article",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-03",
            "--limit",
            "1",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    # nid 1 should be present, nid 4 should be excluded
    listed = [
        line.strip()
        for line in out.splitlines()
        if line.strip().isdigit()
    ]
    assert listed == ["1"]


def test_cli_diff_all_renders_each(capsys):
    rf.main(
        [
            str(BACKUP_A),
            str(BACKUP_B),
            "--type",
            "article",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-03",
            "--diff-all",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "=== node 1 ===" in out
    assert "=== node 4 ===" in out


def test_cli_diff_single_node(capsys):
    rf.main(
        [
            str(BACKUP_A),
            str(BACKUP_B),
            "--type",
            "article",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-03",
            "--diff",
            "1",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "=== node 1 ===" in out
    assert "Hello UPDATED" in out


def test_cli_rejects_bad_machine_name(capsys):
    with pytest.raises(SystemExit):
        rf.main(
            [
                str(BACKUP_A),
                str(BACKUP_B),
                "--type",
                "drop;table",
                "--start",
                "2025-01-01",
                "--end",
                "2025-01-03",
            ]
        )


def test_cli_format_table(capsys):
    rf.main(
        [
            str(BACKUP_A),
            str(BACKUP_B),
            "--type",
            "article",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-03",
            "--format",
            "table",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    # Box-drawing characters and the column headers should be present.
    assert "┌" in out and "┐" in out and "└" in out
    assert "NID" in out and "Status" in out
    assert "modified" in out  # nid 1 has different title
    assert "added" in out  # nid 4 only in B


def test_cli_format_json_is_valid(capsys):
    import json as _json

    rf.main(
        [
            str(BACKUP_A),
            str(BACKUP_B),
            "--type",
            "article",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-03",
            "--format",
            "json",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    payload = _json.loads(out)
    assert payload["count"] == 2
    assert payload["node_ids"] == [1, 4]
    statuses = {item["nid"]: item["status"] for item in payload["items"]}
    assert statuses == {1: "modified", 4: "added"}


def test_cli_format_proof_has_deterministic_hash(capsys):
    import json as _json

    def run_proof():
        rf.main(
            [
                str(BACKUP_A),
                str(BACKUP_B),
                "--type",
                "article",
                "--start",
                "2025-01-01",
                "--end",
                "2025-01-03",
                "--format",
                "proof",
                "--no-color",
            ]
        )
        return _json.loads(capsys.readouterr().out)

    p1 = run_proof()
    p2 = run_proof()

    # generated_at is metadata and may differ across runs, but the
    # proof_sha256 covers only inputs+parameters+result, so it must be
    # byte-stable for identical inputs.
    assert p1["proof_sha256"] == p2["proof_sha256"]
    assert len(p1["proof_sha256"]) == 64  # hex sha256
    assert p1["result"]["count"] == 2
    assert p1["inputs"]["file_a"]["sha256"]
    assert p1["inputs"]["file_b"]["sha256"]
    assert p1["inputs"]["file_a"]["sha256"] != p1["inputs"]["file_b"]["sha256"]


def test_cli_format_proof_changes_when_param_changes(capsys):
    import json as _json

    rf.main(
        [
            str(BACKUP_A),
            str(BACKUP_B),
            "--type",
            "article",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-03",
            "--format",
            "proof",
            "--no-color",
        ]
    )
    p1 = _json.loads(capsys.readouterr().out)

    rf.main(
        [
            str(BACKUP_A),
            str(BACKUP_B),
            "--type",
            "article",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-03",
            "--limit",
            "1",
            "--format",
            "proof",
            "--no-color",
        ]
    )
    p2 = _json.loads(capsys.readouterr().out)

    # Different limit -> different result -> different proof.
    assert p1["proof_sha256"] != p2["proof_sha256"]


def test_cli_rejects_same_file(capsys):
    code = rf.main(
        [
            str(BACKUP_A),
            str(BACKUP_A),
            "--type",
            "article",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-03",
            "--no-color",
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "same file" in err
