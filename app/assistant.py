"""Optional AI assistant: the user's own Gemini, ChatGPT/Codex or Claude plans each song's cover.

The assistant runs through its official command-line app on this PC, signed in with the user's own
account; Audioworks has no server and stores no keys. It's started only inside this project, with
the Audioworks guide (assistant_skill.md) as its instructions, and may write one file: the song's
assistant_plan.json. The engine checks that plan against the user's rules before using it, and
without an assistant everything is decided from the measurements as before.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import songfile  # noqa: E402

SETTINGS = ROOT / "audioworks_settings.json"
SKILL = "app/assistant_skill.md"
PLAN = "assistant_plan.json"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

ASSISTANTS = {
    "gemini": {"name": "Gemini", "account": "Google account", "cli": "gemini", "npm": "@google/gemini-cli",
               "login": ["gemini"],
               "login_hint": "In the window that opens, choose \"Sign in with Google\" and finish in your browser. "
                             "When Gemini shows its prompt, type /quit and close the window."},
    "codex": {"name": "ChatGPT / Codex", "account": "ChatGPT account", "cli": "codex", "npm": "@openai/codex",
              "login": ["codex", "login"],
              "login_hint": "Choose \"Sign in with ChatGPT\" and finish in your browser, then close the window."},
    "claude": {"name": "Claude", "account": "Claude account", "cli": "claude", "npm": "@anthropic-ai/claude-code",
               "login": ["claude", "auth", "login"],
               "login_hint": "Finish signing in in your browser, then close the window."},
}


# ---------------------------------------------------------------- connection

def load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data):
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def connected():
    """The connected assistant's key, or None."""
    key = load_settings().get("assistant")
    return key if key in ASSISTANTS else None


def connect(key):
    save_settings({**load_settings(), "assistant": key})


def disconnect():
    data = load_settings()
    data.pop("assistant", None)
    save_settings(data)


def find_cli(key):
    cli = ASSISTANTS[key]["cli"]
    found = shutil.which(cli)
    if not found and sys.platform == "win32":  # npm's global folder isn't always on PATH yet
        for d in (Path(os.environ.get("APPDATA", "")) / "npm", Path.home() / ".local" / "bin"):
            for ext in (".cmd", ".exe", ""):
                if (d / f"{cli}{ext}").exists():
                    return str(d / f"{cli}{ext}")
    return found


def node_available():
    return shutil.which("npm") is not None


