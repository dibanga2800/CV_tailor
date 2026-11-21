"""Model engines with automatic fallback.

Each engine turns (system prompt, user prompt) into text. The app asks the primary engine first
and, if it fails or returns something that isn't JSON, moves to the next one in the fallback
order. Every draft records which engine wrote it.

  claude    Claude Code CLI (`claude -p`)        your Claude subscription login
  codex     OpenAI Codex CLI (`codex exec`)      your ChatGPT plan login ("Sign in with ChatGPT")
  grok      Grok Build CLI (`grok -p`)           your SuperGrok / X Premium+ login
  cursor    Cursor CLI (`cursor-agent -p`)       your Cursor subscription login
  deepseek  DeepSeek API                         DEEPSEEK_API_KEY (pay as you go)
  openai    OpenAI API                           OPENAI_API_KEY (separate from ChatGPT plans)
  anthropic Anthropic API                        ANTHROPIC_API_KEY
  mock      canned draft for UI testing

Choice is stored in documents/postings/engine.json (gitignored): {"primary": ..., "order": [...]}.
Env vars still work: CV_TAILOR_ENGINE, CV_TAILOR_FALLBACKS="codex,deepseek".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextvars import ContextVar
from pathlib import Path

from store import DOCS, WEBUI, load_json, save_json

ENGINE_FILE = DOCS / "postings" / "engine.json"
DEFAULT_TIMEOUT = 240  # a full CV + letter can take 2-4 minutes on the CLIs
DEFAULT_REQUEST_TIMEOUT = 540  # room to fall back to a second engine after a slow first one
RETRY_AFTER = 300
SPEND_LIMIT_RETRY_AFTER = 3600
DRAFT_SUCCESS_TTL = 3600
MIN_ATTEMPT_TIME = 20
_deadline = ContextVar("provider_request_deadline", default=None)
_state_lock = threading.Lock()
_retry_at: dict[str, float] = {}
_verified: set[str] = set()
_draft_success: dict[str, float] = {}
_timings: dict[str, dict[str, dict]] = {}
LABELS = {"claude": "Claude Code", "codex": "ChatGPT (Codex CLI)", "grok": "Grok Build CLI", "cursor": "Cursor CLI",
          "deepseek": "DeepSeek API", "openai": "OpenAI API", "anthropic": "Anthropic API", "mock": "Mock engine"}
DEFAULT_ORDER = ["claude", "codex", "grok", "deepseek", "openai", "anthropic", "cursor"]


class EngineError(RuntimeError):
    pass


class EngineTimeout(EngineError):
    pass


# ----------------------------------------------------------------------------- discovery

def _which(*names: str) -> str | None:
    for name in names:
        candidates = (name + ".exe", name + ".cmd", name + ".bat") if os.name == "nt" else (name,)
        for candidate in candidates:
            path = shutil.which(candidate)
            if path:
                return path
    home = Path.home()
    extra = [home / ".local" / "bin", home / "AppData" / "Roaming" / "npm",
             home / "AppData" / "Local" / "cursor-agent", home / ".cursor" / "bin",
             home / ".grok" / "bin", home / "AppData" / "Local" / "grok", home / "AppData" / "Local" / "xai" / "bin"]
    for d in extra:
        for n in names:
            for ext in ((".exe", ".cmd", ".bat") if os.name == "nt" else ("",)):
                c = d / (n + ext)
                if c.is_file() and os.access(c, os.X_OK):
                    return str(c)
    return None


def _cursor_exe() -> str | None:
    """Cursor's CLI is `cursor-agent` (newer builds also add `agent`). Grok Build installs an `agent`
    command too, so only accept a bare `agent` that lives in a Cursor folder."""
    exe = _which("cursor-agent")
    if exe:
        return exe
    exe = _which("agent")
    return exe if exe and "cursor" in exe.lower() else None


def _cursor_command() -> list[str]:
    exe = _cursor_exe()
    if not exe:
        raise EngineError("Cursor Agent CLI not found")
    launcher = Path(exe)
    versions = launcher.parent / "versions"
    if os.name == "nt" and launcher.suffix.lower() == ".cmd" and versions.is_dir():
        # Use the installed runtime directly so Python owns the Node process and its timeout.
        for version in sorted(versions.iterdir(), reverse=True):
            node = version / "node.exe"
            script = version / "index.js"
            if not version.name.endswith((".new", ".previous")) and node.is_file() and script.is_file():
                return [str(node), str(script)]
    return [exe]


def available() -> dict:
    status = configured_engines()
    cfg = config()
    status["mock"] = cfg["primary"] == "mock"
    cooling = cooling_engines()
    with _state_lock:
        verified = _verified.copy()
        timings = {name: modes.copy() for name, modes in _timings.items()}
    order = chain(cfg, status, cooling, prefer_drafts=True)
    return {"engines": [{"id": k, "label": LABELS[k], "ready": v,
                         "retrying_later": k in cooling, "verified": v and k in verified,
                         "timings": timings.get(k, {})}
                        for k, v in status.items() if k != "mock" or v],
            **cfg, "selected": order[0] if order else "none",
            "all_paused": bool(order) and all(name in cooling for name in order),
            # kept for older front-end code
            "api": status["anthropic"], "cli": status["claude"]}


def config() -> dict:
    cfg = load_json(ENGINE_FILE, None) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    env_primary = os.environ.get("CV_TAILOR_ENGINE", "").strip().lower()
    primary = env_primary if env_primary in LABELS else cfg.get("primary")
    if not isinstance(primary, str) or primary not in LABELS:
        primary = "auto"
    order = cfg.get("order")
    if not isinstance(order, list) or not order:
        order = [value.strip() for value in os.environ.get("CV_TAILOR_FALLBACKS", "").split(",")]
    preferred = [name for name in order if name in DEFAULT_ORDER]
    return {"primary": primary, "order": list(dict.fromkeys([*preferred, *DEFAULT_ORDER]))}


def save_config(primary: str, order: list[str]) -> dict:
    if primary not in LABELS and primary != "auto":
        raise ValueError("unknown engine")
    with _state_lock:
        save_json(ENGINE_FILE, {"primary": primary,
                                "order": [o for o in dict.fromkeys(order) if o in DEFAULT_ORDER]})
    return available()


def reset_health(name: str) -> None:
    with _state_lock:
        _retry_at.pop(name, None)
        _verified.discard(name)
        _draft_success.pop(name, None)
        _timings.pop(name, None)


def record_draft_success(name: str) -> None:
    if name != "mock":
        with _state_lock:
            _draft_success[name] = time.monotonic()


def configured_engines() -> dict[str, bool]:
    return {
        "claude": bool(_which("claude")), "codex": bool(_which("codex")),
        "grok": bool(_which("grok")),
        "cursor": bool(_cursor_exe()), "deepseek": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")), "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY"))}


def cooling_engines() -> set[str]:
    now = time.monotonic()
    with _state_lock:
        return {name for name, retry_at in _retry_at.items() if retry_at > now}


def chain(cfg=None, status=None, cooling=None, *, prefer_drafts: bool = False) -> list[str]:
    """Prefer configured engines that have not failed recently."""
    cfg = config() if cfg is None else cfg
    if cfg["primary"] == "mock":
        return ["mock"]
    status = configured_engines() if status is None else status
    cooling = cooling_engines() if cooling is None else cooling
    order = [name for name in dict.fromkeys([cfg["primary"], *cfg["order"], *DEFAULT_ORDER])
             if name != "mock" and status.get(name)]
    active = [name for name in order if name not in cooling]
    if prefer_drafts and cfg["primary"] == "auto":
        recent = time.monotonic() - DRAFT_SUCCESS_TTL
        with _state_lock:
            proven = {name for name, timestamp in _draft_success.items() if timestamp >= recent}
        active.sort(key=lambda name: name not in proven)
    return active or order


def selected() -> str:
    c = chain()
    return c[0] if c else "none"


# ----------------------------------------------------------------------------- engines

def _duration(setting: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(setting, default)))
    except ValueError:
        return default


def _timeout() -> int:
    provider_timeout = _duration("CV_TAILOR_ENGINE_TIMEOUT", DEFAULT_TIMEOUT)
    deadline = _deadline.get()
    if deadline is None:
        return provider_timeout
    return max(1, min(provider_timeout, int(deadline - time.monotonic())))


def _run(cmd: list[str], prompt: str | None, cwd: str) -> subprocess.CompletedProcess:
    timeout = _timeout()
    if not cmd[0]:
        raise EngineError("CLI executable not found")
    try:
        # File handles let a timed-out wrapper return even if its child still holds stdout open.
        with tempfile.TemporaryFile(mode="w+b") as stdin, tempfile.TemporaryFile(mode="w+b") as stdout, \
                tempfile.TemporaryFile(mode="w+b") as stderr:
            stdin.write(prompt.encode("utf-8") if prompt is not None else b"")
            stdin.seek(0)
            with subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr, cwd=cwd) as process:
                try:
                    returncode = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
                        try:
                            subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                    process.kill()
                    raise
            stdout.seek(0)
            stderr.seek(0)
            return subprocess.CompletedProcess(cmd, returncode, stdout.read().decode("utf-8", errors="replace"),
                                               stderr.read().decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired as e:
        raise EngineTimeout(f"{Path(cmd[0]).name} timed out after {timeout}s") from e
    except OSError as e:
        raise EngineError(f"couldn't start {Path(cmd[0]).name}: {e}") from e


def _claude(system: str, user: str) -> str:
    exe = _which("claude")
    cmd = [exe, "-p", "--output-format", "json", "--no-session-persistence"]
    if os.environ.get("CV_TAILOR_MODEL"):
        cmd += ["--model", os.environ["CV_TAILOR_MODEL"]]
    # Run from an empty folder so the repo's CLAUDE.md and /apply workflow are not loaded.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as cwd:
        proc = _run(cmd, system + "\n\n=====\n\n" + user, cwd)
    try:
        env = json.loads(proc.stdout)
    except ValueError:
        env = None
    if isinstance(env, dict):
        if env.get("is_error") or proc.returncode != 0:
            raise EngineError(f"Claude Code: {str(env.get('result', ''))[:400]}")
        return env.get("result", "")
    if proc.returncode != 0:
        raise EngineError(f"Claude Code: {(proc.stderr or proc.stdout).strip()[-400:]}")
    return proc.stdout


def _codex(system: str, user: str) -> str:
    """ChatGPT plan via Codex CLI. Read-only sandbox (the default); prompt on stdin."""
    exe = _which("codex")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as cwd:
        out = Path(cwd) / "answer.txt"
        cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral", "-o", str(out)]
        if os.environ.get("CODEX_MODEL"):
            cmd += ["-m", os.environ["CODEX_MODEL"]]
        cmd.append("-")
        proc = _run(cmd, system + "\n\n=====\n\n" + user + "\n\nReply with the JSON only. Do not run commands.", cwd)
        text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
    if proc.returncode != 0 or not text.strip():
        msg = (proc.stderr or proc.stdout).strip()[-400:]
        if "login" in msg.lower() or "auth" in msg.lower():
            msg += "  Fix: run `codex login` and choose Sign in with ChatGPT."
        raise EngineError(f"Codex: {msg or 'no answer'}")
    return text


def _cursor(system: str, user: str) -> str:
    """Cursor CLI in ask mode. The long prompt goes in a file the agent reads, which avoids
    Windows' command-line length limit."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as cwd:
        (Path(cwd) / "task.md").write_text(system + "\n\n=====\n\n" + user, encoding="utf-8")
        # --trust: the folder is a fresh empty temp dir holding only task.md, and ask mode
        # can't edit files or run commands, so trusting it is safe and avoids the prompt.
        cmd = [*_cursor_command(), "--print", "--trust", "--output-format", "text", "--mode", "ask"]
        if os.environ.get("CURSOR_MODEL"):
            cmd += ["--model", os.environ["CURSOR_MODEL"]]
        cmd.append("Read the file task.md in the current folder and follow it exactly. "
                   "Reply with only the JSON object it asks for, no commentary.")
        proc = _run(cmd, None, cwd)
    if proc.returncode != 0 or not proc.stdout.strip():
        msg = (proc.stderr or proc.stdout).strip()[-400:]
        if "login" in msg.lower() or "auth" in msg.lower():
            msg += "  Fix: run `cursor-agent login`."
        raise EngineError(f"Cursor: {msg or 'no answer'}")
    return proc.stdout


