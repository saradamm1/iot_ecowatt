"""Servidor do EcoWatt: broker MQTT embutido + middleware + API FastAPI + dashboard.

Executar:  python -m ecowatt.servidor      (ou use iniciar.py, que também abre o simulador)
"""
import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as cfg
from .banco import Banco
from .nucleo import Nucleo

logging.getLogger("amqtt").setLevel(logging.ERROR)
logging.getLogger("transitions").setLevel(logging.ERROR)
log = logging.getLogger("ecowatt")
STATIC = Path(__file__).parent / "static"


def _log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Servico:
    def __init__(self):
        self.db = Banco(cfg.BANCO)
        self.cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ecowatt-middleware")
        self.nucleo = Nucleo(self.db, self._publicar, log=_log)
        self.parar = threading.Event()

    def _publicar(self, topico, payload):
        self.cliente.publish(topico, payload, qos=1)

    def iniciar_mqtt(self):
        def on_connect(c, u, flags, rc, props=None):
            c.subscribe([(f"ecowatt/{cfg.PREDIO}/+/+/telemetria", 1), (f"ecowatt/{cfg.PREDIO}/+/status", 1)])
            _log("[MIDDLEWARE] conectado ao broker e assinando os tópicos")

        def on_message(c, u, m):
            try:
                self.nucleo.receber(m.topic, m.payload)
            except Exception as e:  # o middleware nunca pode parar por causa de uma mensagem
                _log(f"[ERRO] {m.topic}: {e!r}")

        self.cliente.on_connect = on_connect
        self.cliente.on_message = on_message
        self.cliente.reconnect_delay_set(1, 30)
        self.cliente.connect(cfg.MQTT_HOST, cfg.MQTT_PORTA, keepalive=15)
        self.cliente.loop_start()
        threading.Thread(target=self._loop_tick, daemon=True).start()

    def _loop_tick(self):
        while not self.parar.is_set():
            try:
                self.nucleo.tick()
            except Exception as e:
                _log(f"[ERRO tick] {e!r}")
            time.sleep(1)

    def simulacao(self, cenario: str):
        n = self.nucleo
        if cenario == "normal":
            n.hora_simulada = 10 if cfg.PERFIL == "demo" else None
            n.meta_kwh = cfg.META_MENSAL_KWH
            n.rn02_mantido.clear()
        elif cenario == "madrugada":
            n.hora_simulada = 2
        elif cenario == "meta":
            n.meta_kwh = 3600.0
            n.ultimo_rn04 = 0
        self.cliente.publish(cfg.TOPICO_SIMULACAO, json.dumps({"cenario": cenario}), qos=1)
        if cenario == "rele_travado":   # tenta desligar a carga cujo relé não responde
            threading.Timer(1.5, lambda: n.comando("qd03-c02", "OFF", "teste de falha")).start()
        _log(f"[SIMULAÇÃO] cenário “{cenario}” acionado")


servico: Servico | None = None


async def iniciar_broker():
    from amqtt.broker import Broker
    broker = Broker({
        "listeners": {"default": {"type": "tcp", "bind": f"{cfg.MQTT_HOST}:{cfg.MQTT_PORTA}"}},
        "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}},
    })
    await broker.start()
    _log(f"[BROKER] MQTT ouvindo em {cfg.MQTT_HOST}:{cfg.MQTT_PORTA}")
    return broker


@asynccontextmanager
async def lifespan(app):
    global servico
    broker = await iniciar_broker()
    servico = Servico()
    await asyncio.to_thread(servico.iniciar_mqtt)
    _log(f"[API] dashboard em http://localhost:{cfg.API_PORTA}  (perfil de tempo: {cfg.PERFIL})")
    yield
    servico.parar.set()
    servico.cliente.loop_stop()
    await broker.shutdown()


app = FastAPI(title="EcoWatt API", version="1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/estado")
def estado():
    e = servico.nucleo.estado()
    db = servico.db
    e["alertas"] = db.q("SELECT * FROM alertas ORDER BY CASE status WHEN 'aberto' THEN 0 WHEN 'reconhecido' THEN 1 "
                        "ELSE 2 END, id DESC LIMIT 40")
    e["comandos"] = db.q("SELECT * FROM comandos ORDER BY id DESC LIMIT 15")
    e["notificacoes"] = db.q("SELECT * FROM notificacoes ORDER BY id DESC LIMIT 15")
    e["invalidas"] = db.q("SELECT * FROM mensagens_invalidas ORDER BY id DESC LIMIT 10")
    e["anomalias"] = db.q("SELECT * FROM anomalias ORDER BY id DESC LIMIT 10")
    e["parametros"] = asdict(cfg.P)
    e["total_leituras"] = db.q("SELECT COUNT(*) n FROM leituras")[0]["n"]
    return e


@app.get("/api/circuitos/{circ}/serie")
def serie(circ: str, minutos: int = 10):
    if circ not in cfg.CIRC:
        raise HTTPException(404, "circuito não encontrado")
    desde = int(time.time() * 1000) - minutos * 60000
    return servico.db.q("SELECT ts, potencia, corrente, tensao, presenca, rele, buffered FROM leituras "
                        "WHERE circuito=? AND ts>=? ORDER BY ts", (circ, desde))


@app.post("/api/alertas/{aid}/reconhecer")
def reconhecer(aid: int):
    a = servico.db.alerta(aid)
    if not a:
        raise HTTPException(404, "alerta não encontrado")
    if a["status"] == "aberto":
        servico.db.atualizar_alerta(aid, status="reconhecido")
    return {"ok": True}


@app.post("/api/alertas/{aid}/manter-ligado")
def manter(aid: int):
    if not servico.nucleo.cancelar_acao(aid):
        raise HTTPException(409, "não há ação pendente neste alerta")
    return {"ok": True}


class Rele(BaseModel):
    ligar: bool


@app.post("/api/circuitos/{circ}/rele")
def rele(circ: str, corpo: Rele):
    if circ not in cfg.CIRC:
        raise HTTPException(404, "circuito não encontrado")
    try:
        cid = servico.nucleo.comando(circ, "ON" if corpo.ligar else "OFF", "usuário (dashboard)")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"comando_id": cid}


CENARIOS = {"normal", "madrugada", "sala_vazia", "sobrecarga", "meta", "tensao", "offline", "queda_internet",
            "duplicadas", "corrompidos", "relogio", "rele_travado"}


@app.post("/api/simulacao/{cenario}")
def simular(cenario: str):
    if cenario not in CENARIOS:
        raise HTTPException(404, "cenário desconhecido")
    servico.simulacao(cenario)
    return {"ok": True}


@app.post("/api/limpar")
def limpar():
    servico.db.limpar()
    servico.nucleo.stats.clear()
    servico.nucleo.serie_total.clear()
    return {"ok": True}


@app.exception_handler(Exception)
def erro(req, exc):
    return JSONResponse({"erro": str(exc)}, status_code=500)


def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=cfg.API_PORTA, log_level="warning")


if __name__ == "__main__":
    main()
