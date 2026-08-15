# Qdrant Migration Status — PotsdamBot (agent/anonymous-forum-posts)

Last updated: 2026-07-19 ~22:40 (cron run)

## Что сделано этой сессией
- Диагностирован сбой теневого индексатора `potsdambot-qdrant-build` (ExitCode=1):
  `Qdrant embedding subprocess failed: timed out after 240 seconds` на окне из
  3607 чанков — единичный таймаут эмбеддинга откатывал весь пакет из 12000
  сообщений, из-за чего билд застревал в цикле повторного падения.
- Исправление через TDD (RED→GREEN, коммит `ae33fe0`, запушен в artemtotal):
  - `checkpoint_last_id()` — безопасный чекпоинт last_id по завершённым чанкам;
  - `run_once()` — сохраняет чекпоинт перед повторным raise (resumable);
  - `_batch_embed()` — рекурсивно делит пакет пополам при таймауте, чтобы один
    медленный ONNX-срез не ронял весь прогон.
  - Тесты: QdrantUpdaterCheckpointTests, QdrantBatchEmbedRetryTests.
- Пересобран образ `telegram-search-bot-tgbot`, индексатор перезапущен с
  `QDRANT_EMBED_BATCH_SIZE=64` + `PYTHONUNBUFFERED=1`, продолжает с сохранённого
  состояния (`after_id=91128`). Больше НЕ падает.

## Текущее состояние (на момент записи)
- embed_state.json: `last_id=91144, history_mode=building` (ещё не full).
- Qdrant `chunks_minilm_v1`: status=green, points=19049, max indexed msg_id=103820.
- Макс. подходящий ID в SQLite: 162325; всего подходящих: 138301; осталось
  проиндексировать ~54K сообщений (после 91128).
- tgbot (продакшн): running, OOM=false, VECTOR_BACKEND=shadow (SQLite) — не тронут.
- qdrant: healthy. Индексатор: running, Restart=0, OOM=false.
- Тесты Qdrant/embed/search в образе: 38 passed. (Пред-существующий провал
  `test_anonymous_posts.test_deleted_submission_still_has_cooldown` — не связан,
  локализационная рассинхронизация RU/UK, вне области миграции.)

## Осталось (следующий запуск)
1. Дождаться `history_mode=full`, last_id≈162325 (фоновый монитор уведомит).
2. Реальные векторные запросы: «муж на час», «ремонт бытовой техники»,
   «сантехник» + мастера @Dmytriii/@SolnceVitalii/@Артём Бодян.
3. Перевозчики по всей истории: «перевізник до України», «перевозчик»,
   «виїхати з Німеччини до України» — контрольные контакты (Dima Hryhorovych,
   Ivan, Андрій, Павло+Тетяна).
4. Только после успешной проверки: docker-compose.yml → VECTOR_BACKEND=qdrant,
   QDRANT_UPDATER_ENABLED=1, коммит+пуш, пересоздать tgbot, сохранить оба тома.
5. Проверить restart=0/OOM=false/green/_vector_search/no glibc-fatal.

НЕ удалять/менять старый Chroma-том (tgbot_chroma_data) — оставлен для отката.
