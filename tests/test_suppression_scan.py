from __future__ import annotations

from software_agent_factory.suppression_scan import (
    MAX_LISTED,
    SUPPRESSION_SCAN_COMMAND,
    Suppression,
    added_suppressions,
    suppression_items,
    suppression_scan,
)
from software_agent_factory.workspace import WorkspaceEvidence

DIFF = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 import os
-x = 1
+x = 1  # type: ignore[assignment]
+y = 2  # noqa: E501
 z = 3
@@ -10,2 +11,2 @@ def f():
-    old  # noqa
+    return os.sep
 # pragma: no cover
diff --git a/tests/test_app.py b/tests/test_app.py
new file mode 100644
--- /dev/null
+++ b/tests/test_app.py
@@ -0,0 +1,3 @@
+import pytest
+++ looks like a header but is an added line  # nosec
+@pytest.mark.skip(reason="later")
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1  # noqa
"""


def test_added_suppressions_reads_only_added_lines_with_their_new_line_numbers() -> None:
    assert added_suppressions(DIFF) == [
        Suppression("src/app.py", 2, "type: ignore", "x = 1  # type: ignore[assignment]"),
        Suppression("src/app.py", 3, "# noqa", "y = 2  # noqa: E501"),
        Suppression(
            "tests/test_app.py",
            2,
            "# nosec",
            "++ looks like a header but is an added line  # nosec",
        ),
        Suppression(
            "tests/test_app.py", 3, "pytest.mark.skip", '@pytest.mark.skip(reason="later")'
        ),
    ]


def test_added_suppressions_covers_javascript_markers() -> None:
    diff = (
        "+++ b/web/a.test.ts\n@@ -0,0 +1,4 @@\n"
        "+// @ts-ignore\n+it.skip('x', () => {})\n+/* eslint-disable */\n+const ok = 1\n"
    )

    markers = [item.marker for item in added_suppressions(diff)]

    assert markers == ["@ts-ignore", "it.skip(", "eslint-disable"]


def test_suppression_scan_is_none_without_markers() -> None:
    assert suppression_scan("+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1\n") is None
    assert suppression_scan("") is None


def test_suppression_scan_is_one_passing_check_that_lists_the_markers() -> None:
    check = suppression_scan(DIFF)

    assert check is not None
    assert check.command == SUPPRESSION_SCAN_COMMAND
    assert check.exit_code == 0
    assert check.stdout.splitlines()[:3] == [
        "suppression scan: 4 suppression or skip markers on added lines",
        "- src/app.py:2: x = 1  # type: ignore[assignment]",
        "- src/app.py:3: y = 2  # noqa: E501",
    ]


def test_suppression_scan_caps_the_listed_markers() -> None:
    count = MAX_LISTED + 2
    diff = f"+++ b/a.py\n@@ -0,0 +1,{count} @@\n" + "+x = 1  # noqa\n" * count

    check = suppression_scan(diff)

    assert check is not None
    lines = check.stdout.splitlines()
    assert len(lines) == MAX_LISTED + 2
    assert lines[-1] == "- and 2 more"


def test_suppression_items_name_the_place_and_the_marker() -> None:
    assert suppression_items(None) == []
    assert suppression_items(WorkspaceEvidence(diff=DIFF))[:2] == [
        "`src/app.py:2`: `type: ignore`",
        "`src/app.py:3`: `# noqa`",
    ]
