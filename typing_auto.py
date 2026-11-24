# -*- coding: utf-8 -*-
"""
Web typing-lesson automation (Playwright) — human-like keystroke edition
========================================================================

What this is
------------
A small, configurable automation helper for **web typing lessons** of the
TypingClub family (TypingClub / EdClub school instances). It does NOT log in
for you and does NOT choose the lesson for you: you log in manually, open a
typing article manually, and the script then

    1. reads the article text out of the typing iframe,
    2. types it with a human-like rhythm (random target speed, jitter, word
       pauses, slow drift, optional "typo + backspace" slip-ups),
    3. reads the result screen and retries once with a higher speed if the
       lesson's required speed was not reached.

Why Playwright (and not a global keyboard hook)
-----------------------------------------------
1. The typing engine lives in a **cross-origin iframe**. A system-level
   keyboard hook cannot target it reliably, while Playwright can address
   frames directly.
2. Sending keys through the browser protocol does not steal the OS focus,
   so you can keep using your mouse/keyboard elsewhere.

Configuration is fully external (see config.example.json):
    every site URL, DOM selector, speed range, timing constant and browser
    option can be overridden without touching this file.

Typical usage:
    python typing_auto.py                     # uses ./config.json if present
    python typing_auto.py --config my.json --wpm-range 55,70
    python typing_auto.py --dry-run           # only extract the text
    python typing_auto.py --print-config      # show the effective config

Author: the contributors of this repository.  License: MIT.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


# ============================================================
# 1. Defaults — everything the user may want to change lives here.
#    (Any of these can be overridden by config.json / CLI flags.)
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    # --------------------------------------------------------
    # The site. Point these at YOUR typing platform.
    # School instances usually look like https://<school>.edclub.com
    # --------------------------------------------------------
    "site": {
        "home_url": "https://www.typingclub.com",
        "course_url": "https://www.typingclub.com/sportal/program-3.game",
        # Template used by --lesson; "{lesson}" is replaced by the lesson id.
        # Leave empty if the site has no direct lesson URL.
        "lesson_url_template": "{course_url}?lesson={lesson}",
    },

    # --------------------------------------------------------
    # DOM selectors. If the front end is redesigned, fix it here only.
    # --------------------------------------------------------
    "selectors": {
        # Substring of the typing iframe URL (the text is inside that frame).
        "frame_keyword": "typing",
        # One element per character cell.
        "unit_selector": "span.token_unit",
        # Line container; clicking it moves the keyboard focus into the frame.
        "line_selector": "span.line",
        # Cells already typed (right / wrong), used for progress checks.
        "typed_selector": "span.token_unit._vld, span.token_unit._err",
        # Minimum number of character cells before we consider the page ready.
        "min_units": 20,
    },

    # --------------------------------------------------------
    # Target speed and human rhythm.
    # --------------------------------------------------------
    "typing": {
        # Random target WPM picked once per run; ignored when --wpm is given.
        "wpm_min": 55,
        "wpm_max": 75,
        # Gaussian jitter on top of the base interval (0 = metronome).
        "jitter": 0.22,
        # Probability of a deliberate typo followed by a backspace (0 = none).
        "mistake_rate": 0.0,
        # Retries when the required speed was not reached.
        "max_retry": 1,
        # How much faster the retry should be: required + random(lo, hi).
        "retry_boost_min": 6,
        "retry_boost_max": 14,
        # Extra time is added as a multiple of the unit interval, never as a
        # fixed number of seconds (see HumanRhythm docstring). The base
        # interval is multiplied by this factor to compensate for the pauses.
        "overhead_compensation": 0.80,
        # Hard floor between two keystrokes: below this the lesson engine
        # starts dropping characters (accuracy drops below 100%).
        "min_key_gap": 0.06,
        # Slow random walk of the speed factor (human stamina curve).
        "drift_sigma": 0.015,
        "drift_floor": 0.95,
        "drift_ceil": 1.05,
        # Chance of a longer "distracted" pause, and its length in units.
        "distracted_chance": 0.04,
        "distracted_min_units": 1.0,
        "distracted_max_units": 3.0,
        # Pause before/after a space, in units (not seconds).
        "word_pause_after_min": 0.0,
        "word_pause_after_max": 0.9,
        "word_pause_before_min": 0.1,
        "word_pause_before_max": 0.5,
    },

    # --------------------------------------------------------
    # Browser.
    # --------------------------------------------------------
    "browser": {
        # auto | chrome | edge | chromium
        "channel": "auto",
        # Optional explicit executable path (used by the attach mode).
        "exe_path": "",
        # Independent profile dir; keeps your daily browser untouched.
        "profile_dir": "",          # default: <BASE_DIR>/browser_profile
        "debug_port": 9222,
        "viewport_width": 1440,
        "viewport_height": 900,
        "locale": "zh-CN",
        # Bypass the system proxy (typical fix for the iframe not loading).
        "no_proxy": False,
        "proxy_server": "",
        "extra_args": ["--disable-blink-features=AutomationControlled"],
    },

    # --------------------------------------------------------
    # Login: the script never stores a password.
    # Set these two environment variables to auto-fill the form, or just
    # log in by hand (recommended).
    # --------------------------------------------------------
    "login": {
        "username_field": "input[name=username]",
        "password_field": "input[name=password]",
        "submit_label": "Log in",
        # Name of the env vars to read; empty string = never auto-fill.
        "username_env": "TC_USER",
        "password_env": "TC_PASS",
        # URL fragment that proves the login worked.
        "success_url_fragment": "/student/",
        "wait_seconds": 300,
    },

    # --------------------------------------------------------
    # Behaviour of the waiting / retry logic.
    # --------------------------------------------------------
    "network": {
        "frame_wait_seconds": 180,
        "page_timeout_ms": 60000,
        "reload_after_retry_clicks": 3,
        "max_reloads": 2,
        # Hosts whose failed requests are reported (diagnostics only).
        "diagnostic_hosts": ["api.edclub.com", ".edclub.app"],
    },

    # --------------------------------------------------------
    # Texts shown by the lesson engine itself.
    # Translate them if you use a non-English instance.
    # --------------------------------------------------------
    "messages": {
        "activity_error_hints": [
            "This Activity Didn't Load",
            "此活动未能加载",
            "无法加载活动",
        ],
        "retry_labels": ["Try again", "再试一次", "重试"],
    },

    # --------------------------------------------------------
    # Result-screen parsing.
    # --------------------------------------------------------
    "result": {
        # Unit string printed next to the speed, e.g. "字/分钟" or "WPM".
        "speed_unit": "字/分钟",
        "actual_speed_label": "速度",
        "required_speed_label": "要求速度",
        "target_speed_label": "目标速度",
        "accuracy_label": "准确率",
        "required_accuracy_label": "要求准确率",
        "score_label": "得分",
        "screenshot_path": "typing_result.png",
    },
}

CONFIG_PATH = ""          # filled by load_config()
CFG = copy.deepcopy(DEFAULT_CONFIG)


# ============================================================
# 2. Config helpers
# ============================================================

def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (override wins)."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: str | None) -> dict:
    """
    Build the effective config: defaults <- ./config.json <- --config file.

    A missing file is not an error; defaults are usable on their own.
    Keys whose name starts with "_" are treated as comments and ignored.
    """
    global CONFIG_PATH, CFG

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    candidates = []
    if path:
        candidates.append(path)
    else:
        candidates.append(os.path.join(BASE_DIR, "config.json"))

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as fh:
                    user_cfg = json.load(fh)
            except Exception as exc:
                print(f"⚠️ 配置文件读取失败（{candidate}）：{exc}，改用默认配置。")
                continue
            deep_merge(cfg, {k: v for k, v in user_cfg.items() if not k.startswith("_")})
            CONFIG_PATH = candidate
            break
    if path and not CONFIG_PATH:
        print(f"⚠️ 指定的配置文件不存在：{path}，改用默认配置。")

    CFG = cfg
    return cfg


def c(dotted: str, default=None):
    """Read a value with a dotted path: c("typing.wpm_min")."""
    node = CFG
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


# ============================================================
# 3. Human rhythm model (random speed + slow drift)
# ============================================================

class HumanRhythm:
    """
    Turn a perfectly even machine cadence into something that looks human.

    Five traits are modelled:
      1. base speed        -> interval = 60 / (WPM * 5)
      2. per-key cost      -> shift chars, digits and punctuation are slower
      3. jitter            -> Gaussian noise on every interval
      4. word pauses       -> short hesitation around spaces
      5. slow drift        -> random walk of the speed factor (stamina curve)

    Key design point: every extra pause is expressed as a multiple of the
    *unit* interval, never as a fixed number of seconds. Otherwise the pauses
    would eat a growing share of the time the faster you set the target, and
    "ask for 90, measure 72" happens.
    """

    def __init__(self, wpm: float, jitter: float = 0.22, mistake_rate: float = 0.0,
                 drift: bool = True):
        self.wpm = wpm
        self.unit = 60.0 / (wpm * 5.0)
        self.base_interval = self.unit * float(c("typing.overhead_compensation", 0.80))
        self.jitter = jitter
        self.mistake_rate = mistake_rate
        self.drift = drift
        self.speed_factor = 1.0
        self.min_gap = float(c("typing.min_key_gap", 0.06))
        self.drift_sigma = float(c("typing.drift_sigma", 0.015))
        self.drift_floor = float(c("typing.drift_floor", 0.95))
        self.drift_ceil = float(c("typing.drift_ceil", 1.05))

    def update_drift(self):
        """Nudge the speed factor a little; keeps the run from being a line."""
        if not self.drift:
            return
        self.speed_factor += random.gauss(0, self.drift_sigma)
        self.speed_factor = min(self.drift_ceil, max(self.drift_floor, self.speed_factor))

    @staticmethod
    def char_cost(ch: str) -> float:
        if ch == " ":
            return 0.85           # space bar is the cheapest key
        if ch.isupper():
            return 1.35           # needs Shift
        if ch.isdigit():
            return 1.25           # far from the home row
        if ch in ",.;:!?()[]{}\"'<>@#$%^&*+=|\\/-_~`":
            return 1.45           # punctuation sits on the edges
        return 1.0

    def delay_for(self, ch: str, prev_ch: str) -> float:
        """Seconds to wait *after* typing `ch`."""
        base = self.base_interval * self.char_cost(ch) * self.speed_factor

        if self.jitter > 0:
            base = random.gauss(base, base * self.jitter)
            base = max(base, self.unit * 0.2)

        # Word-boundary hesitation (in units, not seconds).
        if prev_ch == " ":
            base += random.uniform(float(c("typing.word_pause_after_min", 0.0)),
                                   float(c("typing.word_pause_after_max", 0.9))) * self.unit
        if ch == " ":
            base += random.uniform(float(c("typing.word_pause_before_min", 0.1)),
                                   float(c("typing.word_pause_before_max", 0.5))) * self.unit

        # Occasional distracted pause, so the curve is not smooth.
        if random.random() < float(c("typing.distracted_chance", 0.04)):
            base += random.uniform(float(c("typing.distracted_min_units", 1.0)),
                                   float(c("typing.distracted_max_units", 3.0))) * self.unit

        # Never go below the engine's reliable repeat rate.
        return max(base, self.min_gap)


# ============================================================
# 4. Browser helpers
# ============================================================

def resolve_channel(choice: str):
    """
    Pick the browser engine.

      auto     -> installed Chrome, else installed Edge, else Playwright Chromium
      chrome   -> local Google Chrome    (channel="chrome")
      edge     -> local Microsoft Edge   (channel="msedge")
      chromium -> Playwright's bundled Chromium

    Using `channel` reuses the installed binary while keeping a separate,
    throw-away profile, so your daily browser data is untouched.
    """
    if choice == "chromium":
        return None, "Playwright Chromium"
    if choice == "chrome":
        return "chrome", "Google Chrome"
    if choice == "edge":
        return "msedge", "Microsoft Edge"

    exe = find_browser_exe("chrome")
    if exe:
        return "chrome", f"Google Chrome ({exe})"
    exe = find_browser_exe("edge")
    if exe:
        return "msedge", f"Microsoft Edge ({exe})"
    return None, "Playwright Chromium (no local Chrome/Edge found)"


def find_browser_exe(kind: str) -> str:
    """
    Locate an installed browser executable (Windows / macOS / Linux).

    Used by the "attach" mode, which starts a *normal* browser process with a
    debugging port instead of letting Playwright launch it.
    """
    explicit = c("browser.exe_path", "") or ""
    if explicit and os.path.exists(explicit):
        return explicit

    env_local = os.environ.get("LOCALAPPDATA", "")
    env_program = os.environ.get("PROGRAMFILES", "")
    env_program86 = os.environ.get("PROGRAMFILES(X86)", "")

    if kind == "edge":
        candidates = [
            os.path.join(env_program, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(env_program86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(env_local, r"Microsoft\Edge\Application\msedge.exe"),
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/usr/bin/microsoft-edge",
        ]
    else:
        candidates = [
            os.path.join(env_program, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(env_program86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(env_local, r"Google\Chrome\Application\chrome.exe"),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
        ]

    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


def read_system_proxy() -> dict:
    """
    Read the Windows "Settings > Network > Proxy" configuration.

    Returns {'enable': bool, 'server': str, 'auto': str}; empty dict on other
    platforms or when reading fails.
    """
    if sys.platform != "win32":
        return {}
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            enable = winreg.QueryValueEx(key, "ProxyEnable")[0]
            try:
                server = winreg.QueryValueEx(key, "ProxyServer")[0]
            except OSError:
                server = ""
            try:
                auto = winreg.QueryValueEx(key, "AutoConfigURL")[0]
            except OSError:
                auto = ""
        return {"enable": bool(enable), "server": server or "", "auto": auto or ""}
    except Exception:
        return {}


def proxy_port_alive(server: str) -> bool:
    """
    A proxy can be "enabled" in the registry while its client is already gone,
    leaving a dead port that breaks every request routed through it.
    """
    host, _, port = server.partition(":")
    if not port:
        host, _, port = server.rpartition(":")[0], "", ""
    host = host.strip() or "127.0.0.1"
    try:
        port = int(port.strip())
    except ValueError:
        return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def report_proxy_state():
    """Print a short proxy health line before the browser starts."""
    info = read_system_proxy()
    if not info:
        return
    if not info["enable"]:
        tail = f"（残留配置 {info['server']} 当前不生效）" if info["server"] else ""
        print(f"🛡️ 系统代理：已关闭{tail}")
        return
    if info["auto"]:
        print(f"🛡️ 系统代理：已开启（自动配置脚本 {info['auto']}）")
        return
    alive = proxy_port_alive(info["server"])
    if alive:
        print(f"🛡️ 系统代理：已开启 {info['server']}（端口可连）")
    else:
        print(f"🛡️⚠️ 系统代理：已开启 {info['server']}，但端口连不上（代理客户端可能已退出）")
        print("      这会让跨站 iframe 加载失败；可加 --no-proxy，或关掉系统代理后重试。")


def open_browser(p, browser_choice: str = "auto", profile: str = None, launch_args: list = None):
    """
    Start the browser (headless=False, because you log in by hand).

      · default  -> plain launch, a throw-away profile
      · profile  -> launch_persistent_context, login state may survive restarts
    """
    channel, label = resolve_channel(browser_choice)
    print(f"🌐 浏览器：{label}")

    launch_kwargs = {
        "headless": False,
        "args": list(c("browser.extra_args", []) or []) + (launch_args or []),
    }
    if channel:
        launch_kwargs["channel"] = channel

    viewport = {
        "width": int(c("browser.viewport_width", 1440)),
        "height": int(c("browser.viewport_height", 900)),
    }
    locale = c("browser.locale", "zh-CN")

    if profile:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile,
            viewport=viewport,
            locale=locale,
            **launch_kwargs,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return ctx, page

    browser = p.chromium.launch(**launch_kwargs)
    context = browser.new_context(viewport=viewport, locale=locale)
    return browser, context.new_page()


def install_diagnostics(page):
    """
    Report only failures that matter for the typing activity (its API and the
    cross-origin app host). Sub-resource cancels (net::ERR_ABORTED) happen on
    every SPA navigation and are filtered out on purpose.
    """
    hosts = tuple(c("network.diagnostic_hosts", []))
    seen = set()

    def log(msg):
        if msg in seen or len(seen) >= 12:
            return
        seen.add(msg)
        print(msg)

    def on_failed(req):
        if not any(h in req.url for h in hosts):
            return
        failure = (req.failure or "").upper()
        if "ERR_ABORTED" in failure:
            return
        log(f"  ⚠️ 请求失败：{req.url.split('?')[0]}  ({req.failure})")

    def on_response(resp):
        if resp.status >= 400 and any(h in resp.url for h in hosts):
            log(f"  ⚠️ HTTP {resp.status}：{resp.url.split('?')[0]}")

    page.on("requestfailed", on_failed)
    page.on("response", on_response)


def wait_for_cdp(port: int, timeout_s: int = 25) -> bool:
    """Wait until the Chromium debugging port really accepts connections."""
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def launch_browser_attached(port: int, profile_dir: str, kind: str = "edge"):
    """
    Fallback mode: start a *normal* browser process with a debugging port, then
    let Playwright connect to it.

    Unlike a Playwright-launched browser, this process carries no automation
    flags, so cross-origin iframes behave exactly like in your everyday
    browser — the usual answer to "the activity does not load inside the
    scripted browser".
    """
    exe = find_browser_exe(kind)
    if not exe:
        print(f"❌ 本机没找到 {kind}，可改用 --browser chrome / chromium，"
              f"或在配置里填 browser.exe_path。")
        return None

    os.makedirs(profile_dir, exist_ok=True)
    print(f"🧷 备用方案：以调试端口 {port} 启动本地浏览器（独立配置目录 {profile_dir}）")
    proc = subprocess.Popen([
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        c("site.home_url"),
    ])

    if not wait_for_cdp(port, 25):
        print("❌ 调试端口没起来，可能被别的进程占用了同一端口。")
        return None
    print("✅ 调试端口就绪，正在接管这个浏览器…")
    return proc


def wait_for_login(page, timeout_s: int = None):
    """
    Wait until you have logged in (URL contains the configured fragment).

    If the username/password env vars are set, the form is filled for you;
    otherwise just log in by hand. **No credential is ever stored here.**
    """
    timeout_s = timeout_s or int(c("login.wait_seconds", 300))
    page.goto(c("site.home_url"), wait_until="domcontentloaded",
              timeout=int(c("network.page_timeout_ms", 60_000)))

    user_env = c("login.username_env", "TC_USER")
    pass_env = c("login.password_env", "TC_PASS")
    user = os.environ.get(user_env) if user_env else None
    pwd = os.environ.get(pass_env) if pass_env else None

    try:
        page.wait_for_selector(c("login.password_field"), timeout=8_000)
        if user and pwd:
            print(f"检测到 {user_env} / {pass_env}，自动填表登录…")
            page.locator(c("login.username_field")).fill(user)
            page.locator(c("login.password_field")).fill(pwd)
            page.locator(f"button:has-text('{c('login.submit_label')}')").first.click(no_wait_after=True)
        else:
            print("\n👉 请在浏览器窗口里手动登录（脚本不会替你登录）。")
    except Exception:
        pass   # no login form within 8s -> we are probably already logged in

    fragment = c("login.success_url_fragment", "/student/")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        # Only the URL *path* counts: an auth redirect often carries
        # "?next=/student/..." in the query string, which must not be
        # mistaken for a completed login.
        if fragment in urlparse(page.url).path:
            print("✅ 已进入学生端：", page.url)
            return True
        page.wait_for_timeout(500)
    print("❌ 等待登录超时，当前 URL：", page.url)
    return False


# ============================================================
# 5. Locating the typing frame and reading the text
# ============================================================

def _find_frame_once(page):
    """One pass over all frames: return the first one with enough cells."""
    unit_selector = c("selectors.unit_selector")
    min_units = int(c("selectors.min_units", 20))
    keyword = c("selectors.frame_keyword", "")
    for frame in page.frames:
        if keyword and keyword not in (frame.url or ""):
            continue
        try:
            if frame.locator(unit_selector).count() >= min_units:
                return frame
        except Exception:
            continue        # the iframe may be rebuilding right now
    return None


def _click_activity_retry(page) -> bool:
    """
    If the page shows the engine's "activity didn't load" screen, press its
    retry button. Returns True when something was clicked.
    """
    hints = tuple(c("messages.activity_error_hints", []))
    labels = tuple(c("messages.retry_labels", []))
    for frame in page.frames:
        try:
            text = frame.locator("body").inner_text(timeout=800)
        except Exception:
            continue
        if not any(hint in text for hint in hints):
            continue
        for label in labels:
            try:
                btn = frame.locator(f"button:has-text('{label}')").first
                if btn.count() > 0:
                    btn.click(timeout=2_000)
                    return True
            except Exception:
                continue
        try:
            page.keyboard.press("Enter")     # some builds focus the button
            return True
        except Exception:
            pass
    return False


def find_typing_frame(page, timeout_s: int = None):
    """
    Find the frame that actually holds the typing content, with self-healing:
    a failed activity screen is retried, then the lesson page is reloaded.
    """
    timeout_s = timeout_s or int(c("network.frame_wait_seconds", 180))
    reload_threshold = int(c("network.reload_after_retry_clicks", 3))
    max_reloads = int(c("network.max_reloads", 2))
    unit_selector = c("selectors.unit_selector")

    deadline = time.time() + timeout_s
    retry_clicks = 0
    reloads = 0
    last_count = -1

    while time.time() < deadline:
        frame = _find_frame_once(page)
        if frame is not None:
            count = frame.locator(unit_selector).count()
            if count != last_count:
                print(f"✅ 找到打字内容，字符单元数：{count}")
                last_count = count
            return frame

        if _click_activity_retry(page):
            retry_clicks += 1
            print(f"⚠️ 检测到活动加载失败页，已点击重试（第 {retry_clicks} 次）")
            page.wait_for_timeout(3_000)
            if retry_clicks % reload_threshold == 0 and reloads < max_reloads:
                reloads += 1
                print(f"↻ 连续 {retry_clicks} 次仍失败，重载页面（第 {reloads} 次）")
                try:
                    page.reload(wait_until="domcontentloaded", timeout=45_000)
                except Exception:
                    pass
            continue

        page.wait_for_timeout(500)

    return None


def page_has_activity_error(page) -> bool:
    """True if the "activity didn't load" screen is still on screen."""
    hints = tuple(c("messages.activity_error_hints", []))
    for frame in page.frames:
        try:
            text = frame.locator("body").inner_text(timeout=800)
        except Exception:
            continue
        if any(hint in text for hint in hints):
            return True
    return False


def extract_target_text(frame) -> str:
    """
    Rebuild the full string that has to be typed, from the DOM.

    Measured DOM shape:
        span.line > span.token > span.token_unit ×N
    A token is a word, a token_unit is a single character. The space at the
    end of a word is written as `&nbsp;<i> </i>`, whose textContent is
    "non-breaking space + normal space" — two characters — so joining
    textContent would add a stray space. We therefore read only the first
    text node and map U+00A0 back to a plain space.
    """
    js = """
    () => {
        const units = document.querySelectorAll('%s');
        const out = [];
        for (const u of units) {
            const node = u.firstChild;
            let v = (node && node.nodeType === 3) ? node.nodeValue : '';
            if (v === '\\u00a0') v = ' ';
            out.push(v);
        }
        return out.join('');
    }
    """ % c("selectors.unit_selector")
    return frame.evaluate(js)


def count_typed_units(frame) -> int:
    """Number of cells already typed; -1 when the DOM did not answer."""
    js = "() => document.querySelectorAll('%s').length" % c("selectors.typed_selector")
    try:
        return frame.evaluate(js)
    except Exception:
        return -1


def focus_typing_area(frame):
    """Click the text area once so that keyboard events land inside the frame."""
    try:
        frame.locator(c("selectors.line_selector")).first.click(timeout=5_000)
        return True
    except Exception as e:
        print("⚠️ 点击文字区域失败（可能已被自动聚焦）：", e)
        return False


def press_char(page, ch: str):
    """
    Send one real keystroke.

    keyboard.type() emits keydown / keypress / char / keyup and handles Shift
    for capitals and symbols — exactly the sequence the engine expects.
    keyboard.press() is kept for functional keys such as Enter / Backspace.
    """
    aliases = {"\n": "Enter", "\t": "Tab"}
    page.keyboard.type(aliases.get(ch, ch))


# ============================================================
# 6. Reading the result screen ("did we pass?")
# ============================================================

def read_result_text(page) -> str:
    """
    Return the whole text of the result card. It may live in the top document
    or still inside a frame, so look for the speed unit in every frame.
    """
    unit = c("result.speed_unit", "字/分钟")
    for f in page.frames:
        try:
            text = f.locator("body").inner_text(timeout=3_000)
        except Exception:
            continue
        if unit in text:
            return text
    return ""


def parse_result(text: str) -> dict:
    """
    Parse the result card into numbers.

    Typical layout (translated):
        score 2357
        duration 1 min 30 s
        speed 44 <unit>
        accuracy 100 %
        100 % real accuracy   80 % required accuracy
        41 <unit> required   56 <unit> target

    Note that the words for "speed" appear in three different lines, so the
    "required"/"target" labels are what disambiguate them.
    """
    out = {}
    unit = re.escape(c("result.speed_unit", "字/分钟"))
    actual = re.escape(c("result.actual_speed_label", "速度"))
    required = re.escape(c("result.required_speed_label", "要求速度"))
    target = re.escape(c("result.target_speed_label", "目标速度"))
    acc = re.escape(c("result.accuracy_label", "准确率"))
    req_acc = re.escape(c("result.required_accuracy_label", "要求准确率"))
    score = re.escape(c("result.score_label", "得分"))

    # actual speed: "速度 60 <unit>"
    m = re.search(rf"{actual}\s*[:\s]?\s*(\d+)\s*{unit}", text)
    if m:
        out["实际速度"] = int(m.group(1))

    # thresholds, number first, label after ("41 <unit> 要求速度")
    m = re.search(rf"(\d+)\s*{unit}\s*{required}", text)
    if m:
        out["要求速度"] = int(m.group(1))
    m = re.search(rf"(\d+)\s*{unit}\s*{target}", text)
    if m:
        out["目标速度"] = int(m.group(1))

    # thresholds, label first ("要求速度 41 <unit>")
    if "要求速度" not in out:
        m = re.search(rf"{required}[^\d]{{0,7}}(\d+)", text)
        if m:
            out["要求速度"] = int(m.group(1))
    if "目标速度" not in out:
        m = re.search(rf"{target}[^\d]{{0,7}}(\d+)", text)
        if m:
            out["目标速度"] = int(m.group(1))

    m = re.search(rf"{acc}\s*[:\s]?\s*(\d+)\s*%", text)
    if m:
        out["准确率"] = int(m.group(1))
    m = re.search(rf"(\d+)\s*%\s*{req_acc}", text)
    if m:
        out["要求准确率"] = int(m.group(1))
    m = re.search(rf"{score}[\s:]*(\d+)", text)
    if m:
        out["得分"] = int(m.group(1))

    return out


# ============================================================
# 7. The typing loop
# ============================================================

def type_article(page, frame, text: str, rhythm: HumanRhythm):
    """Type the whole article character by character. Returns elapsed seconds."""
    total = len(text)
    prev_ch = ""
    start_ts = time.time()
    stuck_warned = False
    drift_every = 25          # refresh the speed factor this often
    check_every = 40          # ask the DOM for real progress this often

    for idx, ch in enumerate(text, start=1):
        # Optional "human slip": type a wrong letter, then backspace it away.
        if rhythm.mistake_rate and ch.isalpha() and random.random() < rhythm.mistake_rate:
            wrong = random.choice([x for x in "abcdefghijklmnopqrstuvwxyz" if x != ch.lower()])
            press_char(page, wrong)
            time.sleep(random.uniform(0.04, 0.15))
            page.keyboard.press("Backspace")
            time.sleep(random.uniform(0.05, 0.12))

        press_char(page, ch)
        time.sleep(rhythm.delay_for(ch, prev_ch))
        prev_ch = ch

        if idx % drift_every == 0:
            rhythm.update_drift()

        if idx % check_every == 0 or idx == total:
            done = count_typed_units(frame)
            elapsed = time.time() - start_ts
            wpm = (idx / 5.0) / (elapsed / 60.0) if elapsed > 0 else 0
            print(f"  进度 {idx}/{total} ({idx / total * 100:5.1f}%)  "
                  f"| 页面已确认 {done} 格 | 当前 {wpm:4.1f} WPM")

            # The DOM lagging far behind usually means the focus was lost.
            if done >= 0 and done < idx - 20 and not stuck_warned:
                print("  ⚠️ 页面确认进度落后，可能焦点丢失。正在尝试重新聚焦…")
                stuck_warned = True
                try:
                    focus_typing_area(frame)
                except Exception:
                    pass

    return time.time() - start_ts


def run_one_attempt(page, frame, text: str, rhythm: HumanRhythm) -> float:
    """One full attempt: focus -> type -> wait for the result card."""
    focus_typing_area(frame)
    page.wait_for_timeout(600)
    elapsed = type_article(page, frame, text, rhythm)
    page.wait_for_timeout(3_000)
    return elapsed


# ============================================================
# 8. Entry point
# ============================================================

def pick_speed(fixed_wpm, wpm_range) -> float:
    """Fixed --wpm if given, otherwise one random value from the range."""
    if fixed_wpm:
        return float(fixed_wpm)
    lo, hi = wpm_range
    return round(random.uniform(lo, hi), 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Web typing-lesson automation (human-like keystrokes).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", type=str, default=None,
                        help="path to a JSON config (default: ./config.json)")
    parser.add_argument("--print-config", action="store_true",
                        help="print the effective config and exit")
    parser.add_argument("--init-config", action="store_true",
                        help="write config.json from the built-in defaults and exit")
    parser.add_argument("--wpm", type=float, default=None, help="fixed target speed")
    parser.add_argument("--wpm-range", type=str, default=None, help="random range, e.g. 55,70")
    parser.add_argument("--jitter", type=float, default=None, help="rhythm jitter 0..0.6")
    parser.add_argument("--mistake-rate", type=float, default=None, help="typo probability")
    parser.add_argument("--max-retry", type=int, default=None, help="retries when too slow")
    parser.add_argument("--lesson", type=str, default=None, help="open a specific lesson id")
    parser.add_argument("--auto-start", action="store_true", help="skip the Enter prompt")
    parser.add_argument("--dry-run", action="store_true", help="extract text only")
    parser.add_argument("--browser", type=str, default=None,
                        choices=["auto", "edge", "chrome", "chromium"],
                        help="browser engine to use")
    parser.add_argument("--profile", type=str, default=None,
                        help="persistent profile dir (do not point at your daily browser)")
    parser.add_argument("--attach-browser", action="store_true",
                        help="fallback: start a normal local browser with a debug port and attach")
    parser.add_argument("--attach-kind", type=str, default="edge", choices=["edge", "chrome"],
                        help="which local browser the attach mode should start")
    parser.add_argument("--attach-port", type=int, default=None,
                        help="attach to an already running debug port")
    parser.add_argument("--no-proxy", action="store_true", help="bypass the system proxy")
    parser.add_argument("--proxy-server", type=str, default=None,
                        help="force a proxy, e.g. 127.0.0.1:8080")
    return parser


def apply_cli_overrides(args):
    """CLI flags win over the config file, but only when actually given."""
    if args.browser:
        CFG["browser"]["channel"] = args.browser
    if args.no_proxy:
        CFG["browser"]["no_proxy"] = True
    if args.proxy_server:
        CFG["browser"]["proxy_server"] = args.proxy_server
    if args.jitter is not None:
        CFG["typing"]["jitter"] = args.jitter
    if args.mistake_rate is not None:
        CFG["typing"]["mistake_rate"] = args.mistake_rate
    if args.max_retry is not None:
        CFG["typing"]["max_retry"] = args.max_retry
    if args.wpm_range:
        lo, hi = (float(x) for x in args.wpm_range.split(","))
        if lo > hi:
            lo, hi = hi, lo
        CFG["typing"]["wpm_min"], CFG["typing"]["wpm_max"] = lo, hi


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.init_config:
        target = args.config or os.path.join(BASE_DIR, "config.json")
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CONFIG, fh, ensure_ascii=False, indent=2)
        print(f"✅ 已写出配置模板：{target}")
        return

    load_config(args.config)

    if args.print_config:
        print(json.dumps(CFG, ensure_ascii=False, indent=2))
        return

    apply_cli_overrides(args)

    if CONFIG_PATH:
        print(f"📄 配置：{CONFIG_PATH}")
    else:
        print("📄 配置：内置默认（可 --init-config 生成 config.json 后修改）")

    wpm_range = (float(c("typing.wpm_min", 55)), float(c("typing.wpm_max", 75)))
    jitter = float(c("typing.jitter", 0.22))
    mistake_rate = float(c("typing.mistake_rate", 0.0))
    max_retry = int(c("typing.max_retry", 1))
    frame_wait = int(c("network.frame_wait_seconds", 180))
    page_timeout = int(c("network.page_timeout_ms", 60_000))
    course_url = c("site.course_url")
    profile_root = c("browser.profile_dir") or os.path.join(BASE_DIR, "browser_profile")
    debug_profile = os.path.join(profile_root, "attach_profile")

    with sync_playwright() as p:
        attached = False

        report_proxy_state()
        extra_args = []
        if c("browser.no_proxy", False):
            extra_args.append("--no-proxy-server")
            print("🚫 已禁用系统代理：浏览器将完全绕过代理")
        proxy_server = c("browser.proxy_server", "")
        if proxy_server:
            extra_args.append(f"--proxy-server={proxy_server}")
            print(f"🔀 已指定代理：{proxy_server}")

        attach_mode = args.attach_browser or args.attach_port
        if attach_mode:
            # Fallback: drive a *normal* browser (no automation flags).
            port = args.attach_port or int(c("browser.debug_port", 9222))
            if args.attach_browser:
                if launch_browser_attached(port, debug_profile, kind=args.attach_kind) is None:
                    return
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            # Do not reuse an existing tab: it may be closed at any moment.
            page = context.new_page()
            attached = True
            print(f"🔗 已接管调试端口 {port}（不会关闭该浏览器，只新开一个标签页）")
        else:
            browser, page = open_browser(p, browser_choice=c("browser.channel", "auto"),
                                         profile=args.profile, launch_args=extra_args)

        install_diagnostics(page)
        try:
            # ---- step 1: login (by hand, unless env vars are set) ----
            if not wait_for_login(page):
                return

            # ---- step 2: open the course / lesson ----
            lesson_tpl = c("site.lesson_url_template", "")
            if args.lesson:
                if not lesson_tpl:
                    print("❌ 配置里没有 site.lesson_url_template，无法直接用 --lesson 打开课时。")
                    return
                url = lesson_tpl.format(lesson=args.lesson, course_url=course_url,
                                        home_url=c("site.home_url"))
                page.goto(url, wait_until="domcontentloaded", timeout=page_timeout)
            elif course_url and course_url not in page.url:
                print(f"\n👉 请手动进入课程页（例如 {course_url}），然后选一篇文章。")

            # ---- step 3: one article per loop ----
            while True:
                if not args.auto_start:
                    cmd = input("\n👉 选好一篇文章、等文字出现后按 Enter 开始（输 q 回车退出）…").strip().lower()
                    if cmd == "q":
                        print("已退出。")
                        break

                print(f"\n⏳ 正在等待打字页面加载（最长 {frame_wait} 秒）…")
                frame = find_typing_frame(page)
                if frame is None:
                    if page_has_activity_error(page):
                        print("❌ 打字活动始终加载失败（页面停在 activity-didn't-load 页）。")
                        print("   常见原因：代理/VPN 或杀软拦截了跨站域名；浏览器禁用了跨站 Cookie。")
                        print("   建议：① 关掉代理或把相关域名加白；② 用 --attach-port 9222 "
                              "接管你日常那个浏览器来跑。")
                    else:
                        print("❌ 没有找到打字内容。可能选到了视频/游戏课，或页面还没进到文章页。")
                    break

                text = extract_target_text(frame)
                if not text:
                    print("❌ 文字抓取为空，检查 selectors.unit_selector 是否需要更新。")
                    break

                lesson_url = page.url          # remember it, the retry needs it
                print("\n" + "=" * 60)
                print(f"文章长度：{len(text)} 个字符（含空格）")
                print(f"开头预览：{text[:80]}…" if len(text) > 80 else f"全文：{text}")
                print("=" * 60)

                if args.dry_run:
                    print("\n[dry-run] 只抓取，不发送按键。")
                    break

                # ---- step 3.3: type, then check whether we passed ----
                attempt = 0
                stats = {}
                while True:
                    attempt += 1
                    if attempt == 1:
                        target_wpm = pick_speed(args.wpm, wpm_range)
                    else:
                        need = stats.get("要求速度", 0)
                        boost = random.uniform(float(c("typing.retry_boost_min", 6)),
                                               float(c("typing.retry_boost_max", 14)))
                        target_wpm = max(need + boost, pick_speed(args.wpm, wpm_range))

                    print(f"\n🚀 第 {attempt} 次尝试，目标速度 {target_wpm:.1f} WPM"
                          f"（抖动 {jitter}，每 25 字符漂移一次）")
                    rhythm = HumanRhythm(wpm=target_wpm, jitter=jitter, mistake_rate=mistake_rate)
                    elapsed = run_one_attempt(page, frame, text, rhythm)

                    done = count_typed_units(frame)
                    own_wpm = (len(text) / 5.0) / (elapsed / 60.0) if elapsed else 0
                    stats = parse_result(read_result_text(page))
                    print("\n" + "=" * 60)
                    print(f"完成：{done}/{len(text)} 格已确认    耗时 {elapsed:.1f}s"
                          f"    脚本自身统计 {own_wpm:.1f} WPM")
                    if stats:
                        print("站点结算：" + "  ".join(f"{k}={v}" for k, v in stats.items()))

                    need = stats.get("要求速度")
                    actual = stats.get("实际速度", round(own_wpm))
                    if need is None:
                        print("ℹ️ 没读到“要求速度”，无法判断是否达标（结算页结构可能变了）。")
                        break
                    if actual >= need:
                        print(f"✅ 已通过：速度 {actual} ≥ 要求 {need}")
                        break
                    print(f"❌ 未达标：速度 {actual} < 要求 {need}")
                    if attempt > max_retry:
                        print(f"已达最大重试次数 {max_retry}，放弃本次。")
                        break
                    print("↻ 重新进入这一课，提高速度再打一遍…")
                    page.goto(lesson_url, wait_until="domcontentloaded", timeout=page_timeout)
                    frame = find_typing_frame(page)
                    if frame is None:
                        print("❌ 重打时找不到打字内容，结束。")
                        break
                print("=" * 60)

                shot = c("result.screenshot_path", "typing_result.png")
                try:
                    page.screenshot(path=shot)
                    print(f"\n结果截图已保存：{shot}")
                except Exception as exc:
                    print(f"⚠️ 截图失败：{exc}")

                if args.auto_start:
                    break
        finally:
            # In attach mode we only disconnect; the browser belongs to you.
            if not attached:
                browser.close()


if __name__ == "__main__":
    main()
