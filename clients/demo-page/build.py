#!/usr/bin/env python3
"""Build the router-free CareerOS demo page.

Assembles site.html + app.js + the captured API fixtures into two files:

  careeros.html       a complete standalone document, for file:// or any host
  artifact.html       the same page as a bare body, for the Artifact tool,
                      which supplies its own doctype/head/body wrapper

The page has no router and no History API use, so it runs from file://, from
any subpath, and inside a sandboxed frame.

Usage: python3 build.py [output-dir]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = HERE / "site.html"
APP = HERE / "app.js"
TAXONOMY = HERE.parents[1] / "careeros" / "config" / "taxonomy" / "domains.yaml"
FIXTURES = HERE.parent / "app" / "src" / "demoFixtures.json"
FIXTURE_SLOT = "/*__FIXTURES__*/"
APP_SLOT = "/*__APP__*/"

# The recorded agent transcript. It lives in the Expo client as TypeScript
# (clients/app/src/demo.ts), which this page cannot import, so it is mirrored
# here verbatim.
AGENT_ANSWER = {
    "answer": (
        "[Recorded demo response \u2014 the agent is not running here. It needs model access "
        "on the server (ANTHROPIC_API_KEY).]\n\n"
        "Here is what a real run looks like for \u201cWhat should I apply for today?\u201d:\n\n"
        "Start with Senior Network Security Engineer at Cobalt Bank. Its final application "
        "date is today, so it is pinned to the top regardless of score \u2014 and it happens to "
        "be your strongest match at 88.8, with 6 of the 8 required skills directly "
        "evidenced (BGP/OSPF, Cisco ISE, Zero Trust segmentation, SD-WAN).\n\n"
        "The posting says it will sponsor and transfer H1B visas, so the verdict is "
        "\"potentially compatible\" rather than unknown \u2014 but a transfer petition is still "
        "required, and that is the employer's statement, not a guarantee. Verify it on the "
        "screening call.\n\n"
        "Second: Network Engineer - Data Centre at Sahyadri Technologies (97.1 match, no "
        "sponsorship needed in India) \u2014 but 220 applicants, so it is a weaker bet than the "
        "score suggests.\n\n"
        "I would skip the SAP Security contract today. Your evidence supports access "
        "governance and SoD work, but nothing SAP-specific, so the tailored resume leaves "
        "the Firefighter requirement unclaimed. That is honest, and it will not survive a "
        "technical screen."
    ),
    "turns": 4,
    "stop_reason": "end_turn",
    "tool_calls": [
        {"turn": 1, "name": "get_profile", "arguments": {}, "mutating": False, "is_error": False},
        {"turn": 1, "name": "list_jobs", "arguments": {"limit": 10}, "mutating": False, "is_error": False},
        {"turn": 2, "name": "get_job", "arguments": {"job_id": 1}, "mutating": False, "is_error": False},
        {"turn": 2, "name": "get_match_detail", "arguments": {"job_id": 1}, "mutating": False, "is_error": False},
        {"turn": 3, "name": "get_match_detail", "arguments": {"job_id": 3}, "mutating": False, "is_error": False},
        {"turn": 3, "name": "get_analytics", "arguments": {}, "mutating": False, "is_error": False},
    ],
    "mutations": [],
    "input_tokens": 18432,
    "output_tokens": 742,
    "run_id": None,
    "ok": True,
    "error": None,
}


WRAPPER = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>html{color-scheme:light dark}body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>
{body}
</html>
"""


def seed_domains() -> list[dict]:
    """The seeded taxonomy, read from the real config so the site cannot drift."""
    import yaml

    doc = yaml.safe_load(TAXONOMY.read_text(encoding="utf-8"))
    return [{"id": d["id"], "label": d["label"]} for d in doc["domains"]]


def script_safe(blob: str) -> str:
    """A JSON blob lives inside <script>, where these two would end it early."""
    return blob.replace("</", "<\\/").replace("<!--", "<\\!--")


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    out_dir.mkdir(parents=True, exist_ok=True)

    site = SITE.read_text(encoding="utf-8")
    for slot in (FIXTURE_SLOT, APP_SLOT):
        if slot not in site:
            print(f"error: {slot} not found in {SITE}", file=sys.stderr)
            return 1

    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    fixtures["agentAnswer"] = AGENT_ANSWER
    fixtures["seedDomains"] = seed_domains()

    blob = script_safe(json.dumps(fixtures, ensure_ascii=False, separators=(",", ":")))
    page = site.replace(APP_SLOT, APP.read_text(encoding="utf-8")).replace(FIXTURE_SLOT, blob)

    # Bare body, for a host that supplies its own doctype/head/body.
    artifact = out_dir / "artifact.html"
    artifact.write_text(page, encoding="utf-8")

    # Complete document: <title> and the font <link> move up into <head>.
    split = page.index("<style>")
    head, body = page[:split], page[split:]
    standalone = out_dir / "careeros.html"
    standalone.write_text(
        WRAPPER.replace("{body}", f"{head}</head>\n<body>\n{body}\n</body>"),
        encoding="utf-8",
    )

    print(f"{artifact}    {artifact.stat().st_size / 1024:.0f} KB (artifact body)")
    print(f"{standalone}  {standalone.stat().st_size / 1024:.0f} KB (standalone document)")
    print(f"{len(fixtures)} fixture groups, {len(fixtures['seedDomains'])} seed domains")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
