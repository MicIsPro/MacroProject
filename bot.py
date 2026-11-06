import asyncio
import ctypes
import glob
import io
import os
import time
from pathlib import Path

import discord
import mss
import mss.tools
import psutil


def find_macrobot_folder():
    here = Path(__file__).resolve().parent
    home = Path.home()
    candidates = [here, here / "macrobot"]
    for base in (
        home,
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / "OneDrive" / "Desktop",
        home / "OneDrive" / "Documents",
    ):
        candidates.append(base / "macrobot")

    for c in candidates:
        if c.name.lower() == "macrobot" and (c / "token.txt").is_file():
            return c

    skip = {"appdata", "node_modules", ".git", "$recycle.bin"}
    for root, dirs, files in os.walk(home):
        depth = len(Path(root).relative_to(home).parts)
        dirs[:] = [d for d in dirs if d.lower() not in skip] if depth < 4 else []
        if Path(root).name.lower() == "macrobot" and "token.txt" in (f.lower() for f in files):
            return Path(root)
    return None


def load_config() -> dict:
    template = "token=YOUR_BOT_TOKEN\nlabel=Your Pc\nowner_id=123456789012345678"
    folder = find_macrobot_folder()
    if folder is None:
        print(f"Could not find a folder named 'macrobot' containing token.txt.\nCreate it (for example on your Desktop) and put a token.txt in it like this:\n{template}")
        raise SystemExit

    cfg = {}
    with open(folder / "token.txt", "r", encoding="utf-8") as f:
        for line in f.read().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            cfg[key.strip().lower()] = value.strip().strip('"')

    for key in ("token", "label", "owner_id"):
        if not cfg.get(key):
            print(f"token.txt in {folder} is missing '{key}'. Format:\n{template}")
            raise SystemExit
    try:
        cfg["owner_id"] = int(cfg["owner_id"])
    except ValueError:
        print("owner_id in token.txt must be a number.")
        raise SystemExit

    print(f"Loaded settings from {folder}")
    return cfg


CONFIG = load_config()
TOKEN = CONFIG["token"]
PC_LABEL = CONFIG["label"]
OWNER_ID = CONFIG["owner_id"]

ALERT_CHANNEL_ID = 0
PREFIX = "-ss"
ROBLOX_WINDOW_TITLE = "Roblox"

CHECK_INTERVAL = 5
ALERT_COOLDOWN = 30
PROCESS_NAMES = {"robloxplayerbeta.exe", "windows10universal.exe"}
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox", "logs")
DISCONNECT_KEYWORDS = [
    "sending disconnect with reason",
    "connection lost",
    "lost connection",
    "disconnection notification",
    "you were kicked",
    "kicked from",
]

try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
watcher_started = False
last_channel = None
last_disconnect = None
last_crash = None


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def fmt_event(ts) -> str:
    if ts is None:
        return "none since the bot started"
    return f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))} ({fmt_duration(time.time() - ts)} ago)"


def grab(monitor_index: int) -> bytes:
    with mss.mss() as sct:
        if monitor_index >= len(sct.monitors):
            raise ValueError(f"Monitor {monitor_index} not found.")
        shot = sct.grab(sct.monitors[monitor_index])
        return mss.tools.to_png(shot.rgb, shot.size)


def grab_window(title: str) -> bytes:
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        raise ValueError(f"Window '{title}' not found.")
    if user32.IsIconic(hwnd):
        raise ValueError(f"Window '{title}' is minimized.")

    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise ValueError(f"Window '{title}' has no visible area.")

    with mss.mss() as sct:
        shot = sct.grab({"left": origin.x, "top": origin.y, "width": width, "height": height})
        return mss.tools.to_png(shot.rgb, shot.size)


def roblox_processes():
    procs = []
    for p in psutil.process_iter(["name", "create_time"]):
        if (p.info["name"] or "").lower() in PROCESS_NAMES:
            procs.append(p)
    return procs


def roblox_running() -> bool:
    return len(roblox_processes()) > 0


def build_status() -> str:
    procs = roblox_processes()
    if procs:
        started = min(p.info["create_time"] for p in procs)
        ram = 0
        for p in procs:
            try:
                ram += p.memory_info().rss
            except Exception:
                pass
        roblox_line = f"running for {fmt_duration(time.time() - started)} ({ram / 1024 / 1024:.0f} MB)"
    else:
        roblox_line = "NOT running"

    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    return (
        f"**Status ({PC_LABEL})**\n"
        f"Roblox: {roblox_line}\n"
        f"Last disconnect: {fmt_event(last_disconnect)}\n"
        f"Last crash: {fmt_event(last_crash)}\n"
        f"CPU: {cpu:.0f}% | RAM: {mem.percent:.0f}% ({mem.used / 1024 ** 3:.1f}/{mem.total / 1024 ** 3:.1f} GB)"
    )


def newest_log():
    files = glob.glob(os.path.join(LOG_DIR, "*.log"))
    return max(files, key=os.path.getmtime) if files else None


