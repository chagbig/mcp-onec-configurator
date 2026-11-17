# Контрактная карта: mcp-configurator ↔ EDT-MCP (№26)

Соответствие инструментов mcp-configurator (живой конфигуратор через Снегопат)
контрактам EDT-MCP (Eclipse-стек) и их общего эталона (Go-сервер, фасад `onec_*`).
Назначение: паритет при будущем фасаде v2 (алиасы по колонке EDT-MCP) и
ориентир для агентов, привыкших к EDT-терминам.

## Метаданные

| EDT-MCP | mcp-configurator | Заметки |
|---|---|---|
| list_projects | list_sessions | «проект» EDT = сеанс конфигуратора (база) |
| get_project | session_info | |
| get_metadata_tree | md_tree (пагинация по видам) | |
| get_metadata_object | md_object | |
| list_children | md_children | |
| search_metadata | md_find + md_counts/md_walk | |
| create_metadata_object | md_add_child | ОбщиеФормы/Макеты — конструкторы |
| rename_metadata_object | md_rename (cascade — №17) | у EDT-MCP без переписывания ссылок |
| delete_metadata_object | md_delete (confirm-гейт №19) | |
| set_property / синонимы | md_set_property / md_set_synonym (№25 — ru+en) | |
| clone_metadata_object | md_clone (№18; без форм/макетов) | |

## Модули и код

| EDT-MCP | mcp-configurator | Заметки |
|---|---|---|
| get_module_text | module_get (with_text) | |
| set_module_text | module_set / module_patch (hash-страж) | |
| search_in_code | md_search_code (№15; по cf, без платформы) | |
| get_module_structure / call_hierarchy | module_structure (№16; MVP-иерархия в пределах модуля) | |
| validate_query | query_validate (№21; через COM) | |

## Конфигурация/база

| EDT-MCP | mcp-configurator | Заметки |
|---|---|---|
| apply_development | config_save + update_db_cfg (confirm №19) | |
| compare_configurations | cf_diff (--hybrid/--full-canon; кэш v1.3) | |
| create/delete_infobase | infobase (№23; двухфазное удаление) | |
| run_yaxunit_tests | yaxunit_run (№22) | EDT-MCP иного тест-раннера |

## Формы

| EDT-MCP | mcp-configurator | Заметки |
|---|---|---|
| get_form / set_form | form_get_xml / form_set_xml (cf-контур, 8–12 мин на 2 ГБ) | EDT-формы — другой формат (eForm), контент непереносим |

## Диагностика/безопасность (уникальные — в EDT-MCP нет)

doctor (самодиагностика цепочки), eventlog (журнал ИБ через COM, №2),
journal_configure/md_journal (кто что правил), ref_check/md_check_refs,
ext_build_from_cf (конвейер «вендор+доработки → расширение для линии»),
cfgstore_capture/commit (хранилище), ext_list/dump_cfe/save/create.
Read-only пресет — №20 (`read_only: true` в конфиге).

## Принципиально непереносимое (Eclipse-стек)

apply_quick_fix, content assist, go_to_definition (фичи редактора EDT),
отладка BSL (Eclipse Debug), git-workspace EDT, Linux/macOS/headless CI
(конфигуратор Windows-only), теги/группы метаданных (нет в платформе).
