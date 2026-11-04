# Do pacote bruto ao dado útil

**IAL220 · Projeto Integrador de Big Data · Atividade 10** — Fatec Shunji Nishimura, Pompeia
Aluno: Enzo Modena · Professor: [Allan Siriani](https://github.com/siriani)

Análise **somente leitura** dos pacotes que a malha LoRa/Meshtastic da turma grava no MongoDB (`iot.sensores`). O código trata os quatro problemas do dado bruto, **conta** cada um e responde três perguntas com agregações no próprio MongoDB. Nenhuma escrita em banco.

```
DHT11 → Arduino Mega → Heltec !8fa724e8 ┐
                    Heltec !8fa7bcf4 ───┴→ MQTT → MongoDB iot.sensores ──(leitura)──→ analise/
```

## Como rodar

```bash
cp .env.example .env              # preencha MONGO_URI
docker compose run --rm analise                                  # as consultas
docker compose run --rm analise python -m analise.diagnostico    # os contadores
```

Testes das regras, sem banco: `python -m unittest tests.test_regras -v`

| Arquivo | O que faz |
|---|---|
| `analise/transform.py` | regras de tratamento: decodificar por tipo, origem, relógio, faixas, lacunas |
| `analise/diagnostico.py` | aplica as regras na coleção inteira e conta para onde foi cada documento |
| `analise/consultas_mongo.py` | as três consultas, como pipelines de agregação |
| `RESULTADOS.md` | saída real das consultas |

## Os quatro problemas, medidos na coleção real

Execução de 04/11/2026 sobre 3.580 documentos (19/10 a 04/11):

| Destino | Documentos |
|---|---|
| **aceitos** | **1.074** (893 do sensor do Maker, 181 de telemetria do `!8fa7bcf4`) |
| **duplicatas** | **1.766** |
| **quarentena** | **740** |
| total | 3.580 = 1.074 + 1.766 + 740 |

| Problema | Regra | O que apareceu |
|---|---|---|
| **Duplicatas** | Chave (nó de origem, id do pacote), nunca o conteúdo. A origem é `from`; `sender` é quem publicou no MQTT. | 878 pacotes chegaram 3 vezes, 10 chegaram 2 vezes, 186 uma vez só |
| **Relógio errado** | O instante é `received_at` (servidor). O `timestamp` do rádio só é guardado como atributo; inválido se for 0, anterior a 2025 ou a mais de 24 h do recebimento. | 5 pacotes com horário zerado |
| **Buracos** | Intervalo maior que 15 min entre pacotes do mesmo nó conta como silêncio. Nada é interpolado. | ver consulta 3 |
| **Esquema que muda** | Separação por tipo (`text` com `{t, u}`, `telemetry`), validação de número e faixa física, DHT11 0/0 é falha. O resto vai para a quarentena com motivo. | 598 sem `payload_data`, 98 textos vazios, 24 `nodeinfo`, 14 textos que não são leitura, 5 sem origem, 1 JSON inválido |

## As três consultas

Todas começam pela mesma etapa, que deduplica e fixa o relógio:

```js
{ $match: { "payload_data.from": { $exists: true }, "payload_data.id": { $exists: true } } },
{ $sort:  { received_at: 1 } },
{ $group: { _id: { no: "$payload_data.from", id: "$payload_data.id" },
            instante: { $first: "$received_at" },
            tipo:     { $first: "$payload_data.type" },
            p:        { $first: "$payload_data.payload" } } }
```

**1. Como variou cada dia no Maker?**: agrupa por dia (horário de Brasília) e calcula média, mínimo e máximo de temperatura e umidade, descartando as leituras 0/0.

| dia | leituras | temp. média | mín–máx | umid. média |
|---|---|---|---|---|
| 22/10 | 430 | 22,3 °C | 22–27 °C | 15,0 % |
| 23/10 | 291 | 20,5 °C | 19–23 °C | 15,6 % |
| 24/10 | 172 | 20,5 °C | 19–24 °C | 15,6 % |

**2. Qual a hora mais quente?**: agrupa por hora do dia. A mínima fica em torno de 19,2 °C às 4h–5h e a máxima em torno de 22,2 °C às 11h–12h. Em 22/10, dia de testes, as leituras foram mais frequentes, e esse dia pesa mais nas horas 7, 11 e 12.

**3. Quem ficou calado?**: usa `$setWindowFields` com `$shift` para comparar cada pacote com o anterior do mesmo nó.

| nó | pacotes | de | até | silêncios > 15 min | maior silêncio |
|---|---|---|---|---|---|
| `!8fa724e8` (sensor) | 956 | 22/10 10:22 | 04/11 14:01 | 12 | 7.285 min (~5 dias) |
| `!8fa7bcf4` (telemetria) | 200 | 03/11 09:19 | 04/11 14:30 | 2 | 1.530 min (~25 h) |

Extra, telemetria por hora: em 03/11 a bateria caiu de 63 % para 43 % (3,83 V → 3,66 V) em três horas. O uso do canal ficou entre 2 % e 3 %, ou seja, a malha está longe de saturar.

Saída completa em [`RESULTADOS.md`](RESULTADOS.md).

## Observação sobre a umidade

O DHT11 mede umidade a partir de 20 %. Os valores registrados (14 % a 17 %) estão abaixo dessa faixa e não devem ser lidos como a umidade real do laboratório. A regra de faixa física usa 0–100 %, então eles passam. Este é um caso em que conhecer o sensor importa mais que a regra genérica.

---

Credenciais só no `.env` (fora do git); o formato está em `.env.example`.
