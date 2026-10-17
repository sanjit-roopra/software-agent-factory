"""Run the tagged-answer cache of the dashboard in ``node``.

``holdAnswer``, ``readTagged`` and ``apiFetch`` return promises, so each test drives
them in its own ``node`` script with a fake ``fetch`` and reads back what the script
printed as JSON.
"""

from __future__ import annotations

import json
import subprocess

from dashboard_js import definitions_source, require_node

from software_agent_factory.dashboard import assets as dashboard_assets

# readToken reads a meta tag; fetch answers from a queue and records each request.
_PRELUDE = """\
"use strict";
const document = {
  querySelector: () => ({ getAttribute: () => "tok" })
};
const requests = [];
const answers = [];
globalThis.fetch = (path, init) => {
  requests.push({ path: path, headers: init.headers });
  return Promise.resolve(answers.shift());
};
function answer(status, etag, body) {
  return {
    status: status,
    ok: status >= 200 && status < 300,
    headers: { get: (name) => (name === "ETag" ? etag : null) },
    json: () => Promise.resolve(body)
  };
}
"""


def _run(scenario: str) -> object:
    node = require_node()
    names = ["apiFetch", "holdAnswer", "readTagged", "heldAnswers", "MAX_HELD_ANSWERS"]
    script = "\n".join(
        [
            _PRELUDE,
            definitions_source(dashboard_assets.APP_JS, names),
            "(async () => {",
            scenario,
            "})().catch((error) => { console.error(error); process.exit(1); });",
        ]
    )
    completed = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_hold_answer_drops_the_oldest_path_past_the_limit() -> None:
    result = _run(
        """
        for (let i = 0; i <= MAX_HELD_ANSWERS; i += 1) { holdAnswer("/p" + i, "t" + i, i); }
        holdAnswer("/p1", "again", "fresh");
        holdAnswer("/new", "tn", "n");
        process.stdout.write(JSON.stringify({
          limit: MAX_HELD_ANSWERS,
          size: heldAnswers.size,
          first: [...heldAnswers.keys()][0],
          last: [...heldAnswers.keys()].slice(-2),
          p1: heldAnswers.get("/p1")
        }));
        """
    )
    assert result == {
        "limit": 32,
        "size": 32,
        # /p0 went first, then /p2: /p1 was held again, so it moved to the newest end.
        "first": "/p3",
        "last": ["/p1", "/new"],
        "p1": {"etag": "again", "body": "fresh"},
    }


def test_hold_answer_without_a_tag_forgets_the_held_answer() -> None:
    result = _run(
        """
        holdAnswer("/a", "t1", "body");
        holdAnswer("/a", null, "untagged");
        process.stdout.write(JSON.stringify({ held: heldAnswers.has("/a") }));
        """
    )
    assert result == {"held": False}


def test_read_tagged_reuses_the_held_body_on_304_and_holds_a_new_200() -> None:
    result = _run(
        """
        const held = { etag: "old", body: { n: 1 } };
        const reused = await readTagged("/r", held, answer(304, null, null));
        const fresh = await readTagged("/r", held, answer(200, "new", { n: 2 }));
        process.stdout.write(JSON.stringify({
          reused: reused, fresh: fresh, held: heldAnswers.get("/r")
        }));
        """
    )
    assert result == {
        "reused": {"n": 1},
        "fresh": {"n": 2},
        "held": {"etag": "new", "body": {"n": 2}},
    }


def test_api_fetch_sends_if_none_match_only_while_an_answer_is_held() -> None:
    result = _run(
        """
        answers.push(answer(200, "t1", { n: 1 }));
        answers.push(answer(304, null, null));
        answers.push(answer(200, null, { n: 2 }));
        answers.push(answer(200, null, { n: 3 }));
        const bodies = [];
        for (let i = 0; i < 4; i += 1) { bodies.push(await apiFetch("/api/runs")); }
        process.stdout.write(JSON.stringify({
          bodies: bodies,
          tags: requests.map((r) => r.headers["If-None-Match"] ?? null),
          tokens: requests.map((r) => r.headers["X-Factory-Token"])
        }));
        """
    )
    assert result == {
        "bodies": [{"n": 1}, {"n": 1}, {"n": 2}, {"n": 3}],
        # The untagged third answer drops the held one, so the fourth asks without a tag.
        "tags": [None, "t1", "t1", None],
        "tokens": ["tok", "tok", "tok", "tok"],
    }
