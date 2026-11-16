# Этап 18. Журнал MetaDataEvent — «что правил человек» (04.11.2026)

## 1. Задача

Аудит изменений метаданных: кто (человек/агент), когда, какой объект, какая правка.
Основа — события метаданных Снегопата, которые мост уже использовал точечно
(onMdAdd/onDelMd для md_add_child/md_delete).

## 2. Разведка API (факты)

- **Событие:** `eventMetaData` {2282FCC8-BF58-47F6-A420-3FFEB8F193E5}
  (`v8api/metadata.v:560`), виды `EventMetaDataKind`: emdAddChild=0, emdPropChanged=1,
  emdDeleteChild=2, emdChanged=3, emdSave=4, emdClose=5, emdAfterSave=6.
  `MDEventInfo`: request, result, kind, object, parent, id, changedPropId.
- **Механика:** движок Снегопата подписывается сам (`com_metadata.as:24`,
  `getEventService().subscribe(eventMetaData, ...)`) и перебрасывает в аддины:
  `oneDesigner._events.fireEvent(..., "MetaDataEvent", ...)` (`com_metadata.as:1002`).
- **JScript-подписка обкатана:** `events.connect(metadata, "MetaDataEvent",
  SelfScript.self, "Handler")` — Autosave.js:119 и наш мост (md_add_child:1015,
  md_delete:1109). Обработчик получает `e` с `kind/request/obj.id/obj.name/
  obj.mdclass.name(1)/parentObj.id/obj.container`.

## 3. Реализация

- **Мост 1.1** (`bridge/mcp_bridge.js`, деплой в addins выполнен):
  постоянная подписка на `MetaDataEvent` при старте (рядом с `mcp_bridge.on`);
  каждая запись — JSONL-строка в `mcp/journal/<pid>.jsonl` (UTF-16 append через
  FSO): `ts, pid, src, kind, kindName, request, obj{id,name,cls}, parentId, container`.
  **src=agent**, пока `busy` (инвариант 2 — идёт обработка запроса моста), иначе
  `human` — корреляция «правка агента vs правка человека» без ведения списков
  операций. Ошибки записи глотаются (журнал диагностический).
  `session_info` теперь несёт `journal {file, count}`.
- **MCP-инструмент `md_journal`** (44-й): чтение журнала сессии (путь из
  session_info или явно), хвостом `limit`, фильтры `src` («human»/«agent») и
  `kind`; сводка по видам событий; оборванная хвостовая строка не роняет чтение.

## 4. Валидация

- Юнит: чтение UTF-16 JSONL, tail/limit, фильтры src/kind, отсутствующий файл,
  оборванный хвост. 79/79.
- Esprima-парсинг моста 1.1 — OK (Node на машине нет; cscript кириллицу не парсит).
- **Живой смоук — при следующем старте конфигуратора** (деплой сделан, живой
  сессии в момент разработки не было): сделать любую правку в дереве →
  `md_journal` должен показать human-записи; правка через `module_set` → agent.

## 5. Ограничения v1

- Журнал не ротируется (один файл на pid) — объём под наблюдением.
- Двойные записи: события приходят парами request/fact — обе логируются
  (фильтр по `request` на стороне читателя).
- Изменения, минующие событийный слой (внешние пакетные операции), не видны —
  журнал живёт только внутри живого конфигуратора.
