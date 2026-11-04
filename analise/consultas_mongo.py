"""As três consultas do artigo, como agregações no MongoDB. SOMENTE LEITURA.

    python -m analise.consultas_mongo

Toda consulta deduplica por (nó de origem, id do pacote) antes de agregar e usa
received_at (hora do servidor) como instante, nunca o timestamp do rádio.
"""
from datetime import timedelta

from pymongo import MongoClient

from . import config

TZ = "America/Sao_Paulo"

# Etapas comuns: só pacotes com origem e id; um exemplar por (from, id).
DEDUP = [
    {"$match": {"payload_data.from": {"$exists": True}, "payload_data.id": {"$exists": True}}},
    {"$sort": {"received_at": 1}},
    {"$group": {"_id": {"no": "$payload_data.from", "id": "$payload_data.id"},
                "instante": {"$first": "$received_at"},
                "tipo": {"$first": "$payload_data.type"},
                "sender": {"$first": "$payload_data.sender"},
                "p": {"$first": "$payload_data.payload"},
                "copias": {"$sum": 1}}},
]

Q1_DIARIA = DEDUP + [
    {"$match": {"tipo": "text", "p.t": {"$type": "number"}, "p.u": {"$type": "number"},
                "$nor": [{"p.t": 0, "p.u": 0}],          # DHT11 em falha devolve 0/0
                "p.t": {"$gte": -20, "$lte": 60}, "p.u": {"$gte": 0, "$lte": 100}}},
    {"$group": {"_id": {"$dateToString": {"date": "$instante", "format": "%Y-%m-%d", "timezone": TZ}},
                "leituras": {"$sum": 1},
                "temp_media": {"$avg": "$p.t"}, "temp_min": {"$min": "$p.t"}, "temp_max": {"$max": "$p.t"},
                "umid_media": {"$avg": "$p.u"}, "umid_min": {"$min": "$p.u"}, "umid_max": {"$max": "$p.u"}}},
    {"$sort": {"_id": 1}},
]

Q2_HORA = DEDUP + [
    {"$match": {"tipo": "text", "p.t": {"$type": "number"}, "$nor": [{"p.t": 0, "p.u": 0}]}},
    {"$group": {"_id": {"$hour": {"date": "$instante", "timezone": TZ}},
                "temp_media": {"$avg": "$p.t"}, "umid_media": {"$avg": "$p.u"}, "leituras": {"$sum": 1}}},
    {"$sort": {"_id": 1}},
]

Q3_SILENCIO = DEDUP + [
    {"$setWindowFields": {"partitionBy": "$_id.no", "sortBy": {"instante": 1},
                          "output": {"anterior": {"$shift": {"output": "$instante", "by": -1}}}}},
    {"$set": {"intervalo_min": {"$dateDiff": {"startDate": "$anterior", "endDate": "$instante", "unit": "minute"}}}},
    {"$group": {"_id": "$_id.no",
                "primeiro": {"$min": "$instante"}, "ultimo": {"$max": "$instante"}, "pacotes": {"$sum": 1},
                "silencios_acima_15min": {"$sum": {"$cond": [{"$gt": ["$intervalo_min", 15]}, 1, 0]}},
                "minutos_em_silencio": {"$sum": {"$cond": [{"$gt": ["$intervalo_min", 15]}, "$intervalo_min", 0]}},
                "maior_silencio_min": {"$max": "$intervalo_min"}}},
    {"$sort": {"pacotes": -1}},
]

Q_TELEMETRIA = DEDUP + [
    {"$match": {"tipo": "telemetry"}},
    {"$group": {"_id": {"$dateToString": {"date": "$instante", "format": "%d/%m %H:00", "timezone": TZ}},
                "pacotes": {"$sum": 1},
                "bateria": {"$avg": {"$ifNull": ["$p.battery_level", "$p.device_metrics.battery_level"]}},
                "tensao": {"$avg": {"$ifNull": ["$p.voltage", "$p.device_metrics.voltage"]}},
                "uso_canal": {"$avg": {"$ifNull": ["$p.channel_utilization", "$p.device_metrics.channel_utilization"]}},
                "airtime_tx": {"$avg": {"$ifNull": ["$p.air_util_tx", "$p.device_metrics.air_util_tx"]}}}},
    {"$sort": {"_id": 1}},
]


def _f(v):
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return "—" if v is None else str(v)


def tabela(titulo, linhas, colunas):
    print(f"\n### {titulo}\n")
    print("| " + " | ".join(colunas) + " |")
    print("|" + "---|" * len(colunas))
    for l in linhas:
        print("| " + " | ".join(_f(l.get(c)) for c in colunas) + " |")


def main():
    cfg = config.carregar()
    col = MongoClient(cfg.mongo_uri, serverSelectionTimeoutMS=10_000)[cfg.mongo_db][cfg.mongo_colecao]
    ag = lambda p: list(col.aggregate(p, allowDiskUse=True))

    ex = col.find_one({"payload_data.type": "telemetry"}, sort=[("_id", -1)])
    if ex:
        print("exemplo de telemetria:", ex["payload_data"].get("payload"))

    tabela("1. Temperatura e umidade por dia (Maker, !8fa724e8)", ag(Q1_DIARIA),
           ["_id", "leituras", "temp_media", "temp_min", "temp_max", "umid_media", "umid_min", "umid_max"])
    tabela("2. Perfil por hora do dia", ag(Q2_HORA), ["_id", "leituras", "temp_media", "umid_media"])
    q3 = ag(Q3_SILENCIO)
    for l in q3:  # nó em hexadecimal (!8fa724e8) e datas no horário de Brasília
        l["no"] = f"!{int(l['_id']) & 0xFFFFFFFF:08x}"
        for k in ("primeiro", "ultimo"):
            l[k] = (l[k] - timedelta(hours=3)).strftime("%d/%m %H:%M")
    tabela("3. Silêncio por nó (intervalo > 15 min entre pacotes)", q3,
           ["no", "pacotes", "primeiro", "ultimo", "silencios_acima_15min", "minutos_em_silencio", "maior_silencio_min"])
    tabela("Extra. Telemetria do dispositivo por hora", ag(Q_TELEMETRIA),
           ["_id", "pacotes", "bateria", "tensao", "uso_canal", "airtime_tx"])


if __name__ == "__main__":
    main()
