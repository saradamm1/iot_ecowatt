"""Configuração do protótipo EcoWatt.

Há dois perfis de tempo:
- "demo" (padrão): janelas curtas para mostrar as regras disparando em poucos segundos;
- "real": valores especificados no documento (Parte A).
Escolha com a variável de ambiente ECOWATT_PERFIL=real.
"""
import os
from dataclasses import dataclass, field

PREDIO = "predio01"
NOME_PREDIO = "Edifício Comercial Central"

MQTT_HOST = os.environ.get("ECOWATT_MQTT_HOST", "127.0.0.1")
MQTT_PORTA = int(os.environ.get("ECOWATT_MQTT_PORTA", "1883"))
API_PORTA = int(os.environ.get("ECOWATT_API_PORTA", "8000"))
BANCO = os.environ.get("ECOWATT_BANCO", "ecowatt.db")

PERFIL = os.environ.get("ECOWATT_PERFIL", "demo")


@dataclass
class Parametros:
    intervalo_leitura_s: float      # período de publicação do gateway
    intervalo_ambiente_s: float     # DHT22 e energia acumulada
    timeout_offline_s: float        # sem mensagens -> gateway offline
    rn01_janela_s: float            # consumo alto fora do expediente
    rn01_fator_base: float
    rn01_espera_desligar_s: float
    rn02_sem_presenca_s: float      # sala vazia
    rn02_corrente_min_a: float
    rn02_espera_desligar_s: float
    rn03_janela_s: float            # sobrecarga
    rn03_limite: float              # 90% do disjuntor
    rn03_retorno: float             # parar o corte abaixo de 80%
    rn04_dia_minimo: int            # meta mensal
    rn04_limite: float
    rn04_intervalo_s: float
    rn05_janela_tensao_s: float     # qualidade de energia
    rn05_janela_fp_s: float
    tensao_min_v: float = 202.0
    tensao_max_v: float = 231.0
    fp_min: float = 0.92
    confirmacao_comando_s: float = 10.0
    tentativas_comando: int = 3
    tolerancia_relogio_s: float = 5.0
    invalidas_janela_s: float = 3600.0
    invalidas_limite: float = 0.05
    invalidas_minimo: int = 20


PERFIS = {
    "real": Parametros(
        intervalo_leitura_s=5, intervalo_ambiente_s=60, timeout_offline_s=180,
        rn01_janela_s=15 * 60, rn01_fator_base=1.5, rn01_espera_desligar_s=10 * 60,
        rn02_sem_presenca_s=30 * 60, rn02_corrente_min_a=2.0, rn02_espera_desligar_s=5 * 60,
        rn03_janela_s=60, rn03_limite=0.90, rn03_retorno=0.80,
        rn04_dia_minimo=7, rn04_limite=1.10, rn04_intervalo_s=24 * 3600,
        rn05_janela_tensao_s=5 * 60, rn05_janela_fp_s=15 * 60,
    ),
    "demo": Parametros(
        intervalo_leitura_s=2, intervalo_ambiente_s=10, timeout_offline_s=12,
        rn01_janela_s=16, rn01_fator_base=1.5, rn01_espera_desligar_s=20,
        rn02_sem_presenca_s=16, rn02_corrente_min_a=2.0, rn02_espera_desligar_s=20,
        rn03_janela_s=8, rn03_limite=0.90, rn03_retorno=0.80,
        rn04_dia_minimo=1, rn04_limite=1.10, rn04_intervalo_s=10,
        rn05_janela_tensao_s=10, rn05_janela_fp_s=12,
        invalidas_janela_s=120, invalidas_minimo=10,
    ),
}
P = PERFIS.get(PERFIL, PERFIS["demo"])

# Expediente: seg-sex, 07h às 19h
EXPEDIENTE_DIAS = {0, 1, 2, 3, 4}
EXPEDIENTE_INICIO = 7
EXPEDIENTE_FIM = 19

DEMANDA_CONTRATADA_KW = 25.0
META_MENSAL_KWH = 3900.0
TARIFA_R_KWH = 0.85


@dataclass
class Circuito:
    id: str            # ex.: qd02-c01
    nome: str
    quadro: str
    uso: str
    disjuntor_a: float
    prioridade: int    # 1 = essencial, 3 = baixa
    desligavel: bool
    base_w: float      # consumo base (standby) usado na RN01
    ambiente: str | None = None   # ambiente com sensor de presença

    @property
    def device_id(self) -> str:
        return f"esp32-{self.id}"


@dataclass
class Quadro:
    id: str
    nome: str
    disjuntor_geral_a: float
    circuitos: list = field(default_factory=list)


CIRCUITOS = [
    Circuito("qd00-c01", "Iluminação garagem", "qd00", "Iluminação", 16, 2, True, 750, None),
    Circuito("qd01-c01", "Servidores / rack", "qd01", "TI crítica", 25, 1, False, 1700, None),
    Circuito("qd01-c02", "Tomadas TI 1º andar", "qd01", "Tomadas / TI", 25, 2, False, 250, "1º andar"),
    Circuito("qd02-c01", "AC sala 204", "qd02", "Climatização", 20, 2, True, 60, "Sala 204"),
    Circuito("qd02-c02", "Iluminação 2º andar", "qd02", "Iluminação", 16, 2, True, 150, "2º andar"),
    Circuito("qd03-c01", "AC 3º andar", "qd03", "Climatização", 32, 2, True, 80, "3º andar"),
    Circuito("qd03-c02", "Aquecedor copa", "qd03", "Cozinha", 20, 3, True, 0, "Copa"),
]
CIRC = {c.id: c for c in CIRCUITOS}
QUADROS = {
    "qd00": Quadro("qd00", "QD-00 Garagem", 40),
    "qd01": Quadro("qd01", "QD-01 1º andar", 50),
    "qd02": Quadro("qd02", "QD-02 2º andar", 50),
    "qd03": Quadro("qd03", "QD-03 3º andar", 50),
}
for _c in CIRCUITOS:
    QUADROS[_c.quadro].circuitos.append(_c.id)


def topico_telemetria(quadro: str, circuito: str) -> str:
    return f"ecowatt/{PREDIO}/{quadro}/{circuito}/telemetria"


def topico_status(quadro: str) -> str:
    return f"ecowatt/{PREDIO}/{quadro}/status"


def topico_cmd(quadro: str, circuito: str) -> str:
    return f"ecowatt/{PREDIO}/{quadro}/{circuito}/cmd"


TOPICO_SIMULACAO = f"ecowatt/{PREDIO}/simulacao"
