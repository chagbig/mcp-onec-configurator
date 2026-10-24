# mcp-configurator: MCP-сервер живого конфигуратора 1С (Снегопат)

MCP-сервер, через который агент работает с **открытым конфигуратором 1С** человека:
читает дерево метаданных и модули, правит их, создаёт и удаляет объекты, сохраняет
конфигурацию с архивной точкой отката — без циклов «выгрузить XML → загрузить XML».

План и история — [docs/plan-mcp-snegopat.md](docs/plan-mcp-snegopat.md);
отчёт по сборке (этапы 2–6) — [docs/stage-02-06-mcp-server.md](docs/stage-02-06-mcp-server.md).

```
MCP-клиент (stdio, JSON-RPC)
   │
mcp-snegopat (Python, mcp>=2.3)          server/mcp_snegopat/
   │ JSON-файлы, по сессии конфигуратора
   │   Y:\Snegopat-mcp\mcp\sessions\<pid>\{req,resp,session.json}
   ▼
аддин mcp_bridge.js в 1cv8.exe (Снегопат) bridge/mcp_bridge.js
   │ тик таймера 150 мс, только при !busy и без модального окна
   ▼
конфигуратор: метаданные / модули / команды / дерево (этап 7)
```

## Состав

| Что | Где | Назначение |
|---|---|---|
| Мост (аддин JScript) | `bridge/mcp_bridge.js` → `Y:\Snegopat-mcp\x64\modules\snegopat\addins\mcp\` | реестр сессий, диспетчер операций, идемпотентность по id, статусы running, автоответ только знакомым диалогам |
| MCP-сервер (Python, stdio) | `server/` (пакет `mcp_snegopat`, `pip install -e server`) | 33 инструмента, конфиг разрешений, таймауты, журнал вызовов |
| Тесты | `server/tests/` | 27 юнит-тестов на подставном мосте, stdio-смоук, живой e2e-прогон |
| Документация | `docs/` | план, отчёты этапов |

## Установка

1. Снегопат: сборка в `Y:\Snegopat-mcp\x64` (это машина владельца; путь без кириллицы).
   `starter.exe` запущен; `std/main.js` грузит минимальный комплект + `addins\mcp\mcp_bridge.js`.
2. Файл включения `Y:\Snegopat-mcp\mcp\mcp_bridge.on` (без него мост не ставит таймер — инвариант 8).
3. Сервер: `pip install -e server` (Python ≥3.11, ставит `mcp`); команда `mcp-snegopat`.
4. Конфиг (необязателен, безопасные умолчания) — JSON-файл, путь в `MCP_SNEGOPAT_CONFIG`
   или `%LOCALAPPDATA%\mcp-snegopat\config.json` (пример — `server/mcp_snegopat.json.example`):

```json
{
  "root": "Y:\\Snegopat-mcp\\mcp",
  "own_bases": ["File=\"Y:\\Snegopat-mcp\\bases\\empty_mcp\";"],
  "allow_store": false,
  "allow_update_db": false
}
```

5. Подключение к агенту (например, ZCode/Claude): stdio-команда `mcp-snegopat`.

## Инструменты (кратко)

Чтение: `list_sessions`, `session_info`, `md_tree` (пагинация), `md_object` (свойства со
значениями, модули с хешами), `md_children`, `md_find`, `md_counts`, `md_walk`,
`module_get`, `type_lookup`.

Запись (постусловия в мосте, ожидаемое состояние обязательно): `module_set`,
`module_patch`, `md_set_property(_internal)`, `md_set_type(_internal)`, `md_add_child`,
`md_delete`.

Конфигурация: `config_open`, `cfg_archive_cf` (точка отката), `config_save` (без
архива снимает сам; отказ без него), `syntax_check`.

Хранилище и БД (по разрешениям в конфиге, **не проверены вживую**): `cfgstore_capture`,
`cfgstore_commit`, `update_db_cfg` (только `own_bases`).

Формы: `form_export` / `form_import` — внутренний текст 1С (не XML; перевод XML ↔
внутренний текст — этап 8 плана, не сделан). Служебные: `tree_activate`,
`tree_activate_group`, `dialog_probe`, `focus_probe`, `bridge_wait`, `bridge_stats`.

## Порядок работы агента

1. `list_sessions` → выбрать сессию (pid) и контейнер; при нескольких конфигураторах
   pid указывается в каждом вызове.
2. `config_open`, если основная конфигурация не открыта (после старта current — БД).
3. Чтение: `md_tree`/`md_find` → `md_object`/`module_get` (хеш нужен для записи).
4. Запись с ожидаемым состоянием (`expected_hash`/`expected`): расхождение = параллельная
   правка → отказ.
5. Сохранение: `config_save` (архив cf — точка отката — автоматически).
6. Проверка созданного обратным чтением.

## Ограничения и правила безопасности

* Только своя рабочая станция и свои базы; на CI-раннерах и общих базах не используется
  (`starter.exe` внедряется во все `1cv8.exe` пользователя).
* Все операции — в главном потоке конфигуратора из тика таймера; `busy` + модальное окно
  отсекают повторный вход. Ответ — только к моменту завершения: таймаут MCP не значит
  «не выполнено» (статус `running` в `list_sessions`, `bridge_wait(id)`).
* Запись без ожидаемого состояния отказывает; каждое изменение подтверждается обратным
  чтением (инварианты 3–4 плана).
* `config_save` — только после архива cf (последнее сохранённое состояние); обновление
  конфигурации БД — только для `own_bases`; помещение в хранилище — с комментарием, по
  одному объекту.
* Синонимы (локализуемые строки) не записываются (не решено в спайке); формы — во
  внутреннем формате; `setModuleText` платформы пуст — запись через редактор.
* Платформа 8.3.27.2074; `v8Version` виден в `list_sessions`. На 8.3.28 нужен пересъём
  слотов (`dump-tools`).

## Запуск проверок

```
python server/tests/test_all.py            # юнит-тесты (подставной мост)
python server/tests/smoke_stdio.py         # протокол MCP по stdio
python server/tests/live_e2e.py [pid]      # живой прогон (нужен открытый конфигуратор)
```
