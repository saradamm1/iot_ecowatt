"""Simulador dos gateways ESP32 do EcoWatt.

Cada quadro de distribuição é um "ESP32" com conexão MQTT própria, Last Will (LWT),
número de sequência, buffer local para quedas de conexão e relé com estado seguro (fail-safe).
Os cenários de teste são acionados pelo dashboard (tópico ecowatt/<predio>/simulacao).

Executar:  python simulador.py
"""
import json
import sys
import random
import threading
import time

for _s in (sys.stdout, sys.stderr):  # console do Windows: evita erro com acentos e símbolos
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import paho.mqtt.client as mqtt

from ecowatt import config as cfg

P = cfg.P
AGREGACAO_S = 60 if cfg.PERFIL == "real" else 10   # buffer guarda médias deste período
DURACAO_QUEDA_S = 30 if cfg.PERFIL == "demo" else 300

# corrente (A), presença, fp — perfil normal em horário de expediente
NORMAL = {
    "qd00-c01": (3.6, None, 0.95),
    "qd01-c01": (8.0, None, 0.97),
    "qd01-c02": (8.6, True, 0.94),
    "qd02-c01": (10.9, True, 0.95),
    "qd02-c02": (3.0, True, 0.95),
    "qd03-c01": (28.1, True, 0.95),
    "qd03-c02": (0.5, True, 0.96),
}
MADRUGADA = {
    "qd00-c01": (3.4, None, 0.95),
    "qd01-c01": (7.7, None, 0.97),
    "qd01-c02": (1.1, False, 0.93),
    "qd02-c01": (0.0, False, 0.95),
    "qd02-c02": (1.65, False, 0.95),   # luz esquecida: ~2,3x o consumo base -> RN01
    "qd03-c01": (0.3, False, 0.95),
    "qd03-c02": (0.0, False, 0.96),
}


def log(msg):
    print(time.strftime("%H:%M:%S"), "[SIMULADOR]", msg, flush=True)


class EstadoCircuito:
    def __init__(self, cid):
        self.c = cfg.CIRC[cid]
        self.curto = cid.split("-")[1]
        self.corrente, self.presenca, self.fp = NORMAL[cid]
        self.tensao = 220.0
        self.rele = True
        self.energia = round(random.uniform(800, 2500), 3)
        self.seq = random.randint(1000, 5000)
        self.travado = False
        self.adiantar_s = 0
        self.adiantar_n = 0
        self.historico = []          # últimas mensagens enviadas (para o teste de duplicatas)

    def ler(self, agora_ms, incluir_ambiente):
        i = self.corrente * random.uniform(0.97, 1.03) if self.rele else 0.0
        v = self.tensao + random.uniform(-1.5, 1.5)
        fp = min(1.0, self.fp + random.uniform(-0.01, 0.01))
        pot = v * i * fp
        self.energia += pot / 1000 * (P.intervalo_leitura_s / 3600)
        self.seq += 1
        ts = agora_ms
        if self.adiantar_n > 0:
            ts += self.adiantar_s * 1000
            self.adiantar_n -= 1
        msg = {
            "device_id": self.c.device_id, "ts": ts, "seq": self.seq,
            "tensao": round(v, 1), "corrente": round(i, 2), "potencia": round(pot, 1),
            "fp": round(fp, 2), "frequencia": round(60 + random.uniform(-0.05, 0.05), 2),
            "presenca": self.presenca, "rele": self.rele, "rssi": random.randint(-72, -55), "buffered": False,
        }
        if incluir_ambiente:
            msg.update(energia=round(self.energia, 3), temperatura=round(random.uniform(22.5, 25.5), 1),
                       umidade=round(random.uniform(52, 62), 1))
        return msg


