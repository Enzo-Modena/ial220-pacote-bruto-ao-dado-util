"""Regras de tratamento do dado bruto: decodificar, validar, relógio e lacunas.

Nada aqui fala com banco de dados, por isso tudo é testável sem infraestrutura
(ver tests/test_regras.py).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# Tipos
# --------------------------------------------------------------------------- #


class Rejeitado(Exception):
    """Pacote que vai para a quarentena. `motivo` é curto e agrupável."""

    def __init__(self, motivo: str, detalhe: str = ""):
        super().__init__(motivo)
        self.motivo = motivo
        self.detalhe = detalhe[:255]


@dataclass
class Pacote:
    """Um pacote bruto já decodificado (antes de deduplicar e validar)."""

    mongo_id: str
    no: str  # nó de ORIGEM, formato !xxxxxxxx
    packet_id: int
    tipo: str  # tipo lógico: ambiente | dispositivo
    instante: datetime  # recebimento no servidor (UTC, naive) — a referência
    ts_no_bruto: object  # o que o nó disse que era a hora (só atributo)
    gateway: str | None
    metricas: dict[str, object] = field(default_factory=dict)
    instante_estimado: bool = False  # True se received_at faltou


# --------------------------------------------------------------------------- #
# Esquema: o que cada tipo de pacote pode trazer
# --------------------------------------------------------------------------- #

# Pacote "text" do Mega: {"t": 21, "u": 15}
MAPA_TEXTO = {"t": "temperatura_c", "u": "umidade_pct"}

# Pacote "telemetry" (MQTT JSON do Meshtastic). Os nomes mudam entre versões de
# firmware, então aceitamos as duas grafias conhecidas.
MAPA_TELEMETRIA = {
    "battery_level": "bateria_pct",
    "batteryLevel": "bateria_pct",
    "voltage": "tensao_v",
    "channel_utilization": "uso_canal_pct",
    "channelUtilization": "uso_canal_pct",
    "air_util_tx": "airtime_tx_pct",
    "airUtilTx": "airtime_tx_pct",
    "uptime_seconds": "uptime_s",
    "uptimeSeconds": "uptime_s",
    "temperature": "temperatura_c",
    "relative_humidity": "umidade_pct",
    "relativeHumidity": "umidade_pct",
    "barometric_pressure": "pressao_hpa",
    "barometricPressure": "pressao_hpa",
}

# Faixas físicas aceitáveis. Fora disso é lixo, não "um dia estranho".
FAIXAS = {
    "temperatura_c": (-20.0, 60.0),
    "umidade_pct": (0.0, 100.0),
    "bateria_pct": (0.0, 101.0),  # 101 = alimentado por USB no Meshtastic
    "tensao_v": (0.0, 6.0),
    "uso_canal_pct": (0.0, 100.0),
    "airtime_tx_pct": (0.0, 100.0),
    "uptime_s": (0.0, 10 * 365 * 86400.0),
    "pressao_hpa": (300.0, 1100.0),
}

TIPOS_SEM_METRICA = {"position", "nodeinfo", "neighborinfo", "traceroute", "routing", "mapreport"}

# --------------------------------------------------------------------------- #
# Decodificação
# --------------------------------------------------------------------------- #


def _como_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _payload_data(doc: dict) -> dict:
    """`payload_data` é o JSON do Meshtastic já convertido pelo serviço de coleta.
    Se faltar, tenta o texto original em `payload` antes de desistir."""
    pd = doc.get("payload_data")
    if isinstance(pd, dict):
        return pd
    bruto = doc.get("payload")
    if isinstance(bruto, (str, bytes)):
        try:
            obj = json.loads(bruto)
        except (ValueError, TypeError):
            raise Rejeitado("json_invalido", str(bruto)[:120])
        if isinstance(obj, dict):
            return obj
    raise Rejeitado("sem_payload_data")


def _no_origem(pd: dict) -> tuple[str, str | None]:
    """Origem = campo `from` (inteiro do nó que gerou o pacote).

    `sender` no JSON do Meshtastic é o GATEWAY que publicou no MQTT; só serve de
    origem quando `from` não veio (o caso do rádio que é sensor e gateway ao
    mesmo tempo).
    """
    gateway = pd.get("sender") if isinstance(pd.get("sender"), str) else None
    origem = pd.get("from")
    if isinstance(origem, int) and not isinstance(origem, bool) and origem > 0:
        return f"!{origem & 0xFFFFFFFF:08x}", gateway
    if gateway and gateway.startswith("!"):
        return gateway.lower(), gateway
    raise Rejeitado("sem_no_origem")


def _metricas_texto(payload) -> dict:
    if isinstance(payload, dict) and "text" in payload and not (set(payload) & set(MAPA_TEXTO)):
        texto = payload.get("text")
        if not isinstance(texto, str) or not texto.strip():
            raise Rejeitado("texto_vazio")
        try:
            payload = json.loads(texto)
        except ValueError:
            raise Rejeitado("texto_nao_e_leitura", texto[:120])
    if not isinstance(payload, dict):
        raise Rejeitado("payload_nao_objeto", repr(payload)[:120])
    m = {MAPA_TEXTO[k]: v for k, v in payload.items() if k in MAPA_TEXTO}
    if not m:
        raise Rejeitado("texto_sem_campos_t_u", json.dumps(payload)[:120])
    return m


def _metricas_telemetria(payload) -> tuple[str, dict]:
    if not isinstance(payload, dict):
        raise Rejeitado("payload_nao_objeto", repr(payload)[:120])
    # versões mais novas aninham: {"device_metrics": {...}} / {"environment_metrics": {...}}
    plano = {}
    for k, v in payload.items():
        if isinstance(v, dict):
            plano.update(v)
        else:
            plano[k] = v
    m = {MAPA_TELEMETRIA[k]: v for k, v in plano.items() if k in MAPA_TELEMETRIA}
    if not m:
        raise Rejeitado("telemetria_sem_campos_conhecidos", ",".join(sorted(plano))[:120])
    disp = {"bateria_pct", "tensao_v", "uso_canal_pct", "airtime_tx_pct", "uptime_s"}
    return ("dispositivo" if set(m) & disp else "ambiente"), m


def decodificar(doc: dict) -> Pacote:
    """Documento bruto do Mongo -> Pacote. Levanta Rejeitado se não der."""
    mongo_id = str(doc.get("_id"))
    pd = _payload_data(doc)
    no, gateway = _no_origem(pd)
    tipo_bruto = str(pd.get("type", "")).lower()

    pid = pd.get("id")
    if isinstance(pid, str) and pid.isdigit():
        pid = int(pid)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        # sem id não há como deduplicar sem olhar conteúdo — e conteúdo não é chave
        raise Rejeitado("sem_id_pacote")

    if tipo_bruto == "text":
        tipo, metricas = "ambiente", _metricas_texto(pd.get("payload"))
    elif tipo_bruto == "telemetry":
        tipo, metricas = _metricas_telemetria(pd.get("payload"))
    elif tipo_bruto in TIPOS_SEM_METRICA:
        raise Rejeitado(f"tipo_nao_modelado:{tipo_bruto}")
    else:
        raise Rejeitado("tipo_desconhecido", tipo_bruto[:60])

    instante, estimado = instante_recebimento(doc)
    return Pacote(
        mongo_id=mongo_id,
        no=no,
        packet_id=int(pid),
        tipo=tipo,
        instante=instante,
        ts_no_bruto=pd.get("timestamp"),
        gateway=gateway,
        metricas=metricas,
        instante_estimado=estimado,
    )


def instante_recebimento(doc: dict) -> tuple[datetime, bool]:
    """O relógio de referência é o do servidor, nunca o do nó."""
    for campo in ("received_at", "data"):
        v = doc.get(campo)
        if isinstance(v, datetime):
            return _como_utc_naive(v), False
    gt = getattr(doc.get("_id"), "generation_time", None)  # ObjectId embute o instante de inserção
    if isinstance(gt, datetime):
        return _como_utc_naive(gt), True
    raise Rejeitado("sem_instante_recebimento")


# --------------------------------------------------------------------------- #
# Validação
# --------------------------------------------------------------------------- #


def validar_metricas(metricas: dict) -> dict[str, float]:
    limpas = {}
    for nome, v in metricas.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            # aceita número vindo como texto ("21.5"), rejeita o resto
            try:
                v = float(str(v).replace(",", "."))
            except ValueError:
                raise Rejeitado("valor_nao_numerico", f"{nome}={v!r}"[:120])
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            raise Rejeitado("valor_nan", nome)
        lo, hi = FAIXAS[nome]
        if not lo <= v <= hi:
            raise Rejeitado(f"fora_da_faixa:{nome}", f"{v}")
        limpas[nome] = v
    # DHT11 que falhou na leitura costuma devolver 0/0 em vez de erro
    if limpas.get("temperatura_c") == 0.0 and limpas.get("umidade_pct") == 0.0:
        raise Rejeitado("falha_sensor_zerado")
    return limpas


# Abaixo disto o rádio está claramente sem hora (1970, ou o dia que ligou).
RELOGIO_MINIMO = datetime(2025, 1, 1)
TOLERANCIA_RELOGIO = timedelta(hours=24)


def avaliar_relogio(ts_no_bruto, instante: datetime) -> tuple[datetime | None, str | None]:
    """Devolve (ts_no como datetime ou None, motivo da invalidade ou None).

    O timestamp do nó NUNCA vira o instante da leitura; ele é guardado como
    atributo e só serve para estatística (e para detectar fora de ordem).
    """
    if ts_no_bruto in (None, "", 0):
        return None, "ausente_ou_zero"
    try:
        ts = datetime.fromtimestamp(float(ts_no_bruto), tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError, OSError):
        return None, "nao_numerico"
    if ts < RELOGIO_MINIMO:
        return ts, "antes_de_2025"
    if abs(ts - instante) > TOLERANCIA_RELOGIO:
        return ts, "diverge_mais_de_24h"
    return ts, None


# --------------------------------------------------------------------------- #
# Lacunas (a ausência é informação)
# --------------------------------------------------------------------------- #


@dataclass
class Lacuna:
    inicio: datetime  # última leitura antes do silêncio
    fim: datetime  # primeira leitura depois
    duracao_s: int
    leituras_ausentes: int


def detectar_lacuna(anterior: datetime | None, atual: datetime, intervalo_s: int, fator: float) -> Lacuna | None:
    if anterior is None or atual <= anterior:
        return None
    delta = (atual - anterior).total_seconds()
    if delta <= intervalo_s * fator:
        return None
    ausentes = max(int(round(delta / intervalo_s)) - 1, 1)
    return Lacuna(anterior, atual, int(delta), ausentes)


def hora_utc(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def dia_local(dt: datetime, fuso_horas: int):
    return (dt + timedelta(hours=fuso_horas)).date()
