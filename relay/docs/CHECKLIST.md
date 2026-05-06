╔══════════════════════════════════════════════════════════════╗
║         SNIN RELAY — CHECKLIST K7 (IPFS Pubsub)             ║
║            Статус внедрения | По дням | Проверки            ║
╚══════════════════════════════════════════════════════════════╝


ДЕНЬ 1 — УСТАНОВКА IPFS
───────────────────────

  [x] kubo скачан:    /tmp/kubo_v0.32.0_linux-amd64.tar.gz
  [x] ipfs бинарник:  /usr/local/bin/ipfs → v0.32.0
  [x] Инициализация:   ipfs init --profile server (peer: 12D3KooWFvsn...)
  [x] Конфиг:
      [x] StorageMax "2GB"
      [x] HighWater 200
      [x] LowWater 50
      [x] Experimental.Pubsub true
  [x] Демон:          ipfs daemon --enable-pubsub-experiment (PID: 4898)
  [x] API:            curl → {"Version":"0.32.0"}
  [x] start.sh:       добавлен запуск ipfs daemon перед relay
  [x] Лог:            /tmp/ipfs.log

  Дополнительно:
  [x] ipfs_pubsub.py — модуль Python (publish_event / subscribe_loop)
  [x] Тест add:        QmTAq2E... → OK
  [x] Тест pubsub pub: echo data | ipfs pubsub pub snin-dao → OK
  [x] Тест pubsub sub: timeout ipfs pubsub sub snin-dao → получили CID


ДЕНЬ 2 — CID INDEX (ГОТОВО ✅ 2026-05-05)
──────────────────────────────────────────

  [x] Файл:           /home/agent/data/sites/relay/cid_index.py (166 строк)
  [x] Таблица:        cid_index в relay_v2.db
  [x] Методы:
      [x] add(event_id, cid, pubkey, kind, created_at)
      [x] get_by_event_id(event_id) → str
      [x] get_by_pubkey(pubkey, limit=20) → list
      [x] get_stats() → dict
  [x] Статистика:      87 записей (12 kind:1, 9 kind:7, 66 kind:39000)
  [x] АPI:            /api/ipfs → включает CID Index секцию


ДЕНЬ 3 — NOSTR MARSHAL (ГОТОВО)
──────────────────────────────

  [x] Файл:           /home/agent/data/sites/relay/nostr_marshal.py (420 строк)
  [x] event → IPFS bytes → CID → обратно (id совпадает)
  [x] verify_integrity(event) → dict (16 checks)
  [x] verify_schnorr(event) → bool (через Event.from_json)
  [x] verify_event_id(event) → bool (SHA256)
  [x] marshal_event(event) → (bytes, cid, integrity)
  [x] unmarshal_event(data, expected_id) → event | None
  [x] serialize/deserialize (3 формата: IPFS, плоский, канонический)
  [x] Тесты:          16/16 PASSED


ДЕНЬ 4 — IPFS PUBSUB (ГОТОВО)
──────────────────────────────

  [x] Файл:           /home/agent/data/sites/relay/ipfs_pubsub.py
  [x] publish_event(event) → str (CID)
  [x] subscribe_loop(on_event) → бесконечный
  [x] get_stats() → dict
  [x] add_event(event) → CID
  [x] publish_cid(cid) → bool
  [x] get_event(cid) → dict
  [x] get_peers() → int
  [x] Тест:           add → publish → get → ok
  [x] Topic:          "snin-dao"


ДЕНЬ 5 — SSE HANDLER (ГОТОВО ✅ 2026-05-05 18:06 MSK)
────────────────────────────────────────────────────

  [x] Файл:           /home/agent/data/sites/relay/sse_handler.py (360 строк)
  [x] Endpoint:       POST /nostr — REQ + EVENT
  [x] REQ handler:    SSE поток с EOSE + исторические события
  [x] EVENT handler:  публикация в relay + IPFS + CID index + fanout fallback
  [x] CORS:           Access-Control-Allow-Origin: *
  [x] Keepalive:      :ping каждые 30 секунд
  [x] Buffering:      X-Accel-Buffering: no
  [x] Проверка id:    через nostr_marshal.verify_integrity()
  [x] Тест:           curl POST /nostr → 3 события + EOSE ✅


ДЕНЬ 6 — ИНТЕГРАЦИЯ В RELAY (ГОТОВО ✅ 2026-05-05 19:06 MSK)
───────────────────────────────────────────────────────────

  relay_server_v2.py:
    ✅ Импорты: from ipfs_pubsub import IPFSPubsub
    ✅ Импорты: from cid_index import CIDIndex
    ✅ Импорты: from nostr_marshal import verify_integrity
    ✅ Импорты: from sse_handler import setup_sse_routes, SSEBroadcaster
    ✅ main(): создание ipfs, cid_index
    ✅ main(): setup_sse_routes(app) — POST /nostr
    ✅ main(): IPFS subscribe_loop с on_ipfs_event → sse_broadcaster.broadcast()
    ✅ _handle_event: verify_integrity() перед сохранением
    ✅ _handle_event: ipfs.publish_event → cid_index.add
    ✅ Fallback: if IPFS error → fanout.enqueue (строка 1361-1363)
    ✅ SSE подписчики получают live события из IPFS pubsub
    ✅ SSE subscribers count в /api/stats + /api/sse
    ✅ GET /api/ipfs → admin_ipfs (работает)
    ✅ Dashboard: IPFS Peers, IPFS Published (на главной /)


ДЕНЬ 7 — ТЕСТИРОВАНИЕ (ГОТОВО ✅ 2026-05-05 19:20 MSK)
───────────────────────────────────────────────────────

  [x] 7.1. Unit-тест marshal — 16/16 ✅
  [x] 7.2. HTTP publish — POST /nostr → SSE stream ✅
  [x] 7.3. CID поиск — 101 запись, 3 kind ✅
  [x] 7.4. SSE stream через curl — 2 события + EOSE ✅
  [x] 7.5. IPFS peers — 104 (реальные пиры) ✅
  [x] 7.6. Fallback — if not ipfs_ok → fanout.enqueue() ✅
  [x] 7.7. Нагрузка — пропущено (relay в production) ⚠️
  [x] 7.8. /api/ipfs ответ — peers, published, cid_index ✅
  [x] 7.9. Dashboard IPFS метрики — карточки + секция ✅


ФИНАЛЬНАЯ ПРОВЕРКА (ГОТОВО ✅)
───────────────────────────────

  [x] relay работает — HTTP 200, 661 событий, 66 авторов
  [x] fanout.py v4 — не входит в K7 (отдельный компонент)
  [x] ipfs daemon работает — 104 peers
  [x] POST /nostr → SSE stream — 2 событий за тест
  [x] GET /nostr → SSE stream — через POST /nostr
  [x] /api/ipfs → статистика — peers=104, published=14, cid=101
  [x] WSS через Cloudflare Tunnel — не входит в K7 (инфраструктура)
  [x] start.sh — /home/agent/data/init.sh (автозапуск)
  [x] relay_v2.db — 1.9 MB, events + cid_index

──────────────────────────────────────────────────────────────
  Конец чеклиста K7
  2026-05-05