class Gateway:
    def __init__(self, quadro):
        self.q = quadro
        self.circ = {cid: EstadoCircuito(cid) for cid in cfg.QUADROS[quadro].circuitos}
        self.cli = None
        self.conectado = False
        self.desligado_ate = 0        # queda de internet simulada
        self.morto = False            # gateway offline (cenário)
        self.buffer = []              # leituras guardadas durante a queda
        self.acumulador = {}          # agregação para o buffer
        self.ultimo_ambiente = 0
        self.lock = threading.RLock()
        self.conectar()

    # -------- conexão
    def conectar(self):
        cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"esp32-{self.q}")
        cli.will_set(cfg.topico_status(self.q), json.dumps({"status": "offline"}), qos=1, retain=True)
        cli.on_connect = self._on_connect
        cli.on_disconnect = lambda *a: setattr(self, "conectado", False)
        cli.on_message = self._on_message
        try:
            cli.connect(cfg.MQTT_HOST, cfg.MQTT_PORTA, keepalive=10)
            cli.loop_start()
        except OSError as e:
            log(f"{self.q}: broker indisponível ({e}); nova tentativa em seguida")
        self.cli = cli

    def _on_connect(self, c, u, flags, rc, props=None):
        self.conectado = True
        c.publish(cfg.topico_status(self.q), json.dumps({"status": "online"}), qos=1, retain=True)
        c.subscribe(f"ecowatt/{cfg.PREDIO}/{self.q}/+/cmd", qos=1)
        if self.acumulador or self.buffer:
            self._descarregar_buffer()

    def derrubar(self, abrupto=True):
        """Simula perda de energia/rede: fecha o socket sem DISCONNECT, o broker publica o LWT."""
        with self.lock:
            self.cli.loop_stop()
            try:
                if abrupto and self.cli.socket():
                    self.cli.socket().close()
                else:
                    self.cli.disconnect()
            except Exception:
                pass
            self.conectado = False

    # -------- mensagens recebidas
    def _on_message(self, c, u, m):
        curto = m.topic.split("/")[3]
        est = next((e for e in self.circ.values() if e.curto == curto), None)
        if not est:
            return
        cmd = json.loads(m.payload)
        if est.travado:
            log(f"{est.c.nome}: relé travado, comando {cmd['acao']} ignorado")
            return
        est.rele = cmd["acao"] == "ON"
        log(f"{est.c.nome}: relé {'LIGADO' if est.rele else 'DESLIGADO'} (comando {cmd['cmd_id']})")
        self.publicar_leitura(est, int(time.time() * 1000), False)   # confirma o novo estado

    # -------- publicação
    def publicar_leitura(self, est, agora, ambiente):
        msg = est.ler(agora, ambiente)
        if self.conectado:
            self.cli.publish(cfg.topico_telemetria(self.q, est.curto), json.dumps(msg), qos=1)
            est.historico = (est.historico + [msg])[-5:]
        else:
            self._guardar(est, msg)

    def _guardar(self, est, msg):
        """Durante a queda: agrega por período e guarda em 'flash' (lista) para reenvio."""
        chave = (est.c.id, msg["ts"] // (AGREGACAO_S * 1000))
        acc = self.acumulador.setdefault(chave, {"n": 0, "soma": {}, "ultima": msg})
        acc["n"] += 1
        acc["ultima"] = msg
        for k in ("tensao", "corrente", "potencia", "fp"):
            acc["soma"][k] = acc["soma"].get(k, 0) + msg[k]

    def _descarregar_buffer(self):
        for (cid, _), acc in sorted(self.acumulador.items(), key=lambda kv: kv[1]["ultima"]["ts"]):
            m = dict(acc["ultima"])
            for k, s in acc["soma"].items():
                m[k] = round(s / acc["n"], 2)
            m["buffered"] = True
            self.buffer.append((cid, m))
        self.acumulador.clear()
        n = len(self.buffer)
        for cid, m in self.buffer:
            self.cli.publish(cfg.topico_telemetria(self.q, cid.split("-")[1]), json.dumps(m), qos=1)
        self.buffer.clear()
        log(f"{self.q}: reconectado — {n} leituras agregadas reenviadas do buffer (buffered=true)")

    def ciclo(self):
        agora = int(time.time() * 1000)
        if self.morto:
            return
        if self.desligado_ate and agora >= self.desligado_ate:
            self.desligado_ate = 0
            log(f"{self.q}: conexão restabelecida, reconectando")
            self.conectar()
        elif self.desligado_ate:
            # fail-safe: sem comunicação, todos os relés voltam para LIGADO
            for est in self.circ.values():
                if not est.rele:
                    est.rele = True
                    log(f"{est.c.nome}: fail-safe — relé religado por falta de comunicação")
        ambiente = agora - self.ultimo_ambiente >= P.intervalo_ambiente_s * 1000
        if ambiente:
            self.ultimo_ambiente = agora
        for est in self.circ.values():
            self.publicar_leitura(est, agora, ambiente)


class Simulador:
    def __init__(self):
        self.gw = {q: Gateway(q) for q in cfg.QUADROS}
        # canal de controle dos cenários de teste (separado dos gateways)
        self.ctrl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ecowatt-simulador-controle")
        self.ctrl.on_connect = lambda c, *a: c.subscribe(cfg.TOPICO_SIMULACAO, qos=1)
        self.ctrl.on_message = self._on_controle
        self.ctrl.connect(cfg.MQTT_HOST, cfg.MQTT_PORTA, keepalive=30)
        self.ctrl.loop_start()

    def _on_controle(self, c, u, m):
        try:
            nome = json.loads(m.payload).get("cenario")
        except (ValueError, AttributeError):
            nome = None
        if m.topic != cfg.TOPICO_SIMULACAO or not nome:
            log(f"mensagem de controle ignorada: {m.topic} {m.payload[:60]!r}")
            return
        threading.Thread(target=self.cenario, args=(nome,), daemon=True).start()

    def est(self, cid) -> EstadoCircuito:
        return self.gw[cfg.CIRC[cid].quadro].circ[cid]

    def aplicar_perfil(self, perfil):
        for cid, (i, pres, fp) in perfil.items():
            e = self.est(cid)
            e.corrente, e.presenca, e.fp, e.tensao = i, pres, fp, 220.0

    def cenario(self, nome):
        log(f"cenário: {nome}")
        if nome == "normal":
            self.aplicar_perfil(NORMAL)
            for g in self.gw.values():
                for e in g.circ.values():
                    e.rele, e.travado, e.adiantar_n = True, False, 0
                if g.morto or g.desligado_ate:
                    g.morto, g.desligado_ate = False, 0
                    g.conectar()
        elif nome == "madrugada":
            self.aplicar_perfil(MADRUGADA)
        elif nome == "sala_vazia":
            e = self.est("qd02-c01")
            e.presenca, e.rele, e.corrente = False, True, 10.9
        elif nome == "sobrecarga":
            self.est("qd03-c01").corrente = 30.0
            a = self.est("qd03-c02")
            a.corrente, a.rele = 16.0, True
        elif nome == "tensao":
            for e in self.gw["qd02"].circ.values():
                e.tensao = 196.0
            self.est("qd02-c01").fp = 0.84
        elif nome == "offline":
            self.gw["qd00"].morto = True
            self.gw["qd00"].derrubar(abrupto=True)
            log("qd00: gateway desligado abruptamente (o broker publicará o LWT)")
        elif nome == "queda_internet":
            g = self.gw["qd02"]
            g.derrubar(abrupto=True)
            g.desligado_ate = int(time.time() * 1000) + DURACAO_QUEDA_S * 1000
            log(f"qd02: sem internet por {DURACAO_QUEDA_S} s — leituras indo para o buffer local")
        elif nome == "duplicadas":
            g = self.gw["qd03"]
            n = 0
            for e in g.circ.values():
                for m in e.historico:
                    g.cli.publish(cfg.topico_telemetria("qd03", e.curto), json.dumps(m), qos=1)
                    n += 1
            log(f"qd03: {n} mensagens reenviadas com o mesmo seq (duplicatas)")
        elif nome == "corrompidos":
            self.corrompidos()
        elif nome == "relogio":
            e = self.est("qd03-c01")
            e.adiantar_s, e.adiantar_n = 60, 5
        elif nome == "rele_travado":
            self.est("qd03-c02").travado = True

    def corrompidos(self):
        g = self.gw["qd01"]
        e = g.circ["qd01-c01"]
        t = cfg.topico_telemetria("qd01", "c01")
        base = e.ler(int(time.time() * 1000), True)
        envios = [
            ("JSON inválido", '{"device_id": "esp32-qd01-c01", "tensao": 220.1, '),
            ("sem seq", json.dumps({k: v for k, v in base.items() if k != "seq"})),
            ("tensão nula", json.dumps({**base, "seq": e.seq + 1, "tensao": None})),
            ("corrente fora da faixa", json.dumps({**base, "seq": e.seq + 2, "corrente": 500.0})),
            ("energia menor que a anterior", json.dumps({**base, "seq": e.seq + 3, "energia": 1.0})),
            ("device_id trocado", json.dumps({**base, "seq": e.seq + 4, "device_id": "esp32-qd09-c01"})),
            ("tipo errado", json.dumps({**base, "seq": e.seq + 5, "potencia": "alta"})),
        ]
        e.seq += 6
        e.energia = 1.0   # medidor "resetado": a energia recomeça do zero
        for desc, payload in envios:
            g.cli.publish(t, payload, qos=1)
            log(f"qd01: enviada mensagem com problema — {desc}")
            time.sleep(0.2)

    def executar(self):
        for _ in range(50):              # aguarda todos os gateways conectarem
            if all(g.conectado for g in self.gw.values()):
                break
            time.sleep(0.1)
        log(f"iniciado com {len(cfg.CIRCUITOS)} circuitos em {len(self.gw)} quadros; "
            f"leitura a cada {P.intervalo_leitura_s} s")
        while True:
            inicio = time.time()
            for g in self.gw.values():
                try:
                    g.ciclo()
                except Exception as ex:
                    log(f"{g.q}: erro no ciclo {ex!r}")
            time.sleep(max(0.1, P.intervalo_leitura_s - (time.time() - inicio)))


simulador: Simulador | None = None

if __name__ == "__main__":
    for tentativa in range(30):     # aguarda o broker subir
        try:
            import socket
            socket.create_connection((cfg.MQTT_HOST, cfg.MQTT_PORTA), timeout=1).close()
            break
        except OSError:
            time.sleep(1)
    simulador = Simulador()
    try:
        simulador.executar()
    except KeyboardInterrupt:
        pass
