╔══════════════════════════════════════════════════════════════╗
║         SNIN RELAY — ПЛАН ВНЕДРЕНИЯ K7 (IPFS Pubsub)        ║
║         7 дней | ~380 строк кода | 4 новых модуля           ║
║              Версия: 1.0 | Дата: 2026-05-05                 ║
╚══════════════════════════════════════════════════════════════╝


1. СТРУКТУРА ВНЕДРЕНИЯ
───────────────────────

  Фаза 1 (Дни 1-3): Инфраструктура
    • Установка IPFS (kubo)
    • CID Index — поиск событий по CID
    • Nostr Marshal — конвертация event ↔ IPFS

  Фаза 2 (Дни 4-5): Ядро
    • IPFS Pubsub — publish + subscribe
    • SSE Handler — HTTP/SSE gateway

  Фаза 3 (Дни 6-7): Интеграция
    • Встраивание в relay_server_v2.py
    • Fallback: старый fanout если IPFS не отвечает
    • Тестирование + документация


2. ПОСТАТНЫЙ ПЛАН
──────────────────

┌─────────────────────────────────────────────────────────────┐
│ ДЕНЬ 1 — Установка IPFS (kubo)                             │
├─────────────────────────────────────────────────────────────┤

  Цель: IPFS-нода на порту 5001, автозапуск через start.sh

  Шаги:
  [ ] Скачать kubo v0.32.0
      curl -sL https://dist.ipfs.tech/kubo/v0.32.0/kubo_v0.32.0_linux-amd64.tar.gz | tar xz
      sudo cp kubo/ipfs /usr/local/bin/

  [ ] Инициализация
      ipfs init --profile server
      ipfs config Datastore.StorageMax "2GB"
      ipfs config Swarm.ConnMgr.HighWater 200
      ipfs config Swarm.ConnMgr.LowWater 50
      ipfs config --json Experimental.Pubsub true

  [ ] Первый запуск
      ipfs daemon &

  [ ] Проверка
      curl http://127.0.0.1:5001/api/v0/version
      # → {"Version":"0.32.0","Commit":"...","Repo":"...","System":"amd64"}

  [ ] Обновить start.sh
      # Добавить перед relay:
      nohup ipfs daemon > /tmp/ipfs.log 2>&1 &
      sleep 5  # ждём инициализацию DHT

  [ ] Ограничения
      ipfs config --json Swarm.DisableNatPortMap true
      # не нужно открывать порты — у нас нет публичного IP

  Ожидаемый результат: ✅ IPFS работает, API отвечает
                        ✅ автозапуск через start.sh
                        ✅ лог: /tmp/ipfs.log

  Время: ~30 минут

┌─────────────────────────────────────────────────────────────┐
│ ДЕНЬ 2 — CID Index                                         │
├─────────────────────────────────────────────────────────────┤

  Цель: SQLite-индекс event.id → IPFS CID

  Файл: cid_index.py

  Код:
    class CIDIndex:
        - __init__(db_path): открыть БД, создать таблицу
        - _init(): CREATE TABLE IF NOT EXISTS cid_index
        - add(event_id, cid, pubkey, kind, created_at)
        - get_by_event_id(event_id) -> str | None
        - get_by_pubkey(pubkey, limit=20) -> list[str]
        - get_by_kind(kind, limit=50) -> list[str]
        - get_stats() -> dict (total, by_kind, by_pubkey)

  Таблица:
    CREATE TABLE cid_index (
        event_id TEXT PRIMARY KEY,
        cid TEXT NOT NULL,
        pubkey TEXT,
        kind INTEGER,
        created_at INTEGER,
        stored_at INTEGER DEFAULT (strftime('%s','now'))
    );
    CREATE INDEX idx_cid_pubkey ON cid_index(pubkey);
    CREATE INDEX idx_cid_kind ON cid_index(kind);
    CREATE INDEX idx_cid_created ON cid_index(created_at);

  Тест:
    idx = CIDIndex("/home/agent/data/sites/relay/relay_v2.db")
    idx.add("abc123", "QmTest123", "pubkey", 1, 1746450000)
    assert idx.get_by_event_id("abc123") == "QmTest123"
    print("✅ CID Index работает")

  Интеграция с relay:
    # В main() после db = RelayDB(...)
    from cid_index import CIDIndex
    cid_index = CIDIndex(DB_PATH)
    app['cid_index'] = cid_index

  Ожидаемый результат: ✅ CID index — 3 метода поиска
                        ✅ Таблица cid_index в relay_v2.db
                        ✅ 0 дополнительной RAM (SQLite)

  Строк кода: ~120
  Время: ~45 минут

