"""Núcleo do middleware: ingestão, deduplicação, regras de negócio, comandos e resiliência.

Esta classe não conhece o MQTT: recebe mensagens por `receber()` e envia comandos pela
função `publicar(topico, payload)` passada no construtor. Isso permite testar todas as
regras sem broker (ver pasta tests/).
"""
import calendar
import re
import json
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

from . import config as cfg
from .banco import Banco
from .validacao import validar

GESTOR = "Gestor predial"
MANUTENCAO = "Manutenção"


def br(n: float) -> str:
    """Inteiro com separador de milhar (o marcador \x00 vira ponto em texto_br)."""
    return f"{n:,.0f}".replace(",", "\x00")


def texto_br(s: str) -> str:
    """Troca o ponto decimal por vírgula nas mensagens exibidas ao usuário."""
    return re.sub(r"(?<=\d)\.(?=\d)", ",", s).replace("\x00", ".")


def hhmmss(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


class Nucleo:
    def __init__(self, banco: Banco, publicar, params: cfg.Parametros = cfg.P, log=print):
        self.db = banco
        self.publicar = publicar
        self.p = params
        self.log = log
        self.lock = threading.RLock()

        # relógio do prédio: no perfil demo começa às 10h (expediente) para os testes serem previsíveis
        self.hora_simulada: int | None = 10 if cfg.PERFIL == "demo" else None
        self.meta_kwh = cfg.META_MENSAL_KWH
        self.inicio_ms = int(time.time() * 1000)
        self.consumo_mes_inicial = self._consumo_inicial_do_mes()

        self.janela = defaultdict(deque)          # circuito -> deque[(recebido_ms, leitura)] (só ao vivo)
        self.ultima = {}                          # circuito -> última leitura ao vivo
        self.visto = {}                           # circuito -> recebido_ms da última leitura ao vivo
        self.ultima_energia = {}                  # circuito -> último valor de energia aceito
        self.energia_medida = defaultdict(float)  # circuito -> kWh medidos desde o início
        self.ultima_presenca = {}                 # circuito -> último instante com presença
        self.rn02_mantido = set()                 # circuitos em que o usuário escolheu "manter ligado"
        self.desde = {}                           # chave de condição -> instante em que ficou verdadeira
        self.gw_visto = {}                        # quadro -> último instante com mensagem
        self.gw_lwt = {}                          # quadro -> 'online' | 'offline'
        self.cmd_tentativa = {}                   # comando_id -> último envio
        self.qualidade = defaultdict(deque)       # circuito -> deque[(ms, valida)]
        self.serie_total = deque(maxlen=600)      # (ms, kW)
        self.ultimo_rn04 = 0
        self.ultimo_ponto = 0
        self.stats = defaultdict(int)

    # ------------------------------------------------------------------ utilidades
    def _consumo_inicial_do_mes(self) -> float:
        """Consumo do mês antes do início do protótipo (histórico simulado: ~108% da meta)."""
        hoje = datetime.now()
        dias = calendar.monthrange(hoje.year, hoje.month)[1]
        fracao = (hoje.day - 1 + hoje.hour / 24) / dias
        return round(1.08 * cfg.META_MENSAL_KWH * fracao, 1)

    def fora_do_expediente(self, agora_ms: int) -> bool:
        dt = datetime.fromtimestamp(agora_ms / 1000)
        hora = self.hora_simulada if self.hora_simulada is not None else dt.hour
        dia_util = dt.weekday() in cfg.EXPEDIENTE_DIAS or self.hora_simulada is not None
        return not (dia_util and cfg.EXPEDIENTE_INICIO <= hora < cfg.EXPEDIENTE_FIM)

    def _janela(self, circ: str, segundos: float, agora: int):
        """Leituras ao vivo dentro da janela, e se a janela está completa."""
        dq = self.janela[circ]
        inicio = agora - segundos * 1000
        itens = [l for (t, l) in dq if t >= inicio]
        completa = bool(dq) and dq[0][0] <= inicio + self.p.intervalo_leitura_s * 1000
        return itens, completa

    def _online(self, circ: str, agora: int) -> bool:
        c = cfg.CIRC[circ]
        return (circ in self.visto and agora - self.visto[circ] <= self.p.timeout_offline_s * 1000
                and self.gw_lwt.get(c.quadro) != "offline")

    def _condicao_por(self, chave: str, verdadeira: bool, agora: int) -> float:
        """Há quantos segundos a condição está verdadeira sem interrupção (0 se falsa)."""
        if not verdadeira:
            self.desde.pop(chave, None)
            return 0.0
        self.desde.setdefault(chave, agora)
        return (agora - self.desde[chave]) / 1000

    def _notificar(self, alerta_id, canais, destino, msg):
        for canal in canais:
            self.db.notificar(alerta_id, canal, destino, msg)
            self.log(f"[NOTIFICAÇÃO {canal.upper()} → {destino}] {msg}")

    def _abrir(self, regra, severidade, alvo, titulo, detalhe, acao="", pendente=None, espera_s=None,
               canais=("push",), destino=GESTOR, agora=None):
        if self.db.alerta_aberto(regra, alvo):
            return None
        agora = agora or int(time.time() * 1000)
        executa = agora + int(espera_s * 1000) if pendente else None
        titulo, detalhe = texto_br(titulo), texto_br(detalhe)
        aid = self.db.criar_alerta(regra, severidade, alvo, titulo, detalhe, acao, pendente, executa)
        self.stats["alertas"] += 1
        self.log(f"[ALERTA {regra}] {titulo} — {detalhe}")
        self._notificar(aid, canais, destino, f"{titulo}: {detalhe}")
        return aid

    def _resolver(self, regra, alvo, nota):
        a = self.db.alerta_aberto(regra, alvo)
        nota = texto_br(nota)
        if a:
            acao = (a["acao"] + " · " if a["acao"] else "") + nota
            self.db.atualizar_alerta(a["id"], status="resolvido", acao=acao, acao_pendente=None, acao_executa_em=None)
            self.log(f"[RESOLVIDO {regra}] {alvo}: {nota}")

    # ------------------------------------------------------------------ entrada
    def receber(self, topico: str, payload: bytes, agora: int | None = None):
        agora = agora or int(time.time() * 1000)
        with self.lock:
            partes = topico.split("/")
            if len(partes) == 4 and partes[3] == "status":
                self._status(partes[2], payload, agora)
            elif len(partes) == 5 and partes[4] == "telemetria":
                self._telemetria(topico, partes[2], f"{partes[2]}-{partes[3]}", payload, agora)

    def _status(self, quadro, payload, agora):
        try:
            st = json.loads(payload).get("status")
        except Exception:
            st = payload.decode(errors="replace").strip() if isinstance(payload, bytes) else str(payload)
        if st in ("online", "offline"):
            self.gw_lwt[quadro] = st
            if st == "online":
                self.gw_visto[quadro] = agora

    def _telemetria(self, topico, quadro, circ, payload, agora):
        self.stats["recebidas"] += 1
        r = validar(payload, topico, agora, self.p.tolerancia_relogio_s)
        if circ in cfg.CIRC:
            problemas = [a for a in r.avisos if not a.startswith("ts adiantado")]
            self.qualidade[circ].append((agora, r.aceita and not problemas))
        if not r.aceita:
            self.stats["invalidas"] += 1
            self.db.inserir_invalida(topico, payload, r.motivo)
            self.log(f"[REJEITADA] {topico}: {r.motivo}")
            return
        if circ not in cfg.CIRC:
            self.db.inserir_invalida(topico, payload, "circuito não cadastrado")
            return
        l = r.leitura
        for aviso in r.avisos:
            self.stats["anomalias"] += 1
            self.db.inserir_anomalia(l["device_id"], l["seq"], texto_br(aviso))
        if l["ts_corrigido"]:
            self.stats["relogio_corrigido"] += 1

        # energia acumulada nunca diminui: se diminuir (reset do medidor), cria nova referência
        e = l.get("energia")
        if e is not None:
            ant = self.ultima_energia.get(circ)
            if ant is not None and e < ant:
                self.db.inserir_anomalia(l["device_id"], l["seq"],
                                         texto_br(f"energia caiu de {ant} para {e} kWh: nova referência criada"))
                self.stats["anomalias"] += 1
            elif ant is not None and not l["buffered"]:
                self.energia_medida[circ] += e - ant
            if not l["buffered"] or ant is None:
                self.ultima_energia[circ] = e

        l.update(circuito=circ, quadro=quadro, recebido_em=agora)
        if not self.db.inserir_leitura(l):
            self.stats["duplicadas"] += 1
            self.log(f"[DUPLICADA] {l['device_id']} seq={l['seq']} descartada")
            return
        self.stats["gravadas"] += 1
        self.gw_visto[quadro] = agora
        if l["buffered"]:
            # dado recuperado do buffer: entra no histórico, mas não dispara regras de tempo real
            self.stats["recuperadas_buffer"] += 1
            return

        # temperatura/umidade/energia chegam a cada 60 s: mantém o último valor conhecido para exibição
        anterior = self.ultima.get(circ, {})
        for k in ("temperatura", "umidade", "energia"):
            if l.get(k) is None and anterior.get(k) is not None:
                l[k] = anterior[k]
        self.janela[circ].append((agora, l))
        maxjan = max(self.p.rn01_janela_s, self.p.rn05_janela_fp_s, self.p.rn03_janela_s, 120) * 1000
        while self.janela[circ] and self.janela[circ][0][0] < agora - maxjan:
            self.janela[circ].popleft()
        self.ultima[circ] = l
        self.visto[circ] = agora
        if l.get("presenca"):
            self.ultima_presenca[circ] = agora
            self.rn02_mantido.discard(circ)
        self.ultima_presenca.setdefault(circ, agora)
        self._confirmar_comandos(circ, l, agora)
        self._regras_circuito(circ, agora)

    # ------------------------------------------------------------------ regras
    def _regras_circuito(self, circ, agora):
        c = cfg.CIRC[circ]
        l = self.ultima[circ]
        p = self.p

        # RN01 – desperdício fora do expediente
        itens, completa = self._janela(circ, p.rn01_janela_s, agora)
        pot = [x["potencia"] for x in itens if x.get("potencia") is not None]
        media = sum(pot) / len(pot) if pot else 0
        sem_presenca = not any(x.get("presenca") for x in itens)
        cond01 = (self.fora_do_expediente(agora) and completa and c.base_w > 0
                  and media > p.rn01_fator_base * c.base_w and sem_presenca and l.get("rele") is not False)
        if cond01:
            kwh = (media - c.base_w) / 1000 * (p.rn01_janela_s / 3600)
            self._abrir("RN01", "atencao", circ, f"Desperdício fora do expediente — {c.nome}",
                        f"Potência média {media:.0f} W = {media / c.base_w:.1f}× o consumo base ({c.base_w:.0f} W), "
                        f"sem presença. Energia desperdiçada na janela: {kwh * 1000:.1f} Wh",
                        acao=("Desligamento programado" if c.desligavel else "Somente notificação"),
                        pendente="OFF" if c.desligavel else None, espera_s=p.rn01_espera_desligar_s, agora=agora)
        elif not self.fora_do_expediente(agora) or (pot and media <= p.rn01_fator_base * c.base_w) \
                or not sem_presenca or l.get("rele") is False:
            self._resolver("RN01", circ, "Condição normalizada")

        # RN02 – climatização ligada em ambiente vazio
        if c.uso == "Climatização" and c.ambiente and c.desligavel:
            vazio_s = (agora - self.ultima_presenca.get(circ, agora)) / 1000
            corrente = l.get("corrente") or 0
            if (vazio_s >= p.rn02_sem_presenca_s and corrente > p.rn02_corrente_min_a
                    and circ not in self.rn02_mantido):
                self._abrir("RN02", "atencao", circ, f"Ar-condicionado ligado em ambiente vazio — {c.ambiente}",
                            f"Sem presença há {vazio_s:.0f} s com {corrente:.1f} A ({(l.get('potencia') or 0) / 1000:.1f} kW)",
                            acao="Aguardando resposta para desligar", pendente="OFF",
                            espera_s=p.rn02_espera_desligar_s, agora=agora)
            elif l.get("presenca") or corrente <= p.rn02_corrente_min_a:
                self._resolver("RN02", circ, "Presença detectada" if l.get("presenca") else "Carga desligada")

        # RN05 – fator de potência (por circuito com carga relevante)
        itens_fp, completa_fp = self._janela(circ, p.rn05_janela_fp_s, agora)
        fps = [x["fp"] for x in itens_fp if x.get("fp") is not None and (x.get("potencia") or 0) > 100]
        if completa_fp and fps:
            fp_med = sum(fps) / len(fps)
            if fp_med < p.fp_min:
                self._abrir("RN05", "atencao", f"fp:{circ}", f"Fator de potência baixo — {c.nome}",
                            f"FP médio {fp_med:.3f} < {p.fp_min:.2f}", acao="Encaminhado à manutenção",
                            canais=("email",), destino=MANUTENCAO, agora=agora)
            else:
                self._resolver("RN05", f"fp:{circ}", f"FP normalizado ({fp_med:.2f})")

        # RN05 – tensão fora da faixa adequada (por quadro)
        v = l.get("tensao")
        if v is not None:
            fora = not (p.tensao_min_v <= v <= p.tensao_max_v)
            dur = self._condicao_por(f"tensao:{c.quadro}", fora, agora)
            if fora and dur >= p.rn05_janela_tensao_s:
                self._abrir("RN05", "atencao", f"tensao:{c.quadro}",
                            f"Tensão fora da faixa — {cfg.QUADROS[c.quadro].nome}",
                            f"{v:.1f} V fora de {p.tensao_min_v:.0f}–{p.tensao_max_v:.0f} V há {dur:.0f} s",
                            acao="Encaminhado à manutenção", canais=("email",), destino=MANUTENCAO, agora=agora)
            elif not fora:
                self._resolver("RN05", f"tensao:{c.quadro}", f"Tensão normalizada ({v:.1f} V)")

    def _corrente_quadro(self, quadro, agora):
        return sum((self.ultima[ci].get("corrente") or 0) for ci in cfg.QUADROS[quadro].circuitos
                   if ci in self.ultima and self._online(ci, agora))

    def _potencia_total(self, agora):
        return sum((self.ultima[ci].get("potencia") or 0) for ci in self.ultima if self._online(ci, agora)) / 1000

    def _rn03(self, agora):
        p = self.p
        for q, quadro in cfg.QUADROS.items():
            total = self._corrente_quadro(q, agora)
            lim = p.rn03_limite * quadro.disjuntor_geral_a
            dur = self._condicao_por(f"rn03:{q}", total > lim, agora)
            aberto = self.db.alerta_aberto("RN03", q)
            if dur >= p.rn03_janela_s and not aberto:
                self._abrir("RN03", "critico", q, f"Sobrecarga — {quadro.nome}",
                            f"Corrente total {total:.1f} A > {p.rn03_limite:.0%} do disjuntor geral "
                            f"({quadro.disjuntor_geral_a:.0f} A) por {dur:.0f} s",
                            acao="Corte de cargas não essenciais iniciado", canais=("push", "email"),
                            destino=f"{GESTOR} e {MANUTENCAO}", agora=agora)
                aberto = self.db.alerta_aberto("RN03", q)
            if aberto:
                if total > p.rn03_retorno * quadro.disjuntor_geral_a:
                    self._cortar_carga(q, aberto, agora)
                else:
                    self._resolver("RN03", q, f"Corrente normalizada ({total:.1f} A)")

        # demanda contratada do prédio
        kw = self._potencia_total(agora)
        dur = self._condicao_por("rn03:predio", kw > cfg.DEMANDA_CONTRATADA_KW, agora)
        if dur >= p.rn03_janela_s:
            self._abrir("RN03", "critico", "predio", "Demanda contratada ultrapassada",
                        f"Potência total {kw:.1f} kW > {cfg.DEMANDA_CONTRATADA_KW:.0f} kW por {dur:.0f} s",
                        canais=("push", "email"), destino=f"{GESTOR} e {MANUTENCAO}", agora=agora)
        elif kw <= cfg.DEMANDA_CONTRATADA_KW:
            self._resolver("RN03", "predio", f"Demanda normalizada ({kw:.1f} kW)")

    def _cortar_carga(self, quadro, alerta, agora):
        """Desliga uma carga por vez, da menor prioridade para a maior, nunca as essenciais."""
        pendentes = self.db.q("SELECT 1 FROM comandos WHERE status='aguardando' AND alerta_id=?", (alerta["id"],))
        if pendentes:
            return
        candidatos = [cfg.CIRC[ci] for ci in cfg.QUADROS[quadro].circuitos
                      if cfg.CIRC[ci].desligavel and cfg.CIRC[ci].prioridade > 1
                      and self.ultima.get(ci, {}).get("rele") and (self.ultima[ci].get("corrente") or 0) > 0.5]
        if not candidatos:
            return
        alvo = sorted(candidatos, key=lambda c: (-c.prioridade, -(self.ultima[c.id].get("corrente") or 0)))[0]
        self.comando(alvo.id, "OFF", "RN03", alerta["id"])
        self.db.atualizar_alerta(alerta["id"], acao=f"{alerta['acao']} · {alvo.nome} desligado às {hhmmss(agora)}")

    def _rn04(self, agora):
        p = self.p
        if agora - self.ultimo_rn04 < p.rn04_intervalo_s * 1000:
            return
        self.ultimo_rn04 = agora
        hoje = datetime.fromtimestamp(agora / 1000)
        proj = self.projecao_mes(agora)
        if hoje.day >= p.rn04_dia_minimo and proj > p.rn04_limite * self.meta_kwh:
            top = sorted(self.energia_medida.items(), key=lambda kv: -kv[1])[:5]
            nomes = ", ".join(cfg.CIRC[k].nome for k, _ in top) or "—"
            self._abrir("RN04", "info", "predio", "Meta mensal em risco",
                        f"Projeção {br(proj)} kWh = {proj / self.meta_kwh:.0%} da meta ({br(self.meta_kwh)} kWh). "
                        f"Maiores consumidores: {nomes}",
                        acao="Relatório enviado ao gestor", canais=("email",), agora=agora)
        elif proj <= p.rn04_limite * self.meta_kwh:
            self._resolver("RN04", "predio", "Projeção dentro da meta")

    def projecao_mes(self, agora):
        hoje = datetime.fromtimestamp(agora / 1000)
        dias = calendar.monthrange(hoje.year, hoje.month)[1]
        fracao = max((hoje.day - 1 + hoje.hour / 24 + hoje.minute / 1440) / dias, 1 / dias)
        consumo = self.consumo_mes_inicial + sum(self.energia_medida.values())
        return consumo / fracao

    # ------------------------------------------------------------------ comandos
    def comando(self, circ, acao, origem, alerta_id=None):
        c = cfg.CIRC[circ]
        if acao == "OFF" and not c.desligavel:
            raise ValueError(f"{c.nome} é carga essencial e não pode ser desligada remotamente")
        with self.lock:
            cid = self.db.criar_comando(circ, acao, origem, alerta_id)
            self._enviar_comando(cid, c, acao)
            self.log(f"[COMANDO {acao}] {c.nome} (origem: {origem})")
            return cid

    def _enviar_comando(self, cid, c, acao):
        self.cmd_tentativa[cid] = int(time.time() * 1000)
        self.publicar(cfg.topico_cmd(c.quadro, c.id.split("-")[1]),
                      json.dumps({"cmd_id": cid, "acao": acao}))

    def _confirmar_comandos(self, circ, l, agora):
        if l.get("rele") is None:
            return
        for cmd in self.db.q("SELECT * FROM comandos WHERE circuito=? AND status='aguardando'", (circ,)):
            if (cmd["acao"] == "ON") == bool(l["rele"]):
                self.db.x("UPDATE comandos SET status='confirmado', confirmado_em=? WHERE id=?", (agora, cmd["id"]))
                self.cmd_tentativa.pop(cmd["id"], None)

    def _verificar_comandos(self, agora):
        for cmd in self.db.q("SELECT * FROM comandos WHERE status='aguardando'"):
            ult = self.cmd_tentativa.get(cmd["id"], cmd["enviado_em"])
            if agora - ult < self.p.confirmacao_comando_s * 1000:
                continue
            c = cfg.CIRC[cmd["circuito"]]
            if cmd["tentativas"] < self.p.tentativas_comando:
                self.db.x("UPDATE comandos SET tentativas=tentativas+1 WHERE id=?", (cmd["id"],))
                self._enviar_comando(cmd["id"], c, cmd["acao"])
                self.log(f"[REENVIO] comando {cmd['acao']} para {c.nome} (tentativa {cmd['tentativas'] + 1})")
            else:
                self.db.x("UPDATE comandos SET status='falhou' WHERE id=?", (cmd["id"],))
                self.cmd_tentativa.pop(cmd["id"], None)
                self._abrir("CMD", "sistema", c.id, f"Comando não executado — {c.nome}",
                            f"Relé não confirmou {cmd['acao']} após {self.p.tentativas_comando} tentativas",
                            acao="Escalado para a manutenção", canais=("email",), destino=MANUTENCAO, agora=agora)

    def _executar_pendentes(self, agora):
        for a in self.db.q("SELECT * FROM alertas WHERE acao_pendente IS NOT NULL AND status!='resolvido' "
                           "AND acao_executa_em<=?", (agora,)):
            self.db.atualizar_alerta(a["id"], acao_pendente=None, acao_executa_em=None,
                                     acao=f"Relé desligado automaticamente às {hhmmss(agora)}")
            if self.ultima.get(a["alvo"], {}).get("rele") is not False:
                self.comando(a["alvo"], a["acao_pendente"], a["regra"], a["id"])

    def cancelar_acao(self, alerta_id):
        """Usuário escolheu 'Manter ligado'."""
        with self.lock:
            a = self.db.alerta(alerta_id)
            if not a or not a["acao_pendente"]:
                return False
            self.db.atualizar_alerta(alerta_id, acao_pendente=None, acao_executa_em=None, status="reconhecido",
                                     acao="Desligamento cancelado pelo usuário (manter ligado)")
            if a["regra"] == "RN02":
                self.rn02_mantido.add(a["alvo"])
            return True

    # ------------------------------------------------------------------ verificações periódicas
    def tick(self, agora: int | None = None):
        agora = agora or int(time.time() * 1000)
        with self.lock:
            self._offline(agora)
            self._rn03(agora)
            self._rn04(agora)
            self._executar_pendentes(agora)
            self._verificar_comandos(agora)
            self._qualidade_dados(agora)
            if self.ultima and agora - self.ultimo_ponto >= 2000:
                self.serie_total.append((agora, round(self._potencia_total(agora), 2)))
                self.ultimo_ponto = agora

    def _offline(self, agora):
        for q, quadro in cfg.QUADROS.items():
            if q not in self.gw_visto:
                continue
            silencio = (agora - self.gw_visto[q]) / 1000
            off = self.gw_lwt.get(q) == "offline" or silencio > self.p.timeout_offline_s
            if off:
                motivo = "LWT “offline” recebido" if self.gw_lwt.get(q) == "offline" else f"sem mensagens há {silencio:.0f} s"
                self._abrir("OFFLINE", "sistema", q, f"Gateway offline — {quadro.nome}",
                            f"{motivo}. Regras dos circuitos deste quadro suspensas.",
                            acao="Chamado aberto para a manutenção", canais=("email",), destino=MANUTENCAO,
                            agora=agora)
            else:
                self._resolver("OFFLINE", q, f"Gateway voltou às {hhmmss(agora)}")

    def _qualidade_dados(self, agora):
        p = self.p
        for circ, dq in self.qualidade.items():
            while dq and dq[0][0] < agora - p.invalidas_janela_s * 1000:
                dq.popleft()
            if len(dq) < p.invalidas_minimo:
                continue
            taxa = sum(1 for _, ok in dq if not ok) / len(dq)
            if taxa > p.invalidas_limite:
                self._abrir("DADOS", "sistema", circ, f"Leituras inválidas acima do limite — {cfg.CIRC[circ].nome}",
                            f"{taxa:.0%} das leituras com problema na janela de {p.invalidas_janela_s:.0f} s",
                            acao="Verificar sensor e cabeamento", canais=("email",), destino=MANUTENCAO, agora=agora)
            else:
                self._resolver("DADOS", circ, f"Taxa de leituras inválidas normalizada ({taxa:.0%})")

    # ------------------------------------------------------------------ estado para a API
    def estado(self, agora: int | None = None) -> dict:
        agora = agora or int(time.time() * 1000)
        with self.lock:
            circuitos = []
            for c in cfg.CIRCUITOS:
                l = self.ultima.get(c.id, {})
                online = self._online(c.id, agora)
                vazio = (agora - self.ultima_presenca[c.id]) / 1000 if c.id in self.ultima_presenca else None
                circuitos.append({
                    "id": c.id, "nome": c.nome, "quadro": c.quadro, "uso": c.uso, "disjuntor_a": c.disjuntor_a,
                    "prioridade": c.prioridade, "desligavel": c.desligavel, "ambiente": c.ambiente,
                    "online": online, "visto_ha_s": round((agora - self.visto[c.id]) / 1000, 1) if c.id in self.visto else None,
                    "tensao": l.get("tensao"), "corrente": l.get("corrente"), "potencia": l.get("potencia"),
                    "fp": l.get("fp"), "presenca": l.get("presenca"), "rele": l.get("rele"), "rssi": l.get("rssi"),
                    "temperatura": l.get("temperatura"), "umidade": l.get("umidade"),
                    "sem_presenca_s": round(vazio) if vazio is not None and not l.get("presenca") else 0,
                    "energia_kwh": round(self.energia_medida[c.id], 4),
                })
            quadros = [{"id": q, "nome": Q.nome, "disjuntor_a": Q.disjuntor_geral_a,
                        "corrente": round(self._corrente_quadro(q, agora), 1),
                        "online": q in self.gw_visto and not self.db.alerta_aberto("OFFLINE", q)}
                       for q, Q in cfg.QUADROS.items()]
            uso = defaultdict(float)
            for c in circuitos:
                if c["online"] and c["potencia"]:
                    uso[c["uso"]] += c["potencia"]
            temps = [c["temperatura"] for c in circuitos if c["temperatura"] is not None and c["online"]]
            return {
                "agora": agora, "perfil": cfg.PERFIL, "predio": cfg.NOME_PREDIO,
                "hora_simulada": self.hora_simulada, "fora_expediente": self.fora_do_expediente(agora),
                "potencia_kw": round(self._potencia_total(agora), 2),
                "demanda_kw": cfg.DEMANDA_CONTRATADA_KW,
                "meta_kwh": self.meta_kwh, "projecao_kwh": round(self.projecao_mes(agora), 0),
                "temperatura_media": round(sum(temps) / len(temps), 1) if temps else None,
                "circuitos": circuitos, "quadros": quadros, "uso_final": dict(uso),
                "serie": list(self.serie_total)[-150:],
                "stats": dict(self.stats),
            }
