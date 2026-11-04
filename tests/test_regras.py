"""Testes das regras de tratamento, sem banco.

    python -m unittest tests.test_regras -v
"""
import json
import unittest
from datetime import datetime, timedelta

from analise.transform import Rejeitado, avaliar_relogio, decodificar, detectar_lacuna, validar_metricas

NO = "!8fa724e8"
NO_INT = 0x8FA724E8
T0 = datetime(2026, 10, 23, 3, 0, 0)  # 00:00 de Brasília


def doc(i, recebido, pid, t=21, u=15, ts=None, **extra):
    pd = {"from": NO_INT, "sender": NO, "id": pid, "type": "text",
          "payload": {"t": t, "u": u}, "timestamp": ts if ts is not None else int((recebido - datetime(1970, 1, 1)).total_seconds())}
    pd.update(extra)
    return {"_id": f"{i:024x}", "payload_data": pd, "payload": json.dumps(pd), "received_at": recebido}


class TestRegras(unittest.TestCase):
    def test_origem_vem_de_from_e_nao_de_sender(self):
        d = doc(1, T0, 5)
        d["payload_data"]["sender"] = "!aaaaaaaa"  # gateway diferente
        p = decodificar(d)
        self.assertEqual(p.no, NO)
        self.assertEqual(p.gateway, "!aaaaaaaa")

    def test_rejeicoes_de_esquema(self):
        d = doc(1, T0, 5); d["payload_data"]["payload"] = {"text": ""}
        with self.assertRaises(Rejeitado) as e:
            decodificar(d)
        self.assertEqual(e.exception.motivo, "texto_vazio")
        d = doc(1, T0, 0)
        with self.assertRaises(Rejeitado) as e:
            decodificar(d)
        self.assertEqual(e.exception.motivo, "sem_id_pacote")
        d = doc(1, T0, 5, type="position")
        with self.assertRaises(Rejeitado) as e:
            decodificar(d)
        self.assertEqual(e.exception.motivo, "tipo_nao_modelado:position")

    def test_texto_com_json_dentro(self):
        d = doc(1, T0, 5)
        d["payload_data"]["payload"] = {"text": '{"t": 22, "u": 40}'}
        self.assertEqual(decodificar(d).metricas, {"temperatura_c": 22, "umidade_pct": 40})

    def test_telemetria_de_dispositivo(self):
        d = doc(1, T0, 5, type="telemetry")
        d["payload_data"]["payload"] = {"battery_level": 87, "voltage": 4.02, "channel_utilization": 3.1,
                                        "air_util_tx": 0.4, "uptime_seconds": 3600}
        p = decodificar(d)
        self.assertEqual(p.tipo, "dispositivo")
        self.assertEqual(validar_metricas(p.metricas)["tensao_v"], 4.02)

    def test_validacao(self):
        with self.assertRaises(Rejeitado):
            validar_metricas({"temperatura_c": 0, "umidade_pct": 0})
        with self.assertRaises(Rejeitado):
            validar_metricas({"temperatura_c": 85})
        with self.assertRaises(Rejeitado):
            validar_metricas({"temperatura_c": float("nan")})
        with self.assertRaises(Rejeitado):
            validar_metricas({"temperatura_c": "erro"})
        self.assertEqual(validar_metricas({"temperatura_c": "21.5"}), {"temperatura_c": 21.5})

    def test_relogio(self):
        self.assertEqual(avaliar_relogio(0, T0)[1], "ausente_ou_zero")
        self.assertEqual(avaliar_relogio(3600, T0)[1], "antes_de_2025")
        self.assertEqual(avaliar_relogio(1792683446 + 3 * 86400, datetime(2026, 10, 23, 10, 57))[1], "diverge_mais_de_24h")
        self.assertIsNone(avaliar_relogio(1792683446, datetime(2026, 10, 23, 10, 57))[1])

    def test_lacuna(self):
        self.assertIsNone(detectar_lacuna(T0, T0 + timedelta(minutes=10), 300, 2.5))
        lac = detectar_lacuna(T0, T0 + timedelta(minutes=60), 300, 2.5)
        self.assertEqual(lac.leituras_ausentes, 11)


if __name__ == "__main__":
    unittest.main()