┌─────────────────────────────────────────────────────────────┐
│ ДЕНЬ 3 — Nostr Marshal  (ГОТОВО ✅ 2026-05-05 17:56 MSK)   │
├─────────────────────────────────────────────────────────────┤

  Цель: Конвертация Nostr event → IPFS объект (bytes) и обратно,
        с проверкой целостности (id = SHA256) и подписи (Schnorr)

  Файл: /home/agent/data/sites/relay/nostr_marshal.py (420 строк)

  API:

    # Сериализация
    serialize_event(event, canonical=False) → bytes
      - canonical=True → формат [0, pubkey, ts, kind, tags, content] (для id)
      - canonical=False → {"nostr": {...}, "meta": {...}} (полный для IPFS)

    # Десериализация (3 формата)
    deserialize_event(data) → event dict | None
      - Полный IPFS: {"nostr": {...}, "meta": {...}}
      - Плоский: {"id": "...", "pubkey": "...", ...}
      - Канонический: [0, pubkey, ts, kind, tags, content]

    # ID Verification
    compute_event_id(event) → str (SHA256 по NIP-01)
    verify_event_id(event) → bool

    # Sig Verification
    verify_schnorr(event) → bool (через Event.from_json + .verify())

    # Полный цикл
    verify_integrity(event) → dict
      {"valid": bool, "checks": {...}, "error": "..."}
    marshal_event(event) → (bytes, cid, integrity_dict)
    unmarshal_event(data, expected_id) → event | None

  Тесты: 16/16 PASSED
    - serialize/deserialize ✅
    - canonical format ✅
    - id verification + mismatch ✅
    - integrity pass + tampered ✅
    - marshal (bytes + integrity) ✅
    - unmarshal + bad id reject ✅
    - canonical deserialize ✅
    - empty + garbage data ✅
            event["created_at"],
            event["kind"],
            event["tags"],
            event["content"]
        ], separators=(',', ':'), ensure_ascii=False)
        computed = hashlib.sha256(serialized.encode()).hexdigest()
        return computed == event["id"]

  Тест:
    event = {"id":"abc","pubkey":"02...","created_at":1746450000,
             "kind":1,"tags":[],"content":"test","sig":"sig"}
    data = event_to_bytes(event)
    recovered = bytes_to_event(data)
    assert recovered["id"] == event["id"]
    print(f"✅ Marshal: {len(data)} bytes, CID suggested")

  Ожидаемый результат: ✅ event → bytes (обратимо)
                        ✅ verify_integrity проверяет хеш
                        ✅ Размер: ~500-2000 байт на событие

  Строк кода: ~50
  Время: ~20 минут

┌─────────────────────────────────────────────────────────────┐
│ ДЕНЬ 4 — IPFS Pubsub                                       │
├─────────────────────────────────────────────────────────────┤

  Цель: publish() + subscribe() для Nostr событий через IPFS

  Файл: ipfs_pubsub.py

  Код:
    class IPFSPubsub:
        def __init__(self, api_url="http://127.0.0.1:5001"):
            self.api = api_url
            self.topic = "snin-dao"
            self._published = 0
            self._subscribed = 0
            self._peers = 0

        async def publish_event(self, event: dict) -> str:
            """Публикует Nostr событие в IPFS + pubsub.
            Возвращает CID."""
            # 1. Конвертировать в bytes
            data = event_to_bytes(event)
            # 2. Добавить в IPFS
            cid = await self._ipfs_add(data)
            # 3. Опубликовать CID в topic
            await self._pubsub_pub(cid)
            self._published += 1
            return cid

        async def _ipfs_add(self, data: bytes) -> str:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"{self.api}/api/v0/add",
                    data=data
                )
                result = await resp.json()
                return result["Hash"]

        async def _pubsub_pub(self, cid: str):
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"{self.api}/api/v0/pubsub/pub",
                    params={"arg": self.topic},
                    data=cid.encode()
                )

        async def subscribe_loop(self, on_event_callback):
            """Подписка на topic. Вызывает callback для каждого CID."""
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api}/api/v0/pubsub/sub",
                    params={"arg": self.topic}
                ) as resp:
                    async for line in resp.content:
                        try:
                            msg = json.loads(line)
                            cid = msg.get("data", "")
                            if cid:
                                event = await self._fetch_event(session, cid)
                                if event:
                                    await on_event_callback(event)
                                self._subscribed += 1
                        except (json.JSONDecodeError, Exception):
                            continue

        async def _fetch_event(self, session, cid: str) -> dict | None:
            """Забрать данные из IPFS по CID, сконвертировать в event."""
            try:
                resp = await session.post(
                    f"{self.api}/api/v0/cat?arg={cid}"
                )
                data = await resp.read()
                return bytes_to_event(data)
            except Exception:
                return None

        def get_peers(self) -> int:
            """Количество пиров в swarm (обновляется раз в 60с)."""
            try:
                import requests
                resp = requests.post(f"{self.api}/api/v0/swarm/peers")
                return len(resp.json().get("Peers", []))
            except Exception:
                return 0

        def get_stats(self) -> dict:
            return {
                "published": self._published,
                "subscribed": self._subscribed,
                "peers": self.get_peers(),
                "topic": self.topic,
            }

  Тест:
    ipfs = IPFSPubsub()
    event = test_event()
    cid = await ipfs.publish_event(event)
    print(f"✅ Published: {cid}")
    # Ручная проверка:
    # curl http://127.0.0.1:5001/api/v0/pubsub/sub?arg=snin-dao
    # → должен получить CID

  Ожидаемый результат: ✅ publish: event → CID (50ms)
                        ✅ subscribe: CID → event (500ms)
                        ✅ peers: >0

  Строк кода: ~150
  Время: ~1.5 часа

