"""Launch Siteless as a local desktop app in the user's default browser.

The API binds to loopback only. Frozen builds keep their SQLite database and
configuration in the operating system's per-user application data directory.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


APP_NAME = "Siteless"
BASE_DIR = Path(__file__).resolve().parent


def _user_data_dir() -> Path:
    """Return a writable, per-user data directory on Windows, macOS or Linux."""
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return root / APP_NAME


def _runtime_dir() -> Path:
    # In source mode, keep using the repository's existing database and .env.
    # Packaged applications must never write beside the executable.
    return _user_data_dir() if getattr(sys, "frozen", False) else BASE_DIR


def _wait_until_ready(url: str, server_thread: threading.Thread, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not server_thread.is_alive():
            raise RuntimeError("O servidor local encerrou antes de ficar pronto.")
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise TimeoutError("O Siteless demorou demais para iniciar. Consulte os logs no terminal.")


def main() -> int:
    runtime_dir = _runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(runtime_dir)
    os.environ.setdefault("LEADS_DB_PATH", str(runtime_dir / "leads_sem_site.db"))
    os.environ["SITLESS_DESKTOP"] = "1"

    # Reserve a free loopback port before importing app.py because the billing
    # helpers read APP_BASE_URL at import time. The server socket stays bound.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    url = f"http://127.0.0.1:{port}"
    os.environ["APP_BASE_URL"] = url

    import uvicorn

    from app import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
        loop="asyncio",
        http="h11",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="siteless-server",
        daemon=True,
    )

    print("Iniciando o Siteless no seu computador...")
    print(f"Dados locais: {runtime_dir}")
    thread.start()
    try:
        _wait_until_ready(url, thread)
        print(f"Siteless pronto: {url}")
        print("Esta janela mantém o aplicativo em execução. Feche-a para encerrar.")
        try:
            webbrowser.open(url)
        except Exception:
            print(f"Abra este endereço no navegador: {url}")
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Encerrando o Siteless...")
    except Exception as exc:
        print(f"Não foi possível iniciar o Siteless: {exc}")
        return 1
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