def read_new_lines(path: str, pos: int):
    size = os.path.getsize(path)
    if size < pos:
        pos = 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(pos)
        data = f.read()
        pos = f.tell()
    return data.splitlines(), pos


async def ping_owner(text: str, channel=None, screenshot: bool = True):
    content = f"<@{OWNER_ID}> {text}"
    try:
        if channel is None:
            channel = last_channel
        if channel is None and ALERT_CHANNEL_ID:
            channel = client.get_channel(ALERT_CHANNEL_ID) or await client.fetch_channel(ALERT_CHANNEL_ID)
        if channel is None:
            print(f"No channel to send alert to: {text}")
            return

        file = None
        if screenshot:
            try:
                png = await asyncio.to_thread(grab, 0)
                file = discord.File(io.BytesIO(png), filename="alert_screenshot.png")
            except Exception as e:
                print(f"Alert screenshot failed: {e}")

        await channel.send(content, file=file)
    except Exception as e:
        print(f"Could not send alert: {e}")


async def roblox_watcher():
    global last_disconnect, last_crash
    await client.wait_until_ready()
    was_running = False
    log_path = None
    pos = 0
    last_alert = 0.0

    while not client.is_closed():
        try:
            running = await asyncio.to_thread(roblox_running)

            if was_running and not running:
                last_crash = time.time()
                await ping_owner(f"Roblox Crashed at {now_str()}")
            if running and not was_running:
                log_path = None
            was_running = running

            if running:
                latest = await asyncio.to_thread(newest_log)
                if latest != log_path:
                    log_path = latest
                    pos = os.path.getsize(latest) if latest else 0
                elif log_path:
                    lines, pos = await asyncio.to_thread(read_new_lines, log_path, pos)
                    for line in lines:
                        low = line.lower()
                        if any(k in low for k in DISCONNECT_KEYWORDS):
                            print(f"Disconnect log line: {line.strip()[:300]}")
                            if time.time() - last_alert > ALERT_COOLDOWN:
                                last_alert = time.time()
                                last_disconnect = last_alert
                                await ping_owner(f"Roblox Disconnected at {now_str()}")
                            break
        except Exception as e:
            print(f"Watcher error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


@client.event
async def on_ready():
    global watcher_started
    print(f"Logged in as {client.user} on {PC_LABEL} - type -cmds in Discord for commands")
    if not watcher_started:
        watcher_started = True
        client.loop.create_task(roblox_watcher())
        print("Roblox watcher running")


@client.event
async def on_message(message: discord.Message):
    global last_channel
    if message.author.bot:
        return
    if message.author.id != OWNER_ID:
        return

    cmd = message.content.strip().lower()
    screens = {
        f"{PREFIX}1": (1, "screen1.png"),
        f"{PREFIX}2": (2, "screen2.png"),
        f"{PREFIX}a": (0, "all_screens.png"),
    }
    window_cmd = f"{PREFIX}r"
    others = {"-rb", "-testc", "-testdc", "-cmds", "-status", window_cmd}
    if cmd not in screens and cmd not in others:
        return

    last_channel = message.channel

    if cmd == "-cmds":
        await message.channel.send(
            f"**Commands ({PC_LABEL})**\n"
            f"`{PREFIX}1` - screenshot of screen 1\n"
            f"`{PREFIX}2` - screenshot of screen 2\n"
            f"`{PREFIX}a` - screenshot of all screens\n"
            f"`{window_cmd}` - screenshot of just the Roblox window\n"
            "`-status` - Roblox uptime, last disconnect/crash, CPU and RAM\n"
            "`-rb` - check if Roblox is running\n"
            "`-testc` - test the Roblox crashed alert\n"
            "`-testdc` - test the Roblox disconnected alert\n"
            "`-cmds` - show this list"
        )
        return

    if cmd == "-status":
        try:
            text = await asyncio.to_thread(build_status)
            await message.channel.send(text)
        except Exception as e:
            await message.channel.send(f"Status failed: {e}")
        return

    if cmd == "-rb":
        status = "running" if await asyncio.to_thread(roblox_running) else "NOT running"
        await message.channel.send(f"Roblox is {status} on {PC_LABEL}.")
        return

    if cmd == "-testc":
        await ping_owner(f"Roblox Crashed at {now_str()} (test)", message.channel)
        return

    if cmd == "-testdc":
        await ping_owner(f"Roblox Disconnected at {now_str()} (test)", message.channel)
        return

    if cmd == window_cmd:
        try:
            png = await asyncio.to_thread(grab_window, ROBLOX_WINDOW_TITLE)
            await message.channel.send(
                f"Screenshotted Roblox window on {PC_LABEL}",
                file=discord.File(io.BytesIO(png), filename="roblox_window.png"),
            )
        except Exception as e:
            await message.channel.send(f"Window screenshot failed: {e}")
        return

    index, filename = screens[cmd]
    try:
        png = await asyncio.to_thread(grab, index)
        await message.channel.send(f"Screenshotted {PC_LABEL}", file=discord.File(io.BytesIO(png), filename=filename))
    except Exception as e:
        await message.channel.send(f"Screenshot failed: {e}")


client.run(TOKEN)
