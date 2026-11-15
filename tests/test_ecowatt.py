"""Testes automatizados do EcoWatt: regras de negócio (RN01–RN05) e tratamento de falhas (Tabela 2).

Executar:  python -m pytest -v
Os testes usam o núcleo do middleware diretamente, sem broker e sem simulador.
"""
import json
import time

import pytest

from ecowatt import config as cfg
from ecowatt.banco import Banco
from ecowatt.nucleo import Nucleo
from ecowatt.validacao import validar

P = cfg.PERFIS["demo"]
DT = int(P.intervalo_leitura_s * 1000)


class Ambiente:
    """Núcleo isolado com banco temporário e relógio controlado."""

    def __init__(self, tmp_path):
        self.db = Banco(str(tmp_path / "teste.db"))
        self.publicados = []
        self.n = Nucleo(self.db, lambda t, p: self.publicados.append((t, json.loads(p))), params=P,
                        log=lambda *_: None)
        self.agora = int(time.time() * 1000)
        self.seq = {}
        self.estado = {}   # circuito -> dict com valores padrão

    def msg(self, circ, **kw):
        c = cfg.CIRC[circ]
        self.seq[circ] = self.seq.get(circ, 100) + 1
        base = {"device_id": c.device_id, "ts": self.agora, "seq": self.seq[circ], "tensao": 220.0,
                "corrente": 5.0, "potencia": 1045.0, "fp": 0.95, "frequencia": 60.0, "presenca": True,
                "rele": True, "rssi": -60, "buffered": False}
        base.update(self.estado.get(circ, {}))
        base.update(kw)
        return base

    def enviar(self, circ, payload=None, **kw):
        c = cfg.CIRC[circ]
        m = payload if payload is not None else json.dumps(self.msg(circ, **kw))
        self.n.receber(cfg.topico_telemetria(c.quadro, circ.split("-")[1]), m, self.agora)

    def avancar(self, segundos, circuitos=(), **kw):
        """Avança o relógio publicando leituras periódicas dos circuitos indicados."""
        fim = self.agora + int(segundos * 1000)
        while self.agora < fim:
            self.agora += DT
            for ci in circuitos:
                self.enviar(ci, **kw.get(ci, {}))
            self.n.tick(self.agora)

    def alertas(self, regra=None, status=None):
        rs = self.db.q("SELECT * FROM alertas ORDER BY id")
        return [a for a in rs if (regra is None or a["regra"] == regra) and (status is None or a["status"] == status)]

    def comandos(self):
        return [p for t, p in self.publicados if t.endswith("/cmd")]


@pytest.fixture
def amb(tmp_path):
    return Ambiente(tmp_path)


# ------------------------------------------------------------------ validação
class TestValidacao:
    T = "ecowatt/predio01/qd02/c01/telemetria"

    def ok(self, **kw):
        m = {"device_id": "esp32-qd02-c01", "ts": 1_000_000, "seq": 1, "tensao": 220.0, "corrente": 5.0,
             "potencia": 1000.0, "fp": 0.95, "frequencia": 60.0, "rele": True}
        m.update(kw)
        return json.dumps(m)

    def test_mensagem_valida(self):
        r = validar(self.ok(), self.T, 1_000_000)
        assert r.aceita and not r.avisos

    def test_json_invalido_rejeitado(self):
        r = validar('{"device_id": "esp32-qd02-c01", ', self.T, 0)
        assert not r.aceita and r.motivo == "JSON inválido"

    def test_campo_obrigatorio_ausente(self):
        m = json.loads(self.ok())
        del m["seq"]
        assert validar(json.dumps(m), self.T, 1_000_000).motivo == "campo obrigatório ausente: seq"

    def test_device_id_diferente_do_topico(self):
        assert not validar(self.ok(device_id="esp32-qd09-c01"), self.T, 1_000_000).aceita

    def test_nulo_gravado_como_ausente_nunca_zero(self):
        r = validar(self.ok(tensao=None), self.T, 1_000_000)
        assert r.aceita and r.leitura["tensao"] is None

    def test_fora_da_faixa_descartado(self):
        r = validar(self.ok(corrente=500.0), self.T, 1_000_000)
        assert r.aceita and r.leitura["corrente"] is None and "fora da faixa" in r.avisos[0]

    def test_tipo_errado_descartado(self):
        r = validar(self.ok(potencia="alta"), self.T, 1_000_000)
        assert r.aceita and r.leitura["potencia"] is None

    def test_relogio_adiantado_corrigido(self):
        r = validar(self.ok(ts=1_000_000 + 60_000), self.T, 1_000_000)
        assert r.leitura["ts"] == 1_000_000 and r.leitura["ts_corrigido"]

    def test_sem_nenhuma_medida_eletrica(self):
        r = validar(self.ok(tensao=None, corrente=None, potencia=None), self.T, 1_000_000)
        assert not r.aceita


