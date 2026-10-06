#!/usr/bin/env python3
"""Documentation contract for the governed parallel graph command.

The agent-facing docs must keep describing the `cowork graph` surface that the
code ships, so they cannot silently drift from it. Expectations are derived
from the live contract (`cowork_graph.REASON_RC`, `cowork_graph_cli.OPS` and
the execution-profile concurrency policy), never from a literal list, so a new
op, a new reason code or a changed cap makes a stale doc fail here.

Protected, in neutral terms:

- the README graph section carries one reason-code table row per rc, and the
  codes in each row are exactly the codes `REASON_RC` assigns to that rc;
- every op is described in the README and in both agent skills, and the agent
  notes name the command and its result key;
- a join is documented as a decision record and not a merge, and the
  production cap is stated and matches the profile policy;
- the agent-notes transport bullet for the graph command lists the graph rc
  set and says rc 5 is never used.

Run through the offline harness with this module's name as the explicit id.
"""

import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import cowork_execution_profiles  # noqa: E402
import cowork_graph  # noqa: E402
import cowork_graph_cli  # noqa: E402

REPO_ROOT = os.path.dirname(_HERE)
README_HEADING = "### Governed parallel graph"
TRANSPORT_HEADING = "## Agent command transport"
BACKTICKED = re.compile(r"`([a-z][a-z0-9_]*)`")
ROW_RC = re.compile(r"^\|\s*(\d+)\s*\|")


def _read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


def _flow(text):
    """Collapse line wrapping and case so a phrase check is not defeated by
    where a sentence happens to break."""
    return " ".join(text.split()).lower()


def _slice(text, heading, next_marker):
    start = text.index(heading)
    end = text.find(next_marker, start + len(heading))
    return text[start:end if end != -1 else len(text)]


def _readme_section():
    return _slice(_read("README.md"), README_HEADING, "\n### ")


def _production_cap():
    caps = set()
    for profile in cowork_execution_profiles.known_profiles():
        policy = cowork_execution_profiles.resolved_vertex_policy(
            profile, "builder")
        caps.add(policy["concurrency"]["max_parallel_vertices"])
    return max(caps)


class GraphDocsContractTests(unittest.TestCase):

    def test_every_reason_code_is_documented_in_readme(self):
        section = _readme_section()
        rows = {}
        for line in section.splitlines():
            match = ROW_RC.match(line)
            if not match:
                continue
            rc = int(match.group(1))
            self.assertNotIn(rc, rows, "one table row per rc: %d" % rc)
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows[rc] = set(BACKTICKED.findall(cells[-1]))
        self.assertEqual(set(rows), set(cowork_graph.REASON_RC.values()))
        for rc, documented in sorted(rows.items()):
            expected = {c for c, v in cowork_graph.REASON_RC.items()
                        if v == rc}
            self.assertEqual(
                documented, expected,
                "README rc %d row differs from REASON_RC (missing %s, "
                "stale %s)" % (rc, sorted(expected - documented),
                               sorted(documented - expected)))
        self.assertEqual(set().union(*rows.values()),
                         set(cowork_graph.REASON_CODES))

    def test_readme_agents_and_skill_describe_graph_subcommand(self):
        section = _flow(_readme_section())
        cli_skill = _flow(_read("skills", "cowork-cli", "SKILL.md"))
        orchestrate = _flow(_read("skills", "cowork-orchestrate", "SKILL.md"))
        agents = _flow(_read("AGENTS.md"))
        for op in cowork_graph_cli.OPS:
            phrase = "graph " + op
            for name, text in (("README graph section", section),
                               ("cowork-cli skill", cli_skill),
                               ("cowork-orchestrate skill", orchestrate)):
                self.assertIn(phrase, text,
                              "%s does not describe `%s`" % (name, phrase))
        self.assertIn("cowork graph", agents)
        self.assertIn("cowork_graph_result", agents)
        for name, text in (("README graph section", section),
                           ("cowork-cli skill", cli_skill)):
            for token in ("--graph-vertex", "launch_argv",
                          "cowork_graph_result"):
                self.assertIn(token, text, "%s lacks %s" % (name, token))

    def test_docs_state_join_is_not_merge_and_serial_cap(self):
        docs = (("README graph section", _flow(_readme_section())),
                ("cowork-orchestrate skill",
                 _flow(_read("skills", "cowork-orchestrate", "SKILL.md"))),
                ("AGENTS.md", _flow(_read("AGENTS.md"))))
        cap = re.compile(r"\bcap (is |of )?%d\b" % _production_cap())
        for name, text in docs:
            self.assertIn("not a merge", text,
                          "%s must say a join is not a merge" % name)
            self.assertRegex(text, cap,
                             "%s must state the production cap" % name)
        readme = _flow(_readme_section())
        for token in ("never 5", "graph_vertex_flag_conflict",
                      "profile_requires_session", "profile_team_conflict",
                      "already_published"):
            self.assertIn(token, readme, "README graph section lacks %r"
                          % token)
        self.assertIn("already_published",
                      _flow(_read("skills", "cowork-cli", "SKILL.md")))

    def test_agents_transport_lists_graph_rc_set(self):
        section = _slice(_read("AGENTS.md"), TRANSPORT_HEADING, "\n## ")
        bullets = []
        for line in section.splitlines():
            if line.startswith("- "):
                bullets.append(line)
            elif bullets and line.startswith("  "):
                bullets[-1] += " " + line.strip()
        graph_bullets = [_flow(b) for b in bullets
                         if "cowork graph" in b or "cowork_graph_result" in b]
        self.assertEqual(len(graph_bullets), 1,
                         "exactly one transport bullet describes the graph "
                         "command")
        bullet = graph_bullets[0]
        for rc in sorted({0} | set(cowork_graph.REASON_RC.values())):
            self.assertRegex(bullet, r"\b%d\b" % rc,
                             "graph transport bullet lacks rc %d" % rc)
        self.assertIn("never 5", bullet)


if __name__ == "__main__":
    unittest.main()
