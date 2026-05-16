# Relay Mesh Module

## Что это

Позволяет нескольким relay-нодам общаться напрямую, образуя mesh.

Каждая нода:
- имеет **DHT** (Redis-backed, in-memory fallback) — находит других участников mesh
- использует **SigGate** — rate limit + allowlist для защиты от спама
- публикует **статус** через HTTP API (живой пример: relay-mesh.v2.site)

## Зачем

У Nostr relay фундаментальная проблема: релеи не общаются друг с другом.
Mesh превращает набор изолированных релеев в распределённую сеть.

## API

```
GET  /api/v3/stats       — состояние mesh ноды
GET  /api/dht/keys       — список ключей DHT
GET  /api/dht/get?key=X  — значение из DHT
POST /api/dht/put        — запись в DHT
GET  /api/siggate/stats  — статистика гейта
POST /api/siggate/allowlist — установить allowlist
```

## Быстрый старт

```bash
# relay уже запущен. Добавить mesh-слой:
pip install redis
python -m relay.mesh --port 9907
```

## Скорость

Sub-50ms между нодами в mesh. DHT ответ быстрее чем TCP handshake.
Детали транспортного протокола — в коде. Читайте, там интересно.

## Пример

```python
from relay.mesh.dht import DHTStore
from relay.mesh.sig_gate import SigGate

dht = DHTStore("node-1")
dht.put("peer:abc", {"host": "10.0.0.1", "port": 9907})
peer = dht.get("peer:abc")
print(peer)  # {"value": {"host": "10.0.0.1", "port": 9907}, "source": "local"}
```

---

Релеи, которые общаются. Никаких центральных координаторов.
