"""Optional v2 matcher: ask Claude to map a free-text situation onto the tag vocabulary.

Use with `consult --smart` (or SAYINGS_MATCHER=compendium.llm_matcher:match).
The call goes through claude_watcher (compendium/watcher.py) unless
SAYINGS_TRANSPORT=api, which needs `pip install anthropic` and an ANTHROPIC_API_KEY.
If the call can't be made, it warns and falls back to the offline keyword
matcher, so consult always works.

Claude only picks tags and weights; ranking still happens locally against the
accepted sayings, so it can never invent or return a saying that isn't in the DB.
"""

import json
import os
import sys

from . import consult, watcher

MODEL = os.environ.get("SAYINGS_MODEL", "claude-opus-5-5")
TRANSPORT = os.environ.get("SAYINGS_TRANSPORT", "watcher")
KIND_FACTOR = {"situation": 1.0, "subject": 2 / 3}  # same 3:2 balance as the keyword matcher

SYSTEM = """You help a decision-aid tool pick lenses for a person's situation.
The tool holds a curated set of sayings (proverbs, maxims, apophthegms), each tagged
from a fixed vocabulary. Your job is only to choose which tags fit the situation.

Read the situation for what is really going on, not just its words. For example,
"my cofounder keeps meeting with our rival" is about possible betrayal even though
the word never appears. Pick the tags that describe the situation's core dynamics,
usually 2 to 6. Rate each one:
  3 = central to the situation
  2 = clearly relevant
  1 = a useful secondary angle
Leave out tags that only match a surface word. Use only tag ids from the list."""


def _tag_list(tags):
    lines = []
    for t in sorted(tags, key=lambda t: (t["kind"], t["name"])):
        hint = f" (e.g. {', '.join(t['aliases'][:6])})" if t["aliases"] else ""
        lines.append(f"- {t['kind']}:{t['name']}{hint}")
    return "\n".join(lines)


def _schema(tag_ids):
    return {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": tag_ids},
                        "relevance": {"type": "integer", "enum": [1, 2, 3]},
                    },
                    "required": ["id", "relevance"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tags"],
        "additionalProperties": False,
    }


def _fallback(reason, situation, tags):
    print(f"Note: smart matching unavailable ({reason}); using keyword matching.", file=sys.stderr)
    return consult.keyword_matcher(situation, tags)


def weights_from_reply(data, tags):
    """Turn Claude's {"tags": [{"id": "kind:name", "relevance": n}]} into {tag_id: weight}."""
    by_id = {f"{t['kind']}:{t['name']}": t for t in tags}
    weights = {}
    for item in data.get("tags", []):
        tag = by_id.get(item.get("id"))
        rel = item.get("relevance")
        if tag and rel in (1, 2, 3):
            weights[tag["id"]] = max(weights.get(tag["id"], 0), rel * KIND_FACTOR[tag["kind"]])
    return weights


def match(situation, tags, client=None):
    if client is None and TRANSPORT != "api":
        tag_ids = [f"{t['kind']}:{t['name']}" for t in tags]
        prompt = f"{SYSTEM}\n\nTag vocabulary:\n{_tag_list(tags)}\n\nSituation:\n{situation}"
        try:
            raw, _ = watcher.ask(prompt, MODEL, schema=_schema(tag_ids))
        except watcher.WatcherUnavailable as e:
            return _fallback(str(e), situation, tags)
        if not isinstance(raw, dict):
            return _fallback("unreadable reply", situation, tags)
        return weights_from_reply(raw, tags) or _fallback("no tags chosen", situation, tags)
    try:
        import anthropic
    except ImportError:
        return _fallback("the anthropic package is not installed: pip install anthropic", situation, tags)

    if client is None:
        try:
            client = anthropic.Anthropic()
        except anthropic.AnthropicError as e:  # e.g. no credentials configured
            return _fallback(str(e), situation, tags)

    tag_ids = [f"{t['kind']}:{t['name']}" for t in tags]
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # if the model declines, the API retries on a recommended fallback model
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _schema(tag_ids)}},
            system=SYSTEM,
            messages=[{"role": "user", "content":
                       f"Tag vocabulary:\n{_tag_list(tags)}\n\nSituation:\n{situation}"}],
        )
    except anthropic.AuthenticationError:
        return _fallback("API key missing or invalid; set ANTHROPIC_API_KEY", situation, tags)
    except anthropic.RateLimitError:
        return _fallback("rate limited", situation, tags)
    except anthropic.APIStatusError as e:
        return _fallback(f"API error {e.status_code}: {e.message}", situation, tags)
    except anthropic.APIConnectionError:
        return _fallback("no network connection", situation, tags)

    if response.stop_reason in ("refusal", "max_tokens"):
        return _fallback(f"model stopped with {response.stop_reason}", situation, tags)
    text = next((b.text for b in response.content if b.type == "text"), None)
    try:
        weights = weights_from_reply(json.loads(text), tags)
    except (TypeError, ValueError):
        return _fallback("unreadable reply", situation, tags)
    return weights or _fallback("no tags chosen", situation, tags)
