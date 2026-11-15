"""Persistência em SQLite.

No documento, a telemetria vai para o InfluxDB e o cadastro/alertas para o PostgreSQL.
No protótipo, as duas partes ficam em um único arquivo SQLite para não exigir instalação.
"""
import sqlite3
import threading
import time

ESQUEMA = """
CREATE TABLE IF NOT EXISTS leituras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL, circuito TEXT NOT NULL, quadro TEXT NOT NULL,
    seq INTEGER NOT NULL, ts INTEGER NOT NULL, recebido_em INTEGER NOT NULL,
    tensao REAL, corrente REAL, potencia REAL, energia REAL, fp REAL, frequencia REAL,
    temperatura REAL, umidade REAL, presenca INTEGER, rele INTEGER, rssi INTEGER,
    buffered INTEGER NOT NULL DEFAULT 0, ts_corrigido INTEGER NOT NULL DEFAULT 0,
    UNIQUE(device_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_leituras_circ_ts ON leituras(circuito, ts);
CREATE TABLE IF NOT EXISTS mensagens_invalidas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recebido_em INTEGER NOT NULL, topico TEXT, payload TEXT, motivo TEXT
);
CREATE TABLE IF NOT EXISTS anomalias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recebido_em INTEGER NOT NULL, device_id TEXT, seq INTEGER, descricao TEXT
);
CREATE TABLE IF NOT EXISTS alertas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    regra TEXT NOT NULL, severidade TEXT NOT NULL, alvo TEXT NOT NULL,
    titulo TEXT NOT NULL, detalhe TEXT, acao TEXT,
    status TEXT NOT NULL DEFAULT 'aberto',
    criado_em INTEGER NOT NULL, atualizado_em INTEGER NOT NULL,
    acao_pendente TEXT, acao_executa_em INTEGER
);
CREATE TABLE IF NOT EXISTS comandos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    circuito TEXT NOT NULL, acao TEXT NOT NULL, origem TEXT NOT NULL,
    alerta_id INTEGER, enviado_em INTEGER NOT NULL, tentativas INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'aguardando', confirmado_em INTEGER
);
CREATE TABLE IF NOT EXISTS notificacoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alerta_id INTEGER, canal TEXT, destino TEXT, mensagem TEXT, enviado_em INTEGER NOT NULL
);
"""

COLUNAS_LEITURA = ("device_id", "circuito", "quadro", "seq", "ts", "recebido_em", "tensao", "corrente",
                   "potencia", "energia", "fp", "frequencia", "temperatura", "umidade", "presenca",
                   "rele", "rssi", "buffered", "ts_corrigido")


def agora_ms() -> int:
    return int(time.time() * 1000)


class Banco:
    def __init__(self, caminho: str = "ecowatt.db"):
        self.con = sqlite3.connect(caminho, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.con.executescript(ESQUEMA)
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.commit()

    # ---------- utilitário ----------
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            cur = self.con.execute(sql, args)
            self.con.commit()
            return cur

    def limpar(self):
        with self.lock:
            for t in ("leituras", "mensagens_invalidas", "anomalias", "alertas", "comandos", "notificacoes"):
                self.con.execute(f"DELETE FROM {t}")
            self.con.commit()

    # ---------- leituras ----------
    def inserir_leitura(self, l: dict) -> bool:
        """Escrita idempotente: devolve False se (device_id, seq) já existe (duplicata)."""
        vals = [l.get(c) for c in COLUNAS_LEITURA]
        sql = f"INSERT OR IGNORE INTO leituras ({','.join(COLUNAS_LEITURA)}) VALUES ({','.join('?' * len(vals))})"
        return self.x(sql, vals).rowcount == 1

    def inserir_invalida(self, topico, payload, motivo):
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        self.x("INSERT INTO mensagens_invalidas (recebido_em, topico, payload, motivo) VALUES (?,?,?,?)",
               (agora_ms(), topico, payload[:500], motivo))

    def inserir_anomalia(self, device_id, seq, descricao):
        self.x("INSERT INTO anomalias (recebido_em, device_id, seq, descricao) VALUES (?,?,?,?)",
               (agora_ms(), device_id, seq, descricao))

    # ---------- alertas ----------
    def alerta_aberto(self, regra, alvo):
        r = self.q("SELECT * FROM alertas WHERE regra=? AND alvo=? AND status!='resolvido' ORDER BY id DESC LIMIT 1",
                   (regra, alvo))
        return r[0] if r else None

    def criar_alerta(self, regra, severidade, alvo, titulo, detalhe, acao="", acao_pendente=None, executa_em=None):
        t = agora_ms()
        cur = self.x("INSERT INTO alertas (regra, severidade, alvo, titulo, detalhe, acao, criado_em, atualizado_em,"
                     " acao_pendente, acao_executa_em) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (regra, severidade, alvo, titulo, detalhe, acao, t, t, acao_pendente, executa_em))
        return cur.lastrowid

    def atualizar_alerta(self, alerta_id, **campos):
        campos["atualizado_em"] = agora_ms()
        sets = ",".join(f"{k}=?" for k in campos)
        self.x(f"UPDATE alertas SET {sets} WHERE id=?", (*campos.values(), alerta_id))

    def alerta(self, alerta_id):
        r = self.q("SELECT * FROM alertas WHERE id=?", (alerta_id,))
        return r[0] if r else None

    # ---------- comandos e notificações ----------
    def criar_comando(self, circuito, acao, origem, alerta_id=None):
        return self.x("INSERT INTO comandos (circuito, acao, origem, alerta_id, enviado_em) VALUES (?,?,?,?,?)",
                      (circuito, acao, origem, alerta_id, agora_ms())).lastrowid

    def notificar(self, alerta_id, canal, destino, mensagem):
        self.x("INSERT INTO notificacoes (alerta_id, canal, destino, mensagem, enviado_em) VALUES (?,?,?,?,?)",
               (alerta_id, canal, destino, mensagem, agora_ms()))
