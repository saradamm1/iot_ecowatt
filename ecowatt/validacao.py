"""Validação da telemetria recebida (Tabela 1 e Tabela 2 do documento)."""
import json
import re
from dataclasses import dataclass, field

# campo -> (tipos aceitos, faixa mínima, faixa máxima)
NUMERICOS = {
    "tensao": (80.0, 260.0),
    "corrente": (0.0, 100.0),
    "potencia": (0.0, 23000.0),
    "energia": (0.0, 1e9),
    "fp": (0.0, 1.0),
    "frequencia": (45.0, 65.0),
    "temperatura": (-40.0, 80.0),
    "umidade": (0.0, 100.0),
    "rssi": (-100, 0),
}
BOOLEANOS = ("presenca", "rele", "buffered")
OBRIGATORIOS_ID = ("device_id", "ts", "seq")
OBRIGATORIOS_MEDIDA = ("tensao", "corrente", "potencia", "fp", "frequencia", "rele")
PADRAO_DEVICE = re.compile(r"^esp32-qd\d{2}-c\d{2}$")


@dataclass
class Resultado:
    aceita: bool
    leitura: dict | None = None
    motivo: str = ""                       # motivo da rejeição
    avisos: list = field(default_factory=list)  # campos descartados, relógio corrigido etc.


def validar(payload: bytes | str, topico: str, agora_ms: int, tolerancia_s: float = 5.0) -> Resultado:
    """Valida uma mensagem. Nunca lança exceção: devolve aceita=False com o motivo."""
    # 1) JSON válido
    try:
        dados = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return Resultado(False, motivo="JSON inválido")
    if not isinstance(dados, dict):
        return Resultado(False, motivo="payload não é um objeto JSON")

    # 2) identificação obrigatória
    for campo in OBRIGATORIOS_ID:
        if dados.get(campo) is None:
            return Resultado(False, motivo=f"campo obrigatório ausente: {campo}")
    dev = dados["device_id"]
    if not isinstance(dev, str) or not PADRAO_DEVICE.match(dev):
        return Resultado(False, motivo=f"device_id fora do padrão: {dev!r}")
    partes = topico.split("/")
    if len(partes) >= 4 and f"esp32-{partes[2]}-{partes[3]}" != dev:
        return Resultado(False, motivo="device_id não corresponde ao tópico")
    if isinstance(dados["ts"], bool) or not isinstance(dados["ts"], int):
        return Resultado(False, motivo="ts deve ser inteiro (epoch em ms)")
    if isinstance(dados["seq"], bool) or not isinstance(dados["seq"], int) or dados["seq"] < 0:
        return Resultado(False, motivo="seq deve ser inteiro não negativo")

    leitura = {"device_id": dev, "ts": dados["ts"], "seq": dados["seq"], "ts_corrigido": False}
    avisos = []

    # 3) relógio adiantado: usa o horário do servidor
    if dados["ts"] > agora_ms + tolerancia_s * 1000:
        leitura["ts"] = agora_ms
        leitura["ts_corrigido"] = True
        avisos.append("ts adiantado: substituído pelo horário do servidor")

    # 4) grandezas numéricas: tipo e faixa física
    for campo, (vmin, vmax) in NUMERICOS.items():
        v = dados.get(campo)
        if v is None:
            leitura[campo] = None          # ausente, nunca zero
            if campo in OBRIGATORIOS_MEDIDA:
                avisos.append(f"{campo} nulo: gravado como ausente")
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            leitura[campo] = None
            avisos.append(f"{campo} com tipo inválido: descartado")
            continue
        if not (vmin <= v <= vmax):
            leitura[campo] = None
            avisos.append(f"{campo}={v} fora da faixa ({vmin:g} a {vmax:g}): descartado")
            continue
        leitura[campo] = float(v) if campo != "rssi" else int(v)

    for campo in BOOLEANOS:
        v = dados.get(campo)
        if v is None or isinstance(v, bool):
            leitura[campo] = v
        else:
            leitura[campo] = None
            avisos.append(f"{campo} com tipo inválido: descartado")
    if leitura["buffered"] is None:
        leitura["buffered"] = False

    # 5) se todas as medidas elétricas ficaram vazias, a mensagem não tem utilidade
    if all(leitura.get(c) is None for c in ("tensao", "corrente", "potencia")):
        return Resultado(False, motivo="nenhuma medida elétrica válida", avisos=avisos)

    return Resultado(True, leitura=leitura, avisos=avisos)