def signed_in(key):
    cli = find_cli(key)
    if not cli:
        return False
    if key == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY")) or (Path.home() / ".gemini" / "oauth_creds.json").exists()
    args = [cli, "login", "status"] if key == "codex" else [cli, "auth", "status"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = (r.stdout + r.stderr).lower()
    return r.returncode == 0 and "not logged in" not in text and '"loggedin": false' not in text.replace(" ", "")


def status(key):
    if not find_cli(key):
        return "not installed"
    return "ready" if signed_in(key) else "installed, not signed in"


def open_console(args):
    """Run a command in its own console window, so the user sees it and can answer its questions."""
    if sys.platform == "win32":
        subprocess.Popen(["cmd", "/k", *args], creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen(args)


def install(key):
    open_console(["npm", "install", "-g", ASSISTANTS[key]["npm"]])


def sign_in(key):
    open_console([find_cli(key) or ASSISTANTS[key]["cli"], *ASSISTANTS[key]["login"][1:]])


# ---------------------------------------------------------------- planning a song

# A lighter model for each assistant: planning is reading two files and applying the guide's rules, and
# a light model does it as well (Sonnet gave Opus's plan on Yıldızlar) while using much less of the user's
# plan. Model names change, so a run that fails on the light model is retried once on the default.
LIGHT_MODEL = {"gemini": "flash", "codex": "gpt-6-luna", "claude": "sonnet"}


def command(key, prompt, light=True):
    cli = find_cli(key)
    model = ["--model", LIGHT_MODEL[key]] if light else []
    if key == "gemini":
        # edits are approved automatically; shell commands aren't, so it can read and write files only
        return [cli, *model, "--approval-mode", "auto_edit", "--output-format", "text", "-p", prompt]
    if key == "codex":
        # Codex's sandbox: writes only inside the project, no network for the commands it runs
        return [cli, "exec", *model, "--sandbox", "workspace-write", "--skip-git-repo-check", prompt]
    return [cli, "-p", prompt, *model, "--permission-mode", "acceptEdits",
            "--allowedTools", "Read", "Glob", "Grep", "Write", "Edit", "Bash(.venv/Scripts/python.exe tools/*)"]


def run(key, prompt, log, light):
    """Run the assistant once, streaming its output; returns its exit code."""
    name = ASSISTANTS[key]["name"]
    proc = subprocess.Popen(command(key, prompt, light), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                            creationflags=NO_WINDOW)
    for line in proc.stdout:
        if line.strip():
            log(f"  {name}: {line.rstrip()}")
    return proc.wait(timeout=900)


def study(folder, log=print, key=None):
    """Ask the connected assistant to plan this song; stores its checked plan in the song's notes.
    Returns the plan, or None (the cover is then planned from the measurements alone). Never raises."""
    key = key or connected()
    if not key:
        return None
    a = ASSISTANTS[key]
    folder = Path(folder)
    if not find_cli(key):
        log(f"{a['name']} isn't installed any more; connect it again to use it")
        return None
    rel = folder.resolve().relative_to(ROOT).as_posix()
    plan_file = folder / PLAN
    plan_file.unlink(missing_ok=True)
    # one plain line: the command-line apps are .cmd files on Windows, where quotes and symbols break
    prompt = (f"You are the Audioworks producer for the song in {rel}. Read {SKILL} and follow it. "
              f"The measurements are in {rel}/audioworks.json. Write your plan to {rel}/{PLAN} and change no other file.")
    log(f"Asking {a['name']} to plan the cover (this can take a few minutes)...")
    try:
        code = run(key, prompt, log, light=True)
        if code != 0 and not plan_file.exists():
            log(f"{a['name']} couldn't use its lighter model ({LIGHT_MODEL[key]}); trying its default model")
            code = run(key, prompt, log, light=False)
    except Exception as e:  # noqa: BLE001 - the cover can always be planned from the measurements
        log(f"{a['name']} didn't finish ({e!r}); planning from the measurements")
        return None
    if not plan_file.exists():
        log(f"{a['name']} wrote no plan (exit code {code}); planning from the measurements. "
            "If it asked you to sign in, use Connect AI assistant -> Sign in.")
        return None
    try:
        raw = json.loads(plan_file.read_text(encoding="utf-8"))
    except ValueError as e:
        log(f"{a['name']}'s plan isn't valid JSON ({e}); planning from the measurements")
        return None
    plan, dropped = clean(raw)
    plan.update(by=a["name"], made=datetime.date.today().isoformat())
    notes = songfile.load(folder)
    notes["assistant_plan"] = plan
    songfile.save(folder, notes)
    plan_file.unlink(missing_ok=True)
    log(f"{a['name']}'s plan: " + describe(plan))
    if dropped:
        log("  ignored (outside Audioworks' rules): " + ", ".join(dropped))
    return plan


KEY = re.compile(r"^(chromatic|[A-G][#b]?m?( (major|minor))?)$", re.I)


def clean(raw):
    """Keep only what the engine understands, within the user's rules. Returns (plan, dropped field names)."""
    plan, dropped = {}, []
    if not isinstance(raw, dict):
        return plan, ["everything (not a JSON object)"]
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    at = raw.get("autotune")
    if isinstance(at, dict):
        a = {}
        if isinstance(at.get("key"), str) and KEY.match(at["key"].strip()):
            a["key"] = at["key"].strip()
        if num(at.get("strength")):
            a["strength"] = round(min(1.0, max(0.5, float(at["strength"]))), 2)
        if num(at.get("retune_ms")):
            a["retune_ms"] = int(min(200, max(10, at["retune_ms"])))  # never an instant snap
        dropped += [f"autotune.{k}" for k in at if k not in a and k != "max_shift"]
        if a:
            plan["autotune"] = a
    if num(raw.get("deess_target_pct")):
        plan["deess_target_pct"] = round(min(40.0, max(0.0, float(raw["deess_target_pct"]))), 1)
    if num(raw.get("vocal_above_db")):
        plan["vocal_above_db"] = round(min(4.0, max(1.0, float(raw["vocal_above_db"]))), 1)
    for k in ("deep_tone", "hiss", "distortion", "widen", "vocoder"):
        if isinstance(raw.get(k), bool):
            plan[k] = raw[k]
    if isinstance(raw.get("notes"), str):
        plan["notes"] = raw["notes"].strip()[:600]
    known = {"autotune", "deess_target_pct", "vocal_above_db", "deep_tone", "hiss", "distortion", "widen",
             "vocoder", "notes"}
    dropped += [k for k in raw if k not in known or (k not in plan and k != "autotune")]
    return plan, dropped


def describe(plan):
    parts = []
    if "autotune" in plan:
        at = plan["autotune"]
        parts.append("autotune " + ", ".join(f"{k} {v}" for k, v in at.items()))
    for k, label in (("deep_tone", "deep tone"), ("hiss", "tape hiss"), ("distortion", "amp distortion"),
                     ("widen", "double-tracking"), ("vocoder", "vocoder")):
        if k in plan:
            parts.append(f"{label} {'on' if plan[k] else 'off'}")
    if "vocal_above_db" in plan:
        parts.append(f"vocal {plan['vocal_above_db']:+.1f} dB over the original's balance")
    return "; ".join(parts) or "no changes to the measured settings"


def apply(notes, rec):
    """The song's recommended settings with the assistant's plan laid over them, plus the engine
    options it decided ({option: value})."""
    plan = notes.get("assistant_plan") or {}
    if not plan:
        return rec, {}
    rec = json.loads(json.dumps(rec))
    if "autotune" in plan:
        rec["autotune"] = {**rec.get("autotune", {}), **plan["autotune"], "max_shift": 1.5}
    if "deess_target_pct" in plan:
        rec["deess_target_pct"] = plan["deess_target_pct"]
    if "deep_tone" in plan:
        flags = [f for f in rec.get("match_flags", []) if f != "--max-cut 40"]
        rec["match_flags"] = flags + (["--max-cut 40"] if plan["deep_tone"] else [])
    opts = {k: plan[k] for k in ("hiss", "distortion", "widen", "vocoder") if k in plan}
    if "vocal_above_db" in plan:
        opts["above"] = plan["vocal_above_db"]
    return rec, opts
