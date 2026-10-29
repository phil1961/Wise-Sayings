"""Client for claude_watcher, the default route for every AI call in this app.

claude_watcher (D:\\Projects\\claude_watcher) is Phil's HTTP service that answers
POST /ask by running headless Claude Code under his subscription login, so calls
are not billed per token. Its interface is described at the top of its app.py:

    POST <WATCHER_URL>   header X-Watcher-Token
      body  {model, prompt, schema?, system?, append_system?, caller?, timeout_s?}
      200   {ok: true, raw, usage, latency_ms, id}
      4xx/5xx {ok: false, code, message}

Settings come from the environment, or from this project's .env file (never
committed): WATCHER_TOKEN (required), WATCHER_URL (optional).

The watcher is shared with other apps: one Claude run at a time and 200 calls a day
in all. So this client waits and retries a few times when it is busy, and callers
here keep their call counts small.
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://www.toughguycomputing.com/claude_watcher/ask"
CALLER = "wise-sayings"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
BUSY_WAITS = (20, 45, 90)   # seconds to wait before each retry when the watcher is busy


class WatcherUnavailable(Exception):
    """The watcher can't answer now (not configured, refused, busy, down...)."""


def _setting(name):
    if os.environ.get(name):
        return os.environ[name]
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def configured():
    return bool(_setting("WATCHER_TOKEN"))


def ask(prompt, model, schema=None, timeout_s=300, log=None):
    """Send one request. Returns (raw, usage): raw is the parsed object when a schema is given."""
    token = _setting("WATCHER_TOKEN")
    if not token:
        raise WatcherUnavailable("no WATCHER_TOKEN (set it in the environment or in this project's .env)")
    url = _setting("WATCHER_URL") or DEFAULT_URL
    if not url.startswith("https://"):
        raise WatcherUnavailable("WATCHER_URL must be https (the token would otherwise be sent in the clear)")
    body = {"model": model, "prompt": prompt, "caller": CALLER, "timeout_s": timeout_s}
    if schema:
        body["schema"] = schema
    data = json.dumps(body).encode("utf-8")
    waits = list(BUSY_WAITS)
    while True:
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"content-type": "application/json", "x-watcher-token": token})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s + 60) as r:
                out = json.loads(r.read())
            if not out.get("ok"):
                raise WatcherUnavailable(out.get("message") or "the watcher said no")
            return out.get("raw"), out.get("usage") or {}
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except ValueError:
                raise WatcherUnavailable(f"the watcher answered {e.code} without its JSON; it may be down")
            code, message = err.get("code"), err.get("message", "")
            if code == "busy" and waits:
                wait = waits.pop(0)
                (log or (lambda m: None))(f"  the watcher is busy; waiting {wait}s and trying again...")
                time.sleep(wait)
                continue
            raise WatcherUnavailable({
                "unauthorized": "the watcher refused the token",
                "daily_limit": "the watcher's daily call limit is reached (shared by every app)",
                "busy": "the watcher stayed busy with other apps' calls",
                "timeout": "the watcher's run took too long",
            }.get(code, f"{code}: {message}"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise WatcherUnavailable(f"couldn't reach the watcher ({getattr(e, 'reason', e)})")