# ------------------------------------------------------------------ ingestão e resiliência
class TestResiliencia:
    def test_duplicata_descartada(self, amb):
        m = json.dumps(amb.msg("qd02-c01"))
        amb.enviar("qd02-c01", m)
        amb.enviar("qd02-c01", m)
        assert amb.n.stats["duplicadas"] == 1
        assert amb.db.q("SELECT COUNT(*) n FROM leituras")[0]["n"] == 1

    def test_mensagem_invalida_vai_para_fila(self, amb):
        amb.enviar("qd02-c01", "isto não é json")
        assert amb.db.q("SELECT motivo FROM mensagens_invalidas")[0]["motivo"] == "JSON inválido"

    def test_buffer_gravado_sem_disparar_regras(self, amb):
        amb.n.hora_simulada = 2   # madrugada
        amb.estado["qd02-c02"] = {"potencia": 900.0, "presenca": False}
        for _ in range(20):
            amb.enviar("qd02-c02", buffered=True, ts=amb.agora - 300_000)
            amb.agora += DT
        assert amb.n.stats["recuperadas_buffer"] == 20
        assert amb.alertas("RN01") == []

    def test_energia_menor_cria_nova_referencia(self, amb):
        amb.enviar("qd02-c01", energia=1500.0)
        amb.enviar("qd02-c01", energia=1500.5)
        amb.enviar("qd02-c01", energia=0.1)          # medidor resetado
        amb.enviar("qd02-c01", energia=0.3)
        assert amb.n.energia_medida["qd02-c01"] == pytest.approx(0.7)   # 0,5 + 0,2, nunca negativo
        assert amb.db.q("SELECT COUNT(*) n FROM anomalias WHERE descricao LIKE 'energia caiu%'")[0]["n"] == 1

    def test_gateway_offline_por_timeout_e_retorno(self, amb):
        amb.avancar(4, ["qd00-c01"])
        amb.avancar(P.timeout_offline_s + 4)          # silêncio
        assert len(amb.alertas("OFFLINE", "aberto")) == 1
        amb.avancar(4, ["qd00-c01"])
        assert amb.alertas("OFFLINE")[0]["status"] == "resolvido"

    def test_gateway_offline_por_lwt(self, amb):
        amb.avancar(2, ["qd00-c01"])
        amb.n.receber("ecowatt/predio01/qd00/status", b'{"status": "offline"}', amb.agora)
        amb.n.tick(amb.agora)
        assert "LWT" in amb.alertas("OFFLINE")[0]["detalhe"]

    def test_comando_sem_confirmacao_reenvia_e_escala(self, amb):
        amb.avancar(2, ["qd03-c02"])
        amb.n.comando("qd03-c02", "OFF", "teste")
        for _ in range(4):                            # relé continua ligado: nunca confirma
            amb.agora += int(P.confirmacao_comando_s * 1000) + 1000
            amb.n.cmd_tentativa = {k: amb.agora - int(P.confirmacao_comando_s * 1000) - 1 for k in amb.n.cmd_tentativa}
            amb.n.tick(amb.agora)
        cmd = amb.db.q("SELECT * FROM comandos")[0]
        assert cmd["tentativas"] == 3 and cmd["status"] == "falhou"
        assert len(amb.alertas("CMD")) == 1

    def test_comando_confirmado_pela_telemetria(self, amb):
        amb.n.comando("qd02-c01", "OFF", "teste")
        amb.enviar("qd02-c01", rele=False, corrente=0.0, potencia=0.0)
        assert amb.db.q("SELECT status FROM comandos")[0]["status"] == "confirmado"

    def test_carga_essencial_nunca_desligada(self, amb):
        with pytest.raises(ValueError):
            amb.n.comando("qd01-c01", "OFF", "teste")

    def test_taxa_de_invalidas_gera_alerta(self, amb):
        for i in range(30):
            amb.agora += DT
            if i % 5 == 0:
                amb.enviar("qd01-c01", corrente=999.0)
            else:
                amb.enviar("qd01-c01")
        amb.n.tick(amb.agora)
        assert len(amb.alertas("DADOS", "aberto")) == 1