┌─────────────────────────────────────────────────────────────┐
│ ДЕНЬ 5 — SSE Handler  (ГОТОВО ✅ 2026-05-05 18:06 MSK)     │
├─────────────────────────────────────────────────────────────┤

  Файл: /home/agent/data/sites/relay/sse_handler.py (360 строк)
  Тесты: 9/9 passed (build_sse_query, CORS, JSON validation)

  Протокол:
    POST /nostr  REQ → SSE stream (события + EOSE + keepalive)
    POST /nostr  EVENT → {"status":"ok","cid":"Qm..."}

  Реальный API (не план):

    # ── SSE Response ──
    async def sse_response(request) -> web.StreamResponse
      = StreamResponse(headers: SSE, CORS, X-Accel-Buffering: no)

    # ── REQ (подписка) ──
    handle_req(request, sub_id, filters, db, ipfs, cid_index):
      1. sse_response → keepalive task (:ping каждые 30 с)
      2. query_events_sse(db, filters) — SQLite по kind/authors/since/until/search
      3. Отправка событий + EOSE
      4. Ожидание (keepalive до разрыва)

    # ── EVENT (публикация) ──
    handle_event(request, event, db, ipfs, cid_index):
      1. verify_integrity(event) через nostr_marshal
      2. db.store_event_async(event)
      3. ipfs.publish_event(event) → CID
      4. cid_index.add(event_id, cid, pubkey, kind, ts)
      5. Если IPFS не сработал → fanout fallback

    # ── Nostr endpoint ──
    setup_sse_routes(app) — подключает POST /nostr
    handle_nostr — диспетчер REQ / EVENT с валидацией JSON

  Интеграция в relay:
    ✅ from sse_handler import setup_sse_routes
    ✅ setup_sse_routes(app) в main()
    ✅ POST /nostr активен

  Тест (curl):
    $ curl -X POST http://localhost:8198/nostr \\
      -H "Content-Type: application/json" \\
      -d '{"method":"REQ","params":["s1",{"kinds":[1],"limit":3}]}'
    → data: ["EVENT","s1",{...}]
      data: ["EVENT","s1",{...}]
      data: ["EVENT","s1",{...}]
      data: ["EOSE","s1"]

┌─────────────────────────────────────────────────────────────┐
│ ДЕНЬ 6 — Интеграция в relay (ГОТОВО ✅ 2026-05-05 19:06 MSK) │
├─────────────────────────────────────────────────────────────┤

  Цель: Встроить IPFS + SSE + verify_integrity в relay_server_v2.py

  Реальный код (не план):

  1. Импорты:
     from ipfs_pubsub import IPFSPubsub
     from cid_index import CIDIndex
     from nostr_marshal import verify_integrity
     from sse_handler import setup_sse_routes

  2. В main(): инициализация ipfs, cid_index, SSE routes, subscribe_loop

  3. verify_integrity() в _handle_event():
     Проверка NIP-01 id + Schnorr sig перед сохранением события
     → reject с "integrity: ..." если не прошло

  4. IPFS publish в should_fanout блоке:
     ipfs.publish_event(event) → cid_index.add(id, cid, pubkey, kind, ts)
     Если IPFS упал → fanout.enqueue(event) (fallback)

  5. SSE Broadcaster (live stream):
     on_ipfs_event → sse_broadcaster.broadcast(event)
     → все активные SSE подписчики получают новые события в реальном времени

  6. API endpoints:
     GET /api/sse → {"subscribers": N}
     GET /api/stats → включает {"sse_subscribers": N}
     POST /nostr → setup_sse_routes(app)

  Проверено:
    REQ: 2 события + EOSE ✅
    EVENT: reject invalid sig ✅
    SSE subscribers: 3 активных ✅