def _grok(system: str, user: str) -> str:
    """Use Grok Build as a JSON writer without coding-agent tools."""
    exe = _which("grok")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as cwd:
        task = Path(cwd) / "task.md"
        task.write_text(system + "\n\n=====\n\n" + user + "\n\nReply with only the JSON object asked for, "
                        "no commentary and no tool use.", encoding="utf-8")
        cmd = [exe, "--prompt-file", str(task), "--output-format", "json", "--cwd", cwd,
               "--no-subagents", "--no-plan", "--disable-web-search", "--no-auto-update", "--max-turns", "3",
               "--model", os.environ.get("GROK_MODEL", "grok-4.7-build-fast"),
               "--reasoning-effort", os.environ.get("GROK_REASONING_EFFORT", "low"), "--tools", "",
               "--system-prompt-override",
               "You are a CV drafting service. Apply the user's writing instructions and return only the requested JSON. "
               "Do not use tools."]
        proc = _run(cmd, None, cwd)
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        msg = (proc.stderr or proc.stdout).strip()[-400:]
        low = msg.lower()
        if any(w in low for w in ("login", "auth", "unauthor", "subscription", "credential")):
            msg += "  Fix: run `grok login` (needs SuperGrok or X Premium+)."
        raise EngineError(f"Grok: {msg or 'no answer'}")
    try:  # --output-format json gives {"text": ..., "stopReason": ..., ...}
        data = json.loads(out[out.find("{"):])
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            return data["text"]
    except ValueError:
        pass
    return out


