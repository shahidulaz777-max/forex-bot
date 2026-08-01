import subprocess
import time
import os
import socket
import sys

BOT_SCRIPT = "main.py"         # Path to the main bot script
HEARTBEAT_FILE = "heartbeat.txt"
CHECK_INTERVAL = 5             # Check every 5 seconds
HANG_TIMEOUT = 60              # Restart if no heartbeat for 60 seconds
RESTART_DELAY = 5              # Delay before restarting process

def check_internet(host="8.8.8.8", port=53, timeout=3) -> bool:
    """Verifies active internet connectivity via socket connection."""
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except OSError:
        return False

def touch_heartbeat():
    """Initializes or touches the heartbeat file."""
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        print(f"[WATCHER] Error touching heartbeat file: {e}")

def start_bot() -> subprocess.Popen:
    """Launches the bot main process using the active virtualenv python interpreter."""
    print(f"[WATCHER] 🚀 Starting {BOT_SCRIPT}...")
    touch_heartbeat()
    return subprocess.Popen([sys.executable, BOT_SCRIPT])

def main():
    process = None
    print("[WATCHER] 🏥 Auto-Doctor System initialized.")

    while True:
        # 1. CHECK INTERNET CONNECTIVITY
        if not check_internet():
            print("[WATCHER] ⚠️ No internet connection detected. Waiting...")
            if process is not None:
                print("[WATCHER] Terminating bot process until connection is restored.")
                process.kill()
                process.wait()
                process = None
            time.sleep(CHECK_INTERVAL)
            continue

        # 2. CHECK PROCESS CRASH OR INITIAL START
        if process is None or process.poll() is not None:
            if process is not None:
                print(f"[WATCHER] 💥 Bot process exited/crashed with code: {process.returncode}")
                time.sleep(RESTART_DELAY)
            process = start_bot()

        # 3. CHECK HANG / FROZEN LOOP VIA HEARTBEAT FILE
        if process is not None and os.path.exists(HEARTBEAT_FILE):
            last_modified = os.path.getmtime(HEARTBEAT_FILE)
            time_since_beat = time.time() - last_modified

            if time_since_beat > HANG_TIMEOUT:
                print(f"[WATCHER] 🚨 Bot hang detected! Silent for {time_since_beat:.1f}s. Restarting...")
                process.kill()
                process.wait()
                time.sleep(RESTART_DELAY)
                process = start_bot()

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[WATCHER] Auto-Doctor stopped by user.")