┌─────────────────────────────────────────────────────────────┐
│ ДЕНЬ 7 — Тестирование + Документация                        │
├─────────────────────────────────────────────────────────────┤

  Цель: Проверить работу K7 в production

  Тесты:

  [ ] 7.1. Unit-тесты
      python3 -c "
      from nostr_marshal import event_to_bytes, bytes_to_event, verify_integrity
      event = {'id':'test','pubkey':'02...','created_at':1746450000,
               'kind':1,'tags':[],'content':'hello','sig':'abc'}
      data = event_to_bytes(event)
      recovered = bytes_to_event(data)
      assert recovered['id'] == event['id']
      print('✅ marshal OK')
      "

  [ ] 7.2. IPFS publish
      curl -X POST http://127.0.0.1:8198/nostr \
        -H "Content-Type: application/json" \
        -d '{"method":"EVENT","params":[{"id":"test_event_1","pubkey":"02a36a56...","created_at":1746450000,"kind":1,"tags":[],"content":"K7 test from curl","sig":"abc"}]}'
      # → {"status":"ok","cid":"Qm..."}

  [ ] 7.3. CID index
      python3 -c "
      from cid_index import CIDIndex
      idx = CIDIndex('/home/agent/data/sites/relay/relay_v2.db')
      cid = idx.get_by_event_id('test_event_1')
      print(f'CID from index: {cid}')
      assert cid is not None
      print('✅ CID index работает')
      "

  [ ] 7.4. SSE stream
      curl -N -X POST http://127.0.0.1:8198/nostr \
        -H "Content-Type: application/json" \
        -d '{"method":"REQ","params":["test",{"limit":1}]}'
      # → data: ["EVENT","test",{...event...}]
      #   data: ["EOSE","test"]

  [ ] 7.5. IPFS peers
      curl http://127.0.0.1:5001/api/v0/swarm/peers
      # → {"Peers":[{"Addr":"...","Peer":"...","Direction":0,...}]}

  [ ] 7.6. WSS fallback (отключить IPFS)
      sudo systemctl stop ipfs  # или pkill ipfs
      curl -X POST http://127.0.0.1:8198/nostr \
        -H "Content-Type: application/json" \
        -d '{"method":"EVENT","params":[{"id":"test_fallback","pubkey":"02...","created_at":1746450000,"kind":1,"tags":[],"content":"fallback test","sig":"abc"}]}'
      # → {"status":"ok"} (через fanout)
      # → проверить лог: "falling back to fanout"
      ipfs daemon &

  [ ] 7.7. Нагрузочный тест
      python3 -c "
      import asyncio, aiohttp
      async def test():
          session = aiohttp.ClientSession()
          for i in range(100):
              resp = await session.post(
                  'http://127.0.0.1:8198/nostr',
                  json={'method':'REQ','params':[f's{i}',{'limit':1}]}
              )
          await session.close()
          print('✅ 100 SSE подключений')
      asyncio.run(test())
      "

  [ ] 7.8. Проверка /api/ipfs
      curl http://127.0.0.1:8198/api/ipfs
      # → {"published": N, "subscribed": N, "peers": N, "topic": "snin-dao"}

  [ ] 7.9. Проверка dashboard
      curl http://127.0.0.1:8198/ | grep "IPFS"
      # → <div class="value">N</div><div class="label">IPFS Peers</div>

  Ожидаемый результат: ✅ Все тесты пройдены
                        ✅ K7 в production
                        ✅ Fallback работает
                        ✅ Dashboard показывает IPFS метрики

  Время: ~2 часа


3. TOTAL
────────

  ┌─────────────────────┬────────────┐
  │ Метрика             │ Значение   │
  ├─────────────────────┼────────────┤
  │ Дней                │ 7          │
  │ Новых файлов        │ 4          │
  │ Строк кода          │ ~380       │
  │ Системных пакетов   │ 1 (kubo)   │
  │ RAM дополнительно   │ ~400 MB    │
  │ CPU дополнительно   │ ~5%        │
  │ Риск даунтайма      │ Низкий     │
  │                    │ (fanout     │
  │                    │  остаётся   │
  │                    │  как        │
  │                    │  fallback)  │
  └─────────────────────┴────────────┘

──────────────────────────────────────────────────────────────
  Конец плана внедрения
  SNIN Relay — IPFS Pubsub Edition
  2026-05-05
