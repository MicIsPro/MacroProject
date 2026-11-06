import os
import subprocess
import sys
import time
import urllib.request

SCRIPT_URL = "https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/screenshot_bot.py"
CHECK_EVERY = 300
RESTART_DELAY = 30

HERE = os.path.dirname(os.path.abspath(__file__))
BOT_PATH = os.path.join(HERE, "bot.py")


def fetch():
    try:
        req = urllib.request.Request(SCRIPT_URL, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        if b"client.run" not in data:
            print("Downloaded file doesn't look like the bot, ignoring it.")
            return None
        return data
    except Exception as e:
        print(f"Update check failed: {e}")
        return None


def update() -> bool:
    data = fetch()
    if data is None:
        return False
    old = None
    if os.path.exists(BOT_PATH):
        with open(BOT_PATH, "rb") as f:
            old = f.read()
    if old == data:
        return False
    with open(BOT_PATH, "wb") as f:
        f.write(data)
    return True


def start():
    return subprocess.Popen([sys.executable, BOT_PATH], cwd=HERE)


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def main():
    update()
    if not os.path.exists(BOT_PATH):
        print("Could not download the bot script. Check SCRIPT_URL and your internet connection.")
        return

    proc = start()
    last_check = time.time()

    while True:
        time.sleep(5)

        if proc.poll() is not None:
            print(f"Bot stopped, restarting in {RESTART_DELAY}s")
            time.sleep(RESTART_DELAY)
            update()
            proc = start()
            last_check = time.time()
            continue

        if time.time() - last_check >= CHECK_EVERY:
            last_check = time.time()
            if update():
                print("Update found, restarting bot")
                stop(proc)
                proc = start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
