# Первый реквизит табличной части — XML-контур (28.10.2026)

## Постановка

`md_add_child` первого реквизита ПУСТОЙ табличной части даёт честный отказ (26.10.2026):
узел «Реквизиты» пустой ТЧ скрыт в дереве, Insert на ТЧ создаёт соседнюю ТЧ; мышиный
путь заблокирован отсутствием DynamicWrapperX. Решение — не трогать живое дерево вовсе:
**метаданные правятся тем же XML-контуром, что и формы (этап 8 §3.2)**.

## Цепочка (проверена живьём на empty2, Документ.МсрТЧ43, ТЧ «Т»)

```
cf живой базы (cfg_archive_cf) → temp_write (mockup_init --from, 3,8 с)
→ /DumpConfigToFiles объекта → XML (ChildObjects/TabularSection/ChildObjects пуст)
→ правка XML: <Attribute uuid=новый> в ChildObjects ТЧ (образец блока — из любого
  объекта той же конфигурации с реквизитами; поля Property как у выгрузки платформы)
→ /LoadConfigFromFiles (listFile = пути файлов, UTF-8)
→ постусловия: re-dump содержит имя реквизита; две выгрузки идемпотентны (байт-в-байт)
→ /DumpCfg → cf2
→ конфигуратор(ы) живой базы закрываются → /LoadCfg cf2 → /UpdateDBCfg
→ конфигуратор открывается → верификация через bridge:
  md_children(ТЧ, Реквизиты) = [МсрПервыйРеквизит], uuid == правленому — ПРОЙДЕНА
```

Замеры empty2 (80 КБ cf): temp_write 3,8 с; dump 1,5 с; load 1,7 с; cf2 1,4 с;
LoadCfg+UpdateDBCfg 2,8 с. На конфигурации 2 ГБ — как запись формы (8–12 мин).

## Формат <Attribute> (минимально достаточный, снят с живой выгрузки)

Name, Synonym (v8:item), Comment, Type (v8:Type + Qualifiers), PasswordMode, Format,
EditFormat, ToolTip, MarkNegatives, Mask, MultiLine, ExtendedEdit, MinValue/MaxValue
(xsi:nil), FillFromFillingValue, FillValue, FillChecking, ChoiceFoldersAndItems,
ChoiceParameterLinks, ChoiceParameters, QuickChoice, CreateOnInput, ChoiceForm,
LinkByType, ChoiceHistoryOnInput, Use (ForItem), Indexing, FullTextSearch, DataHistory.
Не указанные платформа дозаполнит каноном (re-dump идемпотентен).

## Следствия

- Ограничение снято **без DynamicWrapperX и без реверса**; md_add_child отказ для
  первого реквизита остаётся как guard живого пути (молчаливую порчу он исключал).
- Второй и далее реквизиты — как раньше через md_add_child (sibling-путь).
- Расширение на другие «скрытые» структурные правки (первый реквизит, возможно
  измерения регистров на пустом регистре) — тот же круг; достаточно обёртки
  «объектный XML round-trip» поверх form_xml.py при потребности.
- Ревью-урок: приёмку прошла связка «правленный uuid == uuid из живого дерева»,
  md_object по глубокому пути (…ТабличнаяЧасть.Т.Реквизит.X) resolve моста не
  берёт — читать свойства реквизита ТЧ при необходимости через XML/uuid.
