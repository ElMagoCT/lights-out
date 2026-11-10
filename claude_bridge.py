"""Ask Claude, cheaply. Two ways in, picked in Settings:

  cli  - the Claude Code CLI on this machine (`claude -p`), billed to the
         signed-in account. Needs a one-time `claude` + /login in a terminal.
  api  - an Anthropic API key, stored DPAPI-encrypted in config (only this
         Windows user can decrypt it).

Usage stays low by design: a daily call cap, answers cached on disk, and
every prompt is a compact pre-digested summary rather than raw data.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import time
import urllib.request

log = logging.getLogger("lightsout.claude")
NOWIN = 0x08000000
CHEAP_MODEL = "claude-haiku-4-5-20251001"
GOOD_MODEL = "claude-sonnet-5"
DEFAULTS = {"mode": "cli", "cli_path": "", "api_key_enc": "", "api_model": GOOD_MODEL,
            "cheap_model": CHEAP_MODEL,
            # Adaptive budget: the allowed rate scales between a floor and a ceiling
            # (calls/hour) with projected demand, plus a small daily reserve on top.
            "rate_adaptive": True, "rate_min_per_hour": 0.5, "rate_max_per_hour": 15.0,
            "daily_reserve": 3,
            "daily_call_cap": 120}           # daily maximum: a backstop when adaptive, a flat cap when not


# -- DPAPI --------------------------------------------------------------------------
class _BLOB(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("pb", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    inp = _BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = _BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pb, out.cb)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pb)


def encrypt_key(key: str) -> str:
    return base64.b64encode(_dpapi(key.encode(), True)).decode() if key else ""


def decrypt_key(enc: str) -> str:
    try:
        return _dpapi(base64.b64decode(enc), False).decode() if enc else ""
    except Exception:
        return ""


def find_cli():
    p = shutil.which("claude")
    if p:
        return p
    for cand in (os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
                 os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "claude.exe"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "claude-code", "claude.exe")):
        if cand and os.path.exists(cand):
            return cand
    return ""


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


class RateLimiter:
    """Adaptive call budget for the Claude bridge.

    The allowed rate scales between a floor and a ceiling (calls/hour) with
    projected demand; on top of that sits a small daily reserve. The projection
    is the greater of recent actual demand and a live 'intensity' hint from the
    power engine, which runs high during a gaming or heavy session where new
    processes need classifying. So a busy session ramps straight to the ceiling,
    while a quiet stretch that had been projected high decays back to the floor.
    A token bucket does the metering, so a fractional floor (0.5/hour = one call
    every two hours) works cleanly, and an absolute daily backstop guards against
    a runaway loop."""

    def __init__(self, cfg, path):
        self.cfg = cfg
        self.path = path
        self.intensity = 0.0
        self.st = _load_json(path, {})
        if "last" not in self.st:
            self.st = {"tokens": 1.0, "last": time.time(), "demand": [],
                       "reserve_date": "", "reserve_used": 0, "served_date": "", "served": 0}

    def _f(self, k, d):
        try:
            return float(self.cfg.get(k, d))
        except Exception:
            return d

    def set_intensity(self, x):
        try:
            self.intensity = max(0.0, min(1.0, float(x)))
        except Exception:
            self.intensity = 0.0

    def _bounds(self):
        lo = max(0.0, self._f("rate_min_per_hour", 0.5))
        hi = max(lo, self._f("rate_max_per_hour", 15.0))
        return lo, hi

    def projection(self, now):
        lo, hi = self._bounds()
        self.st["demand"] = [t for t in self.st.get("demand", []) if now - t < 3600]
        demand_rate = len(self.st["demand"])                 # wanted calls in the last hour
        d = demand_rate / hi if hi else 0.0
        return max(0.0, min(1.0, max(d, self.intensity)))

    def rate_now(self, now=None):
        now = time.time() if now is None else now
        lo, hi = self._bounds()
        return lo + (hi - lo) * self.projection(now)

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.st, f)
        except Exception:
            log.exception("rate state save")

    def check(self, now=None, record=True):
        """(allowed, reason, rate_now). record=False peeks without spending."""
        now = time.time() if now is None else now
        today = dt.date.today().isoformat()
        if self.st.get("reserve_date") != today:
            self.st["reserve_date"], self.st["reserve_used"] = today, 0
        if self.st.get("served_date") != today:
            self.st["served_date"], self.st["served"] = today, 0
        r = self.rate_now(now)                               # prunes demand too
        cap = max(1.0, r)                                    # hold at least one token so the floor can fire
        elapsed = max(0.0, now - self.st.get("last", now))
        self.st["tokens"] = min(cap, self.st.get("tokens", cap) + elapsed / 3600.0 * r)
        self.st["last"] = now
        if record:
            self.st["demand"].append(now)
        hard = int(self._f("daily_call_cap", 120))
        reserve = int(self._f("daily_reserve", 3))
        reserve_left = max(0, reserve - int(self.st.get("reserve_used", 0)))
        allowed, reason = False, ""
        if int(self.st.get("served", 0)) >= hard:
            reason = f"Claude daily backstop reached ({hard} calls); raise it in Settings"
        elif self.st.get("tokens", 0.0) >= 1.0:
            allowed = True
            if record:
                self.st["tokens"] -= 1.0
        elif reserve_left > 0:
            allowed = True
            if record:
                self.st["reserve_used"] = int(self.st.get("reserve_used", 0)) + 1
        else:
            mins = (1.0 - self.st.get("tokens", 0.0)) / r * 60 if r > 0 else 9999
            reason = f"Claude rate limit: about {r:.1f} calls/hour right now and the daily reserve is spent; next call in ~{mins:.0f} min"
        if allowed and record:
            self.st["served"] = int(self.st.get("served", 0)) + 1
        if record:
            self._save()
        return allowed, reason, r


class Claude:
    def __init__(self, cfg, data_dir):
        self.cfg = cfg                                   # the "claude" dict (live)
        self.usage_path = os.path.join(data_dir, "claude_usage.json")
        self.cache_path = os.path.join(data_dir, "claude_cache.json")
        self.usage = self._load(self.usage_path, {})
        self.cache = self._load(self.cache_path, {})
        self.limiter = RateLimiter(cfg, os.path.join(data_dir, "claude_rate.json"))
        self.last_error = ""

    def set_load(self, intensity):
        """0..1 hint from the power engine; raises the projected rate while busy."""
        self.limiter.set_intensity(intensity)

    def rate_now(self):
        return self.limiter.rate_now()

    @staticmethod
    def _load(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _save(self, path, obj):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f)
        except Exception:
            log.exception("save %s", path)

    # -- status --------------------------------------------------------------------
    def status(self):
        mode = self.cfg.get("mode", "cli")
        if mode == "api":
            return ("API key set" if decrypt_key(self.cfg.get("api_key_enc", "")) else "API mode: no key saved")
        cli = self.cfg.get("cli_path") or find_cli()
        if not cli:
            return "CLI mode: `claude` not found (install Claude Code, or switch to an API key)"
        return f"CLI mode: {os.path.basename(cli)}" + (f" ({self.last_error})" if self.last_error else "")

    def budget(self):
        """One line on the current budget, for Settings."""
        if not self.cfg.get("rate_adaptive", True):
            return f"Fixed cap: {self.calls_today()}/{int(self.cfg.get('daily_call_cap', 20))} calls today"
        lo = self.cfg.get("rate_min_per_hour", 0.5)
        hi = self.cfg.get("rate_max_per_hour", 15.0)
        res = max(0, int(self.cfg.get("daily_reserve", 3)) - int(self.limiter.st.get("reserve_used", 0)))
        return (f"Adaptive: ~{self.rate_now():.1f} calls/hr now (scales {lo:g}-{hi:g}/hr with load), "
                f"{res} reserve left today")

    def calls_today(self):
        return self.usage.get(dt.date.today().isoformat(), {}).get("calls", 0)

    def _record(self, in_tok, out_tok, cost):
        day = dt.date.today().isoformat()
        u = self.usage.setdefault(day, {"calls": 0, "in": 0, "out": 0, "usd": 0.0})
        u["calls"] += 1
        u["in"] += in_tok or 0
        u["out"] += out_tok or 0
        u["usd"] = round(u["usd"] + (cost or 0), 4)
        for k in list(self.usage):
            if k < (dt.date.today() - dt.timedelta(days=30)).isoformat():
                del self.usage[k]
        self._save(self.usage_path, self.usage)

    # -- asking --------------------------------------------------------------------
    def ask(self, prompt, cheap=False, max_tokens=900, cache_key=None):
        """Returns (text, error). Cached answers cost nothing."""
        if cache_key and cache_key in self.cache:
            return self.cache[cache_key], ""
        if self.cfg.get("rate_adaptive", True):
            ok, why, _ = self.limiter.check()
            if not ok:
                return "", why
        elif self.calls_today() >= int(self.cfg.get("daily_call_cap", 20)):
            return "", "daily Claude call cap reached (raise it in Settings)"
        mode = self.cfg.get("mode", "cli")
        text, err = (self._api(prompt, cheap, max_tokens) if mode == "api" else self._cli(prompt, cheap))
        self.last_error = err
        if text and cache_key:
            self.cache[cache_key] = text
            self._save(self.cache_path, self.cache)
        return text, err

    def _cli(self, prompt, cheap):
        cli = self.cfg.get("cli_path") or find_cli()
        if not cli:
            return "", "claude CLI not found"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
        args = [cli, "-p", "--output-format", "json", "--max-turns", "1"]
        if cheap:
            args += ["--model", "haiku"]
        try:
            r = subprocess.run(args, input=prompt, capture_output=True, text=True, timeout=180,
                               creationflags=NOWIN, env=env, encoding="utf-8", errors="replace")
        except Exception as e:
            return "", f"claude CLI failed to run: {e}"
        # The CLI prints stray warnings (e.g. "Client.listTools() ...") around the
        # JSON, so scan for the result object instead of assuming it is the last line.
        d = None
        for line in r.stdout.splitlines():
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                cand = json.loads(line)
            except ValueError:
                continue
            if isinstance(cand, dict) and ("result" in cand or "is_error" in cand or cand.get("type") == "result"):
                d = cand
                break
        if d is None:
            return "", f"claude CLI gave no answer: {(r.stderr or r.stdout)[:200].strip()}"
        if d.get("is_error"):
            msg = str(d.get("result", ""))[:200]
            if "authenticate" in msg.lower() or "oauth" in msg.lower() or "login" in msg.lower():
                msg = "not signed in: open a terminal, run `claude`, then `/login`"
            return "", msg
        u = d.get("usage", {})
        self._record(u.get("input_tokens"), u.get("output_tokens"), d.get("total_cost_usd"))
        return str(d.get("result", "")).strip(), ""

    def _api(self, prompt, cheap, max_tokens):
        key = decrypt_key(self.cfg.get("api_key_enc", ""))
        if not key:
            return "", "no API key saved"
        model = self.cfg.get("cheap_model", CHEAP_MODEL) if cheap else self.cfg.get("api_model", GOOD_MODEL)
        body = json.dumps({"model": model, "max_tokens": max_tokens,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("error", {}).get("message", str(e))
            except Exception:
                msg = str(e)
            return "", f"API error: {msg[:200]}"
        except Exception as e:
            return "", f"API request failed: {e}"
        u = d.get("usage", {})
        self._record(u.get("input_tokens"), u.get("output_tokens"), None)
        return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text").strip(), ""

    # -- the two things the app asks ---------------------------------------------------
    def classify_processes(self, names):
        """names -> {name: 'throttle' | 'protect'} for background processes we do
        not know. One cheap call for the whole batch, cached per name."""
        todo = [n for n in names if f"proc:{n}" not in self.cache]
        if todo:
            prompt = ("For each Windows process name below, answer whether it is SAFE to run in Windows "
                      "'Efficiency mode' (EcoQoS + lower priority) while the user plays a game or does heavy "
                      "work in another app. Answer 'protect' only for audio/video capture, input, display, "
                      "security, or anything whose slowdown the user would notice immediately. Reply with "
                      "one line per name exactly as `name: throttle` or `name: protect`, nothing else.\n\n"
                      + "\n".join(todo))
            text, err = self.ask(prompt, cheap=True, max_tokens=400)
            if text:
                for line in text.splitlines():
                    if ":" in line:
                        n, v = line.split(":", 1)
                        n, v = n.strip().lower(), v.strip().lower()
                        if n in todo and v in ("throttle", "protect"):
                            self.cache[f"proc:{n}"] = v
                self._save(self.cache_path, self.cache)
        return {n: self.cache.get(f"proc:{n}", "throttle") for n in names}

    def security_narrative(self, findings_json):
        prompt = ("You are a pragmatic Windows security reviewer for a single personal laptop (not a company). "
                  "Below is a JSON summary produced by local checks. Write a short report in Markdown: a one-line "
                  "verdict, then the findings ordered by real-world risk, each with WHY it matters in one sentence "
                  "and the exact fix (Settings path or command). Skip anything that is fine. Keep it under 450 "
                  "words. Do not invent facts not in the data.\n\n" + findings_json)
        return self.ask(prompt, cheap=False, max_tokens=1200)