def _openai_compatible(base: str, key_env: str, model: str, system: str, user: str, label: str,
                       token_limit_field: str) -> str:
    key = os.environ.get(key_env)
    if not key:
        raise EngineError(f"{key_env} is missing")
    body = {"model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            token_limit_field: 16000,
            "response_format": {"type": "json_object"}}
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 method="POST", headers={"Authorization": f"Bearer {key}",
                                                         "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        hint = {401: "key rejected", 402: "account has no credit", 429: "rate limited"}.get(e.code, "")
        raise EngineError(f"{label} {e.code} {hint}: {detail}") from e
    except urllib.error.URLError as e:
        raise EngineError(f"{label}: network error {e.reason}") from e
    return data["choices"][0]["message"]["content"] or ""


def _deepseek(system: str, user: str) -> str:
    return _openai_compatible("https://api.deepseek.com", "DEEPSEEK_API_KEY",
                              os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"), system, user, "DeepSeek",
                              "max_tokens")


def _openai(system: str, user: str) -> str:
    return _openai_compatible("https://api.openai.com/v1", "OPENAI_API_KEY",
                              os.environ.get("OPENAI_MODEL", "gpt-5-mini"), system, user, "OpenAI",
                              "max_completion_tokens")


def _anthropic(system: str, user: str) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise EngineError("pip install anthropic") from e
    client = anthropic.Anthropic(timeout=_timeout(), max_retries=0)
    chunks = []
    with client.messages.stream(model=os.environ.get("CV_TAILOR_MODEL", "claude-sonnet-5-5"), max_tokens=16000,
                                system=system, messages=[{"role": "user", "content": user}]) as stream:
        for text in stream.text_stream:
            chunks.append(text)
    return "".join(chunks)


def _mock(system: str, user: str) -> str:
    return (WEBUI / "fixtures" / "mock_draft.json").read_text(encoding="utf-8")


RUNNERS = {"claude": _claude, "codex": _codex, "grok": _grok, "cursor": _cursor, "deepseek": _deepseek,
           "openai": _openai, "anthropic": _anthropic, "mock": _mock}


# ----------------------------------------------------------------------------- public API

def _complete(system, user, decode, mock=None, only=None, on_try=None, on_failure=None,
              prefer_drafts=False, mode=None):
    order = [only] if only else chain(prefer_drafts=prefer_drafts)
    if not order:
        raise EngineError("No engine is set up. Log in to Claude Code, Codex (ChatGPT) or Grok, "
                          "or add a DeepSeek / OpenAI / Anthropic key.")
    if not only:
        cooling = cooling_engines()
        cooling_names = [LABELS[name] for name in order if name in cooling]
        order = [name for name in order if name not in cooling]
        if not order:
            raise EngineError("Recent failures paused all configured providers. "
                              "Open AI providers to retest: " + ", ".join(cooling_names) + ".")
    provider_timeout = _duration("CV_TAILOR_ENGINE_TIMEOUT", DEFAULT_TIMEOUT)
    budget = max(provider_timeout, _duration("CV_TAILOR_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT))
    short_attempt = mode in {"rewrite", "test"}
    if short_attempt:
        provider_timeout, budget = min(provider_timeout, 30), min(budget, 95)
    deadline = time.monotonic() + budget if len(order) > 1 else None
    failures = []
    for name in order:
        t0 = time.monotonic()
        if deadline is not None and deadline - t0 < MIN_ATTEMPT_TIME:
            failures.append("The request time limit left too little time for another provider.")
            break
        if on_try:
            on_try(name)
        remaining_limited = deadline is not None and deadline - t0 < provider_timeout
        attempt_deadline = deadline if remaining_limited else (t0 + provider_timeout if short_attempt else None)
        token = _deadline.set(attempt_deadline)
        ok = False
        try:
            raw = mock() if (name == "mock" and mock is not None) else RUNNERS[name](system, user)
            if not isinstance(raw, str) or not raw.strip():
                raise EngineError("the model returned an empty response")
            result = decode(raw)
        except Exception as e:  # move on to the next engine
            msg = str(e)
            short_timeout = (remaining_limited or mode == "rewrite") and isinstance(e, (EngineTimeout, TimeoutError))
            if name != "mock" and not short_timeout:
                retry_after = SPEND_LIMIT_RETRY_AFTER if "monthly spend limit" in msg.lower() else RETRY_AFTER
                with _state_lock:
                    _verified.discard(name)
                    _draft_success.pop(name, None)
                    _retry_at[name] = time.monotonic() + retry_after
            for p in ("Claude Code: ", "Codex: ", "Grok: ", "Cursor: "):
                msg = msg.removeprefix(p)
            elapsed = round(time.monotonic() - t0)
            failures.append(f"{LABELS.get(name, name)}: {msg[:300]} ({elapsed}s)")
            if on_failure:
                on_failure(name, msg, elapsed)
        else:
            ok = True
            with _state_lock:
                _retry_at.pop(name, None)
                _verified.add(name)
            return result, name, failures
        finally:
            _deadline.reset(token)
            if mode and name != "mock":
                with _state_lock:
                    _timings.setdefault(name, {})[mode] = {"ok": ok, "seconds": round(time.monotonic() - t0, 1)}
    prefix = "" if only else "Every configured provider failed. "
    raise EngineError(prefix + " | ".join(failures))


def complete_json(system: str, user: str, mock=None, only: str | None = None, on_try=None,
                  expected: dict[str, type] | None = None, on_failure=None,
                  prefer_drafts: bool = False, mode: str | None = None) -> tuple[dict, str, list[str]]:
    """Return the first usable JSON object, its provider, and earlier failures."""
    def decode(raw):
        result = parse_json(raw)
        for field, kind in (expected or {}).items():
            if type(result.get(field)) is not kind:
                raise EngineError(f"the response has an invalid or missing {field!r} field")
        return result

    return _complete(system, user, decode, mock=mock, only=only, on_try=on_try,
                     on_failure=on_failure, prefer_drafts=prefer_drafts, mode=mode)


def complete(system: str, user: str, mock=None) -> str:
    """Return the first non-empty text response."""
    return _complete(system, user, str, mock=mock)[0]


def test(name: str | None = None) -> dict:
    t0 = time.monotonic()
    failures = []
    def verify(raw):
        if parse_json(raw).get("ok") is not True:
            raise EngineError("the model returned an unexpected connection test response")

    try:
        _, used, _ = _complete('Reply with exactly this JSON and nothing else: {"ok": true}', "ping",
                               verify, mock=lambda: '{"ok": true}', only=name, prefer_drafts=name is None,
                               mode="test", on_failure=lambda provider, reason, elapsed: failures.append({
                                   "provider": LABELS[provider], "message": reason[:180], "seconds": elapsed}))
        return {"ok": True, "engine": used, "seconds": round(time.monotonic() - t0, 1),
                "message": f"{LABELS[used]} connected.", "failures": failures}
    except Exception as e:
        return {"ok": False, "engine": name, "seconds": round(time.monotonic() - t0, 1),
                "message": str(e)[:600], "failures": failures}


def parse_json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    if t.lstrip().startswith("["):
        raise EngineError("the model returned a JSON array instead of an object")
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < start:
        raise EngineError("the model did not return JSON")
    try:
        return json.loads(t[start:end + 1])
    except ValueError as e:
        raise EngineError(f"couldn't parse the model's JSON: {e}") from e


def load_dotenv() -> None:
    env = WEBUI / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v:
                os.environ.setdefault(k.strip(), v)
