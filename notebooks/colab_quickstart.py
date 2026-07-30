# %% [markdown]
# # Ouroboros Colab Quickstart
#
# Runs full source-mode Ouroboros in Google Colab without the desktop UI and
# brings up the Telegram control bridge automatically.

# %%
import json
import os
import pathlib
import subprocess
import sys

try:
    from google.colab import drive  # type: ignore
except Exception as exc:  # pragma: no cover - only meaningful in Colab
    raise RuntimeError("This quickstart is intended for Google Colab.") from exc

drive.mount("/content/drive")

# Minimal bootstrap clone so `ouroboros.colab_bootstrap` becomes importable.
# Remote roles and fast-forward updates are handled by clone_or_update_repo below.
REPO_DIR = pathlib.Path("/content/ouroboros_repo")
if not (REPO_DIR / ".git").exists():
    subprocess.run(
        ["git", "clone", "--branch", "ouroboros", "https://github.com/razzant/ouroboros.git", str(REPO_DIR)],
        check=True,
    )

os.chdir(REPO_DIR)
sys.path.insert(0, str(REPO_DIR))

# %%
from ouroboros.colab_bootstrap import (
    build_colab_settings,
    clone_or_update_repo,
    collect_colab_secrets,
    configure_colab_personal_origin,
    ensure_native_telegram_live,
    export_colab_env,
    masked_secret_status,
    server_command,
    write_colab_settings,
)

# Canonical update: establish the `managed` remote role and fast-forward.
clone_or_update_repo(REPO_DIR)
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", "."], check=True)

APP_ROOT = pathlib.Path("/content/drive/MyDrive/Ouroboros")
DATA_DIR = APP_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

secrets = collect_colab_secrets()
# Preserve prior owner choices on Drive across ephemeral Colab sessions (a re-run
# of this cell must not wipe a pinned chat, tweaked models, or other prefs).
_existing_settings = {}
_settings_file = DATA_DIR / "settings.json"
if _settings_file.exists():
    try:
        _existing_settings = json.loads(_settings_file.read_text(encoding="utf-8"))
    except Exception:
        _existing_settings = {}
settings = build_colab_settings(
    secrets,
    total_budget=float(os.environ.get("TOTAL_BUDGET", "10")),
    runtime_mode=os.environ.get("OUROBOROS_RUNTIME_MODE", "advanced"),
    max_workers=int(os.environ.get("OUROBOROS_MAX_WORKERS", "1")),
    existing=_existing_settings,
)
# GitHub persistence is optional: a personal fork is configured only when a token
# is present, otherwise the prototype still runs (without remote self-persistence).
origin_result = configure_colab_personal_origin(REPO_DIR, DATA_DIR, settings)
settings_path = write_colab_settings(DATA_DIR, settings)
export_colab_env(REPO_DIR, DATA_DIR, settings_path)

print("Secrets configured:", masked_secret_status(settings))
print("Personal origin:", origin_result)
print("Settings:", settings_path)

# %%
server = subprocess.Popen(
    server_command(REPO_DIR),
    cwd=str(REPO_DIR),
    env=os.environ.copy(),
)
print("Ouroboros server PID:", server.pid)

# Grant, enable, and configure the bundled native Telegram skill over loopback.
telegram_status = ensure_native_telegram_live(settings=settings)
print("Native Telegram:", telegram_status)
if telegram_status.get("ok") and telegram_status.get("settings_ok"):
    print("Message your Telegram bot now. Your first owner slash command (e.g. /status) registers your chat and asks you to send it once more;")
    print("after that, owner commands like /status and /panic run immediately.")
elif telegram_status.get("ok"):
    print("Telegram is enabled, but its settings were not applied:", telegram_status.get("warning"))
    print("Set full_access, mirror mode all, and Mini App on in the Telegram skill settings.")
else:
    print("Native Telegram not live yet:", telegram_status.get("error") or telegram_status)
