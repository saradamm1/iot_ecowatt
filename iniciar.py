"""Inicia todo o protótipo com um único comando:  python iniciar.py

1. Broker MQTT + middleware + API + dashboard (este processo)
2. Simulador dos gateways ESP32 (processo separado, como um dispositivo independente)
3. Abre o dashboard no navegador
"""
import os
import subprocess
import sys
import threading
import time

for _s in (sys.stdout, sys.stderr):  # console do Windows: evita erro com acentos e símbolos
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import webbrowser
from pathlib import Path

RAIZ = Path(__file__).parent
os.chdir(RAIZ)
sys.path.insert(0, str(RAIZ))

from ecowatt import config as cfg  # noqa: E402


def iniciar_simulador():
    time.sleep(2)
    proc = subprocess.Popen([sys.executable, str(RAIZ / "simulador.py")], cwd=RAIZ,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    time.sleep(3)
    if "--sem-navegador" not in sys.argv:
        webbrowser.open(f"http://localhost:{cfg.API_PORTA}")
    return proc


if __name__ == "__main__":
    if "--limpar" in sys.argv and Path(cfg.BANCO).exists():
        Path(cfg.BANCO).unlink()
        print("Banco de dados apagado.")
    if "--sem-simulador" not in sys.argv:
        threading.Thread(target=iniciar_simulador, daemon=True).start()
    print("=" * 64)
    print(" EcoWatt — protótipo funcional")
    print(f" Dashboard: http://localhost:{cfg.API_PORTA}    (Ctrl+C para encerrar)")
    print("=" * 64)
    from ecowatt.servidor import main
    try:
        main()
    except KeyboardInterrupt:
        pass
