"""Contadores dos quatro problemas do dado bruto. SOMENTE LEITURA.

    python -m analise.diagnostico

Lê a coleção inteira, aplica as regras de analise/transform.py (decodificar,
relógio, deduplicar por (origem, id), validar) e conta para onde foi cada
documento: aceito, duplicata ou quarentena (com o motivo).
"""
from collections import Counter
from datetime import timedelta

from pymongo import MongoClient

from . import config
from .transform import Rejeitado, avaliar_relogio, decodificar, validar_metricas


def main():
    cfg = config.carregar()
    col = MongoClient(cfg.mongo_uri, serverSelectionTimeoutMS=10_000)[cfg.mongo_db][cfg.mongo_colecao]
    total = col.count_documents({})
    print(f"MongoDB {cfg.mongo_db}.{cfg.mongo_colecao}: {total} documentos")

    campos = Counter()
    for d in col.find({}, {"payload_data": 1}).sort("_id", -1).limit(2000):
        pd = d.get("payload_data")
        if not isinstance(pd, dict):
            campos["(sem payload_data)"] += 1
            continue
        for k in ("id", "from", "sender", "type", "timestamp", "payload"):
            if k in pd:
                campos[k] += 1
        campos[f"type={pd.get('type')}"] += 1
    print("\ncampos nos 2000 documentos mais recentes:")
    for k, v in sorted(campos.items()):
        print(f"  {k:28s} {v}")

    print("\nexemplo de documento recente com leitura:")
    ex = col.find_one({"payload_data.payload.t": {"$exists": True}}, sort=[("_id", -1)])
    if ex:
        pd = ex["payload_data"]
        print("  ", {k: pd.get(k) for k in ("id", "from", "sender", "type", "timestamp", "payload")},
              "received_at:", ex.get("received_at"))

    cont, vistos, nos = Counter(), set(), Counter()
    por_dia, copias = Counter(), Counter()
    for doc in col.find({}).sort("_id", 1):
        cont["lidos"] += 1
        try:
            p = decodificar(doc)
        except Rejeitado as r:
            cont[f"quarentena.{r.motivo}"] += 1
            continue
        _, motivo = avaliar_relogio(p.ts_no_bruto, p.instante)
        copias[(p.no, p.packet_id)] += 1
        if (p.no, p.packet_id) in vistos:
            cont["duplicatas"] += 1
            continue
        vistos.add((p.no, p.packet_id))
        try:
            validar_metricas(p.metricas)
        except Rejeitado as r:
            cont[f"quarentena.{r.motivo}"] += 1
            continue
        if motivo:
            cont[f"relogio_invalido.{motivo}"] += 1
        cont["aceitos"] += 1
        nos[p.no] += 1
        por_dia[(p.no, str((p.instante - timedelta(hours=3)).date()))] += 1
    print("\npara onde foi cada documento (nada foi gravado):")
    for k, v in sorted(cont.items()):
        print(f"  {k:40s} {v}")
    print("  aceitos por nó:", dict(nos))
    print("\nexemplares por pacote (1 = chegou uma vez):", dict(sorted(Counter(copias.values()).items())))
    print("\npacotes aceitos por nó e dia (horário de Brasília):")
    for (no, dia), n in sorted(por_dia.items()):
        print(f"  {no}  {dia}  {n:4d}  {'#' * (n // 10)}")
    pri = col.find_one({}, sort=[("_id", 1)])
    ult = col.find_one({}, sort=[("_id", -1)])
    print("\nprimeiro documento:", pri.get("received_at"), "| último:", ult.get("received_at"),
          "| tipo do último:", (ult.get("payload_data") or {}).get("type"))


if __name__ == "__main__":
    main()