# ------------------------------------------------------------------ regras de negócio
class TestRegras:
    def test_rn01_desperdicio_fora_do_expediente(self, amb):
        amb.n.hora_simulada = 2
        amb.estado["qd02-c02"] = {"potencia": 345.0, "corrente": 1.65, "presenca": False}   # 2,3x a base (150 W)
        amb.avancar(P.rn01_janela_s + 4, ["qd02-c02"])
        a = amb.alertas("RN01", "aberto")
        assert len(a) == 1 and a[0]["acao_pendente"] == "OFF"
        amb.avancar(P.rn01_espera_desligar_s + 2, ["qd02-c02"])
        assert {"cmd_id": 1, "acao": "OFF"} in amb.comandos()

    def test_rn01_nao_dispara_no_expediente(self, amb):
        amb.n.hora_simulada = 10
        amb.estado["qd02-c02"] = {"potencia": 345.0, "presenca": False}
        amb.avancar(P.rn01_janela_s + 4, ["qd02-c02"])
        assert amb.alertas("RN01") == []

    def test_rn01_nao_dispara_com_presenca(self, amb):
        amb.n.hora_simulada = 2
        amb.estado["qd02-c02"] = {"potencia": 345.0, "presenca": True}
        amb.avancar(P.rn01_janela_s + 4, ["qd02-c02"])
        assert amb.alertas("RN01") == []

    def test_rn02_sala_vazia_desliga_apos_espera(self, amb):
        amb.estado["qd02-c01"] = {"corrente": 10.9, "potencia": 2300.0, "presenca": False}
        amb.avancar(P.rn02_sem_presenca_s + 4, ["qd02-c01"])
        assert len(amb.alertas("RN02", "aberto")) == 1
        amb.avancar(P.rn02_espera_desligar_s + 2, ["qd02-c01"])
        assert any(c["acao"] == "OFF" for c in amb.comandos())

    def test_rn02_manter_ligado_cancela(self, amb):
        amb.estado["qd02-c01"] = {"corrente": 10.9, "potencia": 2300.0, "presenca": False}
        amb.avancar(P.rn02_sem_presenca_s + 4, ["qd02-c01"])
        assert amb.n.cancelar_acao(amb.alertas("RN02")[0]["id"])
        amb.avancar(P.rn02_espera_desligar_s + 4, ["qd02-c01"])
        assert amb.comandos() == []

    def test_rn02_corrente_baixa_nao_dispara(self, amb):
        amb.estado["qd02-c01"] = {"corrente": 1.5, "potencia": 300.0, "presenca": False}   # standby
        amb.avancar(P.rn02_sem_presenca_s + 4, ["qd02-c01"])
        assert amb.alertas("RN02") == []

    def test_rn03_sobrecarga_corta_menor_prioridade(self, amb):
        amb.estado["qd03-c01"] = {"corrente": 30.0, "potencia": 6300.0}
        amb.estado["qd03-c02"] = {"corrente": 16.0, "potencia": 3500.0}    # total 46 A > 45 A
        amb.avancar(P.rn03_janela_s + 4, ["qd03-c01", "qd03-c02"])
        assert len(amb.alertas("RN03", "aberto")) == 1
        cmds = amb.db.q("SELECT circuito, acao FROM comandos")
        assert cmds == [{"circuito": "qd03-c02", "acao": "OFF"}]          # prioridade 3 sai primeiro
        amb.estado["qd03-c02"] = {"corrente": 0.0, "potencia": 0.0, "rele": False}
        amb.avancar(4, ["qd03-c01", "qd03-c02"])
        assert amb.alertas("RN03")[0]["status"] == "resolvido"            # 30 A < 80% de 50 A

    def test_rn03_ignora_pico_curto(self, amb):
        amb.estado["qd03-c01"] = {"corrente": 30.0}
        amb.estado["qd03-c02"] = {"corrente": 16.0}
        amb.avancar(P.rn03_janela_s / 2, ["qd03-c01", "qd03-c02"])
        amb.estado["qd03-c02"] = {"corrente": 0.5}
        amb.avancar(P.rn03_janela_s, ["qd03-c01", "qd03-c02"])
        assert amb.alertas("RN03") == []

    def test_rn04_meta_em_risco(self, amb):
        amb.n.meta_kwh = 3600.0                  # projeção inicial ~4.212 kWh = 117%
        amb.n.tick(amb.agora)
        a = amb.alertas("RN04", "aberto")
        assert len(a) == 1 and "Maiores consumidores" in a[0]["detalhe"]

    def test_rn04_dentro_da_meta(self, amb):
        amb.n.tick(amb.agora)                    # meta padrão 3.900 kWh: ~108% < 110%
        assert amb.alertas("RN04") == []

    def test_rn05_tensao_fora_da_faixa(self, amb):
        amb.estado["qd02-c01"] = {"tensao": 196.0}
        amb.avancar(P.rn05_janela_tensao_s + 4, ["qd02-c01"])
        assert len(amb.alertas("RN05", "aberto")) == 1

    def test_rn05_fator_de_potencia_baixo(self, amb):
        amb.estado["qd02-c01"] = {"fp": 0.84, "potencia": 2000.0}
        amb.avancar(P.rn05_janela_fp_s + 4, ["qd02-c01"])
        assert any(a["alvo"] == "fp:qd02-c01" for a in amb.alertas("RN05"))
