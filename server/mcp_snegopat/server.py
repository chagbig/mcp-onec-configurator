"""MCP-сервер (stdio) живого конфигуратора 1С через Снегопат.

Инструменты читают и правят конфигурацию в ОТКРЫТОМ конфигураторе человека:
дерево метаданных, свойства, модули, создание/удаление объектов (этап 7),
сохранение с архивной точкой отката, синтакс-проверка, хранилище и обновление
БД — с разрешениями из конфига сервера (инвариант 5 плана).

Запуск: mcp-snegopat (после pip install) или python -m mcp_snegopat.
"""
from __future__ import annotations

import json
import sys
import os
import threading
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import __version__
from .bridge import Bridge, BridgeError
from .config import Config, load_config

# ---------- навигация дерева для создания подчинённых (этап 7, снято с экрана 17.10.2026) ----------

DOWN, ADD, SUB = 0x28, 0x6B, 0x6D

GROUP_NAV: dict[str, dict[str, int | list]] = {
    "Конфигурация": {
        "Константы": 2, "Справочники": 3, "Документы": 4, "ЖурналыДокументов": 5,
        "Перечисления": 6, "Отчеты": 7, "Обработки": 8, "ПланыВидовХарактеристик": 9,
        "ПланыСчетов": 10, "ПланыВидовРасчета": 11, "РегистрыСведений": 12,
        "РегистрыНакопления": 13, "РегистрыБухгалтерии": 14, "РегистрыРасчета": 15,
        "БизнесПроцессы": 16, "Задачи": 17,
        # узел «Общие» = 1; дети сняты автосъёмом 24.10.2026 (Insert по k-му ребёнку,
        # класс из mdeAdd; 17=ОбщиеФормы и 18=ОбщиеМакеты открывают конструкторы —
        # требуют wizard, координаты не внесены)
        "Подсистемы": [1, 1], "ОбщиеМодули": [1, 2], "ПараметрыСеанса": [1, 3],
        "Роли": [1, 4], "ОбщиеРеквизиты": [1, 5], "ПланыОбмена": [1, 6],
        "КритерииОтбора": [1, 7], "ПодпискиНаСобытия": [1, 8],
        "РегламентныеЗадания": [1, 9], "Боты": [1, 10],
        "ФункциональныеОпции": [1, 11], "ПараметрыФункциональныхОпций": [1, 12],
        "ОпределяемыеТипы": [1, 13], "ХранилищаНастроек": [1, 14],
        "ОбщиеКоманды": [1, 15], "ГруппыКоманд": [1, 16],
        # 17-18: Insert открывает конструктор — закрывает мастер-раннер (флаг-файл)
        "ОбщиеФормы": [1, 17], "ОбщиеМакеты": [1, 18],
    },
    "Справочник": {"Реквизиты": 1, "ТабличныеЧасти": 2, "Формы": 3, "Команды": 4, "Макеты": 5},
    "Документ": {"Реквизиты": 1, "ТабличныеЧасти": 2, "Формы": 3, "Команды": 4, "Макеты": 5},
    "РегистрСведений": {"Измерения": 1, "Ресурсы": 2, "Реквизиты": 3},
    "Перечисление": {"ЗначенияПеречисления": 1},
}

SINGULAR: dict[str, str] = {
    "Справочники": "Справочник", "Документы": "Документ", "Перечисления": "Перечисление",
    "РегистрыСведений": "РегистрСведений", "Реквизиты": "Реквизит",
    "ТабличныеЧасти": "ТабличнаяЧасть", "Измерения": "Измерение", "Ресурсы": "Ресурс",
    "ЗначенияПеречисления": "ЗначениеПеречисления", "Константы": "Константа",
    "Подсистемы": "Подсистема", "Формы": "Форма", "Команды": "Команда", "Макеты": "Макет",
    "Отчеты": "Отчет", "Обработки": "Обработка", "РегистрыНакопления": "РегистрНакопления",
    "ОбщиеМодули": "ОбщийМодуль", "ПланыВидовХарактеристик": "ПланВидовХарактеристик",
    "ПланыСчетов": "ПланСчетов", "ПланыВидовРасчета": "ПланВидовРасчета",
    "РегистрыБухгалтерии": "РегистрБухгалтерии", "РегистрыРасчета": "РегистрРасчета",
    "ЖурналыДокументов": "ЖурналДокументов", "БизнесПроцессы": "БизнесПроцесс",
    "Задачи": "Задача", "Роли": "Роль", "ОбщиеФормы": "ОбщаяФорма",
    "ОбщиеМакеты": "ОбщийМакет",
    "ПараметрыСеанса": "ПараметрСеанса", "ОбщиеРеквизиты": "ОбщийРеквизит",
    "КритерииОтбора": "КритерийОтбора", "ПодпискиНаСобытия": "ПодпискаНаСобытие",
    "РегламентныеЗадания": "РегламентноеЗадание", "Боты": "Бот",
    "ФункциональныеОпции": "ФункциональнаяОпция",
    "ПараметрыФункциональныхОпций": "ПараметрФункциональныхОпций",
    "ОпределяемыеТипы": "ОпределяемыйТип", "ХранилищаНастроек": "ХранилищеНастроек",
    "ОбщиеКоманды": "ОбщаяКоманда", "ГруппыКоманд": "ГруппаКоманд",
    "ПланыОбмена": "ПланОбмена", "Последовательности": "Последовательность",
}

# Виды, у которых Insert открывает конструктор: имя задаётся в конструкторе,
# который закрывает мастер-раннер моста (флаг-файл mcp_stg_wizard.json в root).
# Только ОбщиеФормы: у макетного конструктора «ОК» открывает редактор макета, и
# клик не возвращается — поток повисал (24.10.2026); макеты не автоматизированы.
CONSTRUCTOR_KINDS: dict[str, str] = {
    "ОбщиеФормы": "Конструктор общих форм",
}
CONSTRUCTOR_BLOCKED: dict[str, str] = {
    "ОбщиеМакеты": ("конструктор макета не автоматизирован: его «ОК» открывает редактор "
                    "макета и вешает автоматизацию (24.10.2026); создайте макет в "
                    "конфигураторе вручную или в XML-контуре"),
}

def _session_base_path(bridge, session: int):
    """Строка соединения сессии → путь базы (для пакетных вызовов)."""
    for s in bridge.sessions():
        if s.pid == session:
            conn = str(s.data.get("conn") or "")
            if "=" in conn:
                value = conn.split("=", 1)[1].rstrip(';" ').strip('"')
                return Path(value)
    raise BridgeError("bad_args", f"сессия {session} не найдена")


def _ext_exists_packed(base, name: str) -> bool:
    """Есть ли расширение с именем name в базе base — пакетно, по КОПИИ базы
    (пакетный вызов на живой базе падает «1Cv8.cdn»; 28.10.2026)."""
    import subprocess as _sp
    import os as _os
    import shutil as _sh
    base = str(base)
    work = "Y:/Snegopat-mcp/temp/extcheck-base"
    _sh.rmtree(work, ignore_errors=True)
    _os.makedirs(work, exist_ok=True)
    for _f in _os.listdir(base):
        if _f in ("1Cv8.cgr", "1Cv8Log") or _f.endswith(".cfl"):
            continue
        _s = _os.path.join(base, _f)
        if _os.path.isfile(_s):
            _sh.copy2(_s, _os.path.join(work, _f))
    probe = work + "\\extcheck-out.cfe"
    r = _sp.run([r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe", "DESIGNER",
                 "/DisableStartupDialogs", "/F", work,
                 "/DumpCfg", probe, "-Extension", name,
                 "/Out", work + "\\extcheck.log", "/DumpResult", work + "\\extcheck.res"],
                capture_output=True, timeout=300)
    return _os.path.isfile(probe) and _os.path.getsize(probe) > 0


INSTRUCTIONS = """\
Работа с ОТКРЫТЫМ конфигуратором 1С через мост Снегопата. Порядок:
1) list_sessions — найти сессию (pid) и контейнер (индекс + identifier);
2) config_open, если основная конфигурация ещё не открыта;
3) чтение: md_tree / md_object / md_find / module_get (текст берите с with_text=true);
4) запись — только с ожидаемым состоянием (expected_hash у модулей, expected у свойств):
   сначала прочитайте текущее, затем правьте; расхождение = кто-то правил параллельно;
5) сохранение: config_save (сам снимает архив cf — точку отката, и отказывает без него);
6) созданные объекты проверяйте обратным чтением (md_object / module_get).
Пути объектов: "Справочник.Имя.Реквизит.Х" (без имени корня), поиск — md_find.
Внимание: несколько открытых конфигураторов — указывайте session (pid) в каждом вызове.
"""


class Toolkit:
    """Инструменты MCP; каждый метод регистрируется как инструмент 1:1."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bridge = Bridge(cfg)

    # ---------- сессии ----------

    def list_sessions(self) -> dict:
        """Открытые конфигураторы с мостом: pid, платформа, строка соединения, контейнеры
        (identifier, isModified, root), running-операция, возраст heartbeat.
        Мёртвые pid показываются с alive=false. Пусто — мост/конфигуратор не запущен."""
        return {"sessions": [s.summary(self.cfg.heartbeat_stale_s) for s in self.bridge.sessions()]}

    def session_info(self, session: int | None = None) -> dict:
        """Полное состояние моста сессии: контейнеры, статистика тиков, modalMode, running."""
        return self._call(session, "session_info", {})

    # ---------- чтение ----------

    def md_tree(self, session: int | None = None, container: int | None = None,
                path: str | None = None, kinds: list[str] | None = None,
                offset: int = 0, limit: int = 100) -> dict:
        """Дети узла дерева метаданных по видам, с пагинацией (ERP — десятки тысяч).
        path — "Справочник.Имя" или пусто (корень); kinds — фильтр видов ("Справочники", ...);
        limit=0 — только счётчики. Ответ: kinds (вид+счёт), items (kind, name, id, cls), hasMore."""
        return self._call(session, "md_tree", self._a(container, path=path, kinds=kinds,
                                                      offset=offset, limit=limit))

    def md_object(self, session: int | None = None, container: int | None = None,
                  path: str | None = None, uuid: str | None = None) -> dict:
        """Объект метаданных: класс, uuid, имя, синоним, комментарий, свойства со значениями,
        модули с хешами (хеш нужен для module_set/module_patch). uuid — сверка адресата."""
        return self._call(session, "md_object", self._a(container, path=path, uuid=uuid))

    def md_children(self, session: int | None = None, container: int | None = None,
                    path: str | None = None, kind: str = "") -> dict:
        """Все подчинённые объекта одного вида (имена и uuid) — сверка до/после правок.
        kind — рус. множественное: "Реквизиты", "ТабличныеЧасти", "Справочники"...
        """
        if not kind:
            raise BridgeError("bad_args", "kind обязателен (например «Реквизиты»)")
        return self._call(session, "md_children", self._a(container, path=path, kind=kind))

    def md_find(self, session: int | None = None, container: int | None = None,
                mask: str = "", kind: str | None = None, limit: int = 50) -> dict:
        """Поиск объектов по подстроке имени (без учёта регистра) по всему дереву.
        Ответ: path (готов для других инструментов), name, id, cls; truncated=true — сузьте."""
        if not mask:
            raise BridgeError("bad_args", "mask обязателен")
        return self._call(session, "md_find", self._a(container, mask=mask, kind=kind, limit=limit))

    def module_get(self, session: int | None = None, container: int | None = None,
                   path: str = "", prop: str = "Модуль", with_text: bool = False,
                   open_editor: bool = False) -> dict:
        """Модуль объекта: хеш (нужен для записи), строки, readOnly.
        prop — имя свойства-модуля из md_object.modules ("Модуль", "МодульМенеджера", ...).
        with_text=true — сам текст; open_editor=true — сверка с буфером редактора."""
        return self._call(session, "module_get",
                          self._a(container, path=path, prop=prop,
                                  withText=with_text, openEditor=open_editor))

    def type_lookup(self, session: int | None = None, container: int | None = None,
                    name: str | None = None, limit: int = 20) -> dict:
        """GUID прикладного типа для md_set_type_internal (например "СправочникСсылка.Номенклатура").
        Без name — первые limit типов конфигурации."""
        return self._call(session, "type_lookup", self._a(container, name=name, limit=limit))

    def md_counts(self, session: int | None = None, container: int | None = None) -> dict:
        """Счёт объектов верхнего уровня по всем видам корня (диагностика)."""
        return self._call(session, "md_counts", self._a(container))

    def md_walk(self, session: int | None = None, container: int | None = None) -> dict:
        """Полный обход дерева: всего объектов, глубина, битые, время (диагностика)."""
        return self._call(session, "md_walk", self._a(container))

    # ---------- запись: модули и свойства ----------

    def module_set(self, session: int | None = None, container: int | None = None,
                   path: str = "", prop: str = "Модуль", text: str = "",
                   expected_hash: str = "") -> dict:
        """Заменить весь текст модуля. expected_hash — обязательный хеш из module_get:
        защищает от несохранённой правки человека и параллельных правок. Постусловие
        (сверка буфера и свойства с переданным) — в мосте; расхождение — отказ."""
        if not expected_hash:
            raise BridgeError("bad_args", "expected_hash обязателен (снимите module_get)")
        return self._call(session, "module_set",
                          self._a(container, path=path, prop=prop, text=text,
                                  expected_hash=expected_hash))

    def module_patch(self, session: int | None = None, container: int | None = None,
                     path: str = "", prop: str = "Модуль", edits: list[dict] | None = None,
                     expected_hash: str = "") -> dict:
        """Точечные правки модуля по номерам строк (меньше трафика и риск затереть):
        edits=[{line: номер с 1, from: точная текущая строка, to: новая}].
        expected_hash — из module_get. Несовпадение строки или хеша — отказ без изменений."""
        if not expected_hash:
            raise BridgeError("bad_args", "expected_hash обязателен (снимите module_get)")
        if not edits:
            raise BridgeError("bad_args", "edits обязателен")
        return self._call(session, "module_patch",
                          self._a(container, path=path, prop=prop, edits=edits,
                                  expected_hash=expected_hash))

    # guid типа «ЛокализуемаяСтрока» во внутреннем представлении 1С (снято с живого
    # объекта 24.10.2026; строкой setProperty синоним не пишется — спайк этапа 1)
    SYN_TYPE_GUID = "87024738-fc2a-4436-ada1-df79d395c424"

    def _synonym_internal(self, synonym: str) -> str:
        # кавычка/обратный слэш/перевод строки ломают внутреннее представление
        # (ревью 25.10.2026): записать их через этот путь нельзя — отказываем явно
        bad = [c for c in synonym if c in '"\\\n\r\t']
        if bad:
            raise BridgeError("bad_args",
                              f"синоним содержит недопустимые символы {bad!r} "
                              "(кавычка, обратный слэш, переводы строк)")
        return '{"#",%s,\n{1,"ru","%s"}\n}' % (self.SYN_TYPE_GUID, synonym)

    def md_set_synonym(self, session: int | None = None, container: int | None = None,
                       path: str = "", synonym: str = "",
                       expect_id: str | None = None) -> dict:
        """Записать СИНОНИМ объекта (русский). Строкой setProperty не пишется (спайк
        этапа 1) — пишется внутренним представлением локализуемой строки; постусловие
        сверяет внутреннее значение. Проверено живьём 24.10.2026."""
        if not synonym:
            raise BridgeError("bad_args", "synonym обязателен")
        return self._call(session, "prop_set_internal",
                          self._a(container, path=path, prop="Синоним",
                                  internal=self._synonym_internal(synonym),
                                  expectId=expect_id))

    def md_set_property(self, session: int | None = None, container: int | None = None,
                        path: str = "", prop: str = "", value: str = "",
                        expected: str | None = None, uuid: str | None = None) -> dict:
        """Записать простое свойство (строка/число/булево текстом): "Имя", "Комментарий"...
        expected — текущее значение из md_object/prop_get (защита от параллельных правок);
        для "Имя" обязательна uuid-сверка. Синоним — отдельный инструмент md_set_synonym."""
        if not prop:
            raise BridgeError("bad_args", "prop обязателен")
        if prop == "Имя" and not uuid:
            raise BridgeError("bad_args", "для переименования обязателен uuid (сверка адресата)")
        args = self._a(container, path=path, prop=prop, value=value, uuid=uuid)
        if expected is not None:
            args["expected"] = expected
        return self._call(session, "prop_set", args)

    def md_set_property_internal(self, session: int | None = None, container: int | None = None,
                                 path: str = "", prop: str = "", internal: str = "",
                                 expect_before: str | None = None,
                                 expect_id: str | None = None) -> dict:
        """Записать свойство-объект (Состав подсистемы, Владельцы, ...) внутренней строкой
        1С (ЗначениеВСтрокуВнутр; снимите образец md_object/prop_get_internal с базы-образца)."""
        return self._call(session, "prop_set_internal",
                          self._a(container, path=path, prop=prop, internal=internal,
                                  expectBefore=expect_before, expectId=expect_id))

    def md_set_type(self, session: int | None = None, container: int | None = None,
                    path: str = "", types: list[str] | None = None,
                    digits: int | None = None, length: int | None = None,
                    fraction: int | None = None, expect_id: str | None = None) -> dict:
        """Тип реквизита: примитивные типы, например types=["Строка"], length=50 или
        types=["Число"], digits=15, fraction=2. Ссылочные типы — md_set_type_internal
        (v8New("ОписаниеТипов") с прикладным типом роняет конфигуратор — этап 7)."""
        if not types:
            raise BridgeError("bad_args", "types обязателен")
        # отсутствующие квалификаторы НЕ передаём (null в v8New("КвалификаторыЧисла")
        # вешал движок — проверка этапа 2-4, 24.10.2026)
        t: dict = {"types": types}
        if digits is not None:
            t["digits"] = digits
            if fraction is not None:
                t["fraction"] = fraction
        if length is not None:
            t["length"] = length
        return self._call(session, "prop_set_type",
                          self._a(container, path=path, expectId=expect_id, type=t))

    def md_set_type_internal(self, session: int | None = None, container: int | None = None,
                             path: str = "", internal: str = "",
                             expect_id: str | None = None) -> dict:
        """Ссылочный/составной тип реквизита внутренней строкой. Шаблон для одного
        ссылочного: {"#",f5c65050-3bbb-11d5-b988-0050bae0a95d,{"Pattern",{"#",<guid>},{"S",20,1}}},
        где guid — из type_lookup (например СправочникСсылка.Х)."""
        return self._call(session, "prop_set_type_internal",
                          self._a(container, path=path, internal=internal, expectId=expect_id))

    # ---------- этап 7: создание и удаление ----------

    def md_add_child(self, session: int | None = None, container: int | None = None,
                     owner_path: str | None = None, kind: str = "", name: str = "",
                     comment: str | None = None, synonym: str | None = None,
                     sibling: str | None = None,
                     owner_class: str | None = None, nav_keys: list[int] | None = None,
                     wizard: dict | None = None) -> dict:
        """Создать объект метаданных в живом конфигураторе (этап 7: клавиатура в дереве).
        owner_path="" + kind="Справочники" — справочник верхнего уровня;
        owner_path="Справочник.Х", kind="Реквизиты" — реквизит (в т.ч. первый);
        kind — рус. МНОЖЕСТВЕННОЕ (Реквизиты, ТабличныеЧасти, Справочники, Подсистемы...).
        synonym — русский синоним (иначе объект останется без синонима); comment — комментарий.
        По умолчанию навигация к узлу группы по классу владельца (снимок этапа 7);
        sibling="ИмяСоседа" — вставка рядом с существующим; nav_keys — вручную (VK_коды).
        Для форм wizard задает шаги конструктора: {caption, steps:[{click/set}], click:"ОК"}.
        ОбщиеФормы/ОбщиеМакеты создаются через конструктор автоматически (мастер-раннер
        моста закрывает его по флаг-файлу; имя и комментарий задаются в конструкторе).
        Ответ: created {id, name, cls}, renamed, stray (чужие объекты — удалить вручную).
        ОГРАНИЧЕНИЕ (24.10.2026): на конфигурации, привязанной к хранилищу, клавиатурные
        Insert/Delete в дереве не срабатывают (проверено: sibling, навигация, после
        захвата) — создание/удаление объектов делайте на непривязанной базе; правки
        свойств и модулей под хранилищем работают (после захвата)."""
        if not kind or not name:
            raise BridgeError("bad_args", "kind и name обязательны")
        if kind in CONSTRUCTOR_BLOCKED:
            raise BridgeError("bad_args", CONSTRUCTOR_BLOCKED[kind])
        expect = SINGULAR.get(kind)
        if not expect:
            raise BridgeError("bad_args",
                              f"неизвестный вид {kind}; известны: {', '.join(sorted(SINGULAR))}")
        pid = self._pid(session)
        first, nav = False, None
        expand_keys: list[int] | None = None
        if nav_keys:
            first, target = True, owner_path or ""
            nav = nav_keys
        elif sibling:
            target = (owner_path + "." if owner_path else "") + expect + "." + sibling
        else:
            oc = owner_class
            if not oc:
                oc = self._call(session, "md_object", self._a(container, path=owner_path),
                                pid=pid).get("cls", "")
            # ПЕРВЫЙ реквизит ТЧ невозможен (26.10.2026): узел «Реквизиты» пустой ТЧ
            # в дереве не отображается, Insert на ТЧ создаёт соседнюю ТЧ; мышиный путь
            # требует DynamicWrapperX. Отказ до дерева — без побочных действий.
            if oc == "ТабличнаяЧасть" and kind == "Реквизиты" and not sibling:
                ch = self._call(session, "md_children",
                                self._a(container, path=owner_path, kind="Реквизиты"), pid=pid)
                if not ch.get("count"):
                    raise BridgeError(
                        "bad_args",
                        "первый реквизит табличной части не автоматизирован: узел «Реквизиты» "
                        "пустой ТЧ в дереве скрыт, Insert создаёт соседнюю ТЧ (26.10.2026); "
                        "добавьте реквизит в конфигураторе вручную, затем создавайте остальные "
                        "через sibling")
            table = GROUP_NAV.get(oc)
            if table is None:
                raise BridgeError("bad_args",
                                  f"неизвестный класс владельца «{oc}»; передайте nav_keys или sibling")
            k = table.get(kind)
            if k is None:
                raise BridgeError(
                    "bad_args",
                    f"для владельца «{oc}» вид {kind} не снят; доступно: "
                    + ", ".join(sorted(table)) + "; либо передайте sibling/nav_keys")
            first, target = True, owner_path or ""
            steps = k if isinstance(k, list) else [k]
            if len(steps) == 1:
                nav = [ADD] + [DOWN, SUB] * steps[0]
            else:
                # многоуровневый путь (например Общие -> Подсистемы): раскрытие
                # отдельным тиком (раскрытие узла асинхронно), затем ход + Insert
                expand_keys = [ADD] + [DOWN, SUB] * steps[0] + [ADD]
                nav = []
                for s in steps[1:]:
                    nav += [DOWN, SUB] * s
        self._focus_tree(pid, container, target)
        if expand_keys:
            self.bridge.call(pid, "tree_nav", {"keys": expand_keys})
            time.sleep(0.35)  # раскрытие узла завершается между тиками
        args = self._a(container, ownerPath=owner_path or "", kind=kind,
                       noActivate=True, requireTree=True, expectClass=expect, newName=name)
        if first:
            args["firstChild"] = True
            if nav:
                args["navKeys"] = nav
        else:
            args["sibling"] = sibling
        if comment and kind not in CONSTRUCTOR_KINDS:
            args["comment"] = comment
        if wizard:
            args["wizard"] = wizard
            args["modalPolicy"] = "wizard"
        if kind in CONSTRUCTOR_KINDS:
            # Insert открывает конструктор; его закрывает мастер-раннер моста.
            # Клик из тика внутри конструктора вешает поток — только leave+флаг-файл.
            caption = CONSTRUCTOR_KINDS[kind]
            self._set_wizard_dialog(
                caption,
                [{"set": self._constructor_fields(kind, name, comment),
                  "report": list(self._constructor_fields(kind, name, comment))},
                 {"click": "OK"}])
            args["modalPolicy"] = "leave"
            try:
                self._call(session, "md_add_child", args, pid=pid, timeout=120.0)
                res = self._wait_constructor_child(pid, container, owner_path or "", kind,
                                                   expect, name)
            finally:
                # снимаем запись независимо от исхода — иначе мастер-раннер навсегда
                # перехватывает и РУЧНЫЕ конструкторы человека с этим заголовком
                self._clear_wizard_dialog(caption)
            if synonym and res.get("created"):
                self._call(session, "prop_set_internal",
                           self._a(container,
                                   path=(owner_path + "." if owner_path else "")
                                   + expect + "." + name,
                                   prop="Синоним",
                                   internal=self._synonym_internal(synonym),
                                   expectId=res["created"].get("id")),
                           pid=pid)
                res["synonym"] = synonym
            return res
        res = self._call(session, "md_add_child", args, pid=pid, timeout=300.0)
        if synonym and res.get("renamed") and res.get("created"):
            new_path = (owner_path + "." if owner_path else "") \
                + expect + "." + name
            self._call(session, "prop_set_internal",
                       self._a(container, path=new_path, prop="Синоним",
                               internal=self._synonym_internal(synonym),
                               expectId=res["created"].get("id")),
                       pid=pid)
            res["synonym"] = synonym
        return res

    def md_delete(self, session: int | None = None, container: int | None = None,
                  path: str = "", uuid: str = "") -> dict:
        """Удалить объект (только с явным uuid — инвариант 5). Перед удалением снимите
        md_children/md_object владельца; отчёт о ссылках частичный — штатный поиск ссылок
        уходит в окно поиска без API чтения. Отказ хранилища/поддержки — без побочных действий."""
        if not uuid:
            raise BridgeError("bad_args", "uuid обязателен (удаление только по явному uuid)")
        pid = self._pid(session)
        self._focus_tree(pid, container, path)
        return self._call(session, "md_delete",
                          self._a(container, path=path, uuid=uuid,
                                  noActivate=True, requireTree=True),
                          pid=pid, timeout=300.0)

    # ---------- конфигурация ----------

    def config_open(self, session: int | None = None) -> dict:
        """Открыть основную конфигурацию (без этого current — конфигурация БД).
        На ERP ~80-110 с: долгая операция, статус running в list_sessions."""
        return self._call(session, "config_open", {})

    def cfg_archive_cf(self, session: int | None = None, container: int | None = None) -> dict:
        """Снять архив cf штатной командой «Сохранить конфигурацию в файл» (точка отката:
        уходит последнее СОХРАНЁННОЕ состояние). Файл — в Y:\\Snegopat-mcp\\backups с меткой
        времени. На ERP — сотни секунд; статус running в list_sessions."""
        name = f"mcp-{time.strftime('%Y%m%d-%H%M%S')}.cf"
        return self._call(session, "cfg_archive_cf", self._a(container, file=name))

    def config_save(self, session: int | None = None, container: int | None = None,
                    archive: str | None = None, accept_warnings: bool = False) -> dict:
        """Сохранить конфигурацию. БЕЗ archive сама снимает архив cf (точка отката) и
        сохраняет; с archive — использует указанный (из ответа cfg_archive_cf).
        Замечания проверки конфигурации: accept_warnings=false — «Нет», замечания в ответе;
        true — «Да». Постусловие: isModified=false после сохранения."""
        pid = self._pid(session)
        if archive is None:
            arch = self.cfg_archive_cf(session=session, container=container)
            archive = arch.get("file")
            if not archive:
                raise BridgeError("archive_failed", "архив cf не создан, сохранение отменено")
        return self._call(session, "config_save",
                          self._a(container, archive=archive, acceptWarnings=accept_warnings),
                          pid=pid)

    def syntax_check(self, session: int | None = None) -> dict:
        """Синтаксическая проверка модулей (Config.SyntaxCheck); сообщения — из окна
        сообщений; errorLines — строки с ошибками. На больших конфигурациях — минуты."""
        return self._call(session, "syntax_check", {})

    def update_db_cfg(self, session: int | None = None, container: int | None = None) -> dict:
        """Обновить конфигурацию БД. Только для баз из own_bases конфига сервера
        (нормализованное сравнение строки соединения; пустой список — отказ всегда) и только
        при allow_update_db. Незнакомые диалоги закрываются Отменой с отказом. НЕ ВЫПОЛНЕНО
        вживую (этап 5): перед первым использованием прогоните на своей тестовой базе."""
        if not self.cfg.allow_update_db:
            raise BridgeError("forbidden",
                              "update_db_cfg запрещён конфигурацией сервера (allow_update_db)")
        if not self.cfg.own_bases:
            raise BridgeError("forbidden", "own_bases пуст — обновление БД запрещено (инвариант 5)")
        return self._call(session, "update_db_cfg",
                          self._a(container, own_bases=self.cfg.own_bases,
                                  allowedDialogs=self.cfg.update_db_allowed_dialogs))

    # ---------- хранилище ----------

    def cfgstore_capture(self, session: int | None = None, container: int | None = None,
                         path: str = "", recursive: bool = False) -> dict:
        """Захватить объект в хранилище (диалог с проверкой заголовка, recursive=false —
        только сам объект). Требует allow_store в конфиге сервера. НЕ ВЫПОЛНЕНО вживую:
        перед использованием проверьте на своём тестовом хранилище."""
        self._require_store()
        if not path:
            raise BridgeError("bad_args", "path обязателен")
        return self._call(session, "cfgstore_capture", self._a(container, path=path, recursive=recursive))

    def cfgstore_commit(self, session: int | None = None, container: int | None = None,
                        path: str = "", comment: str = "") -> dict:
        """Поместить ОДИН объект в хранилище с непустым комментарием (инвариант 5);
        поле комментария ищется по известным именам контролов, незнакомый диалог — Отмена
        и отказ. Требует allow_store. НЕ ВЫПОЛНЕНО вживую (см. cfgstore_capture)."""
        self._require_store()
        if not path or not comment.strip():
            raise BridgeError("bad_args", "path и непустой comment обязательны")
        return self._call(session, "cfgstore_commit", self._a(container, path=path, comment=comment))

    def _require_store(self) -> None:
        if not self.cfg.allow_store:
            raise BridgeError("forbidden",
                              "операции хранилища запрещены конфигурацией сервера (allow_store)")

    # ---------- формы (внутренний текст, этап 8) ----------

    def form_export(self, session: int | None = None, container: int | None = None,
                    path: str = "", prop: str = "Форма", file: str = "") -> dict:
        """Выгрузить форму (макет) во внутреннем текстовом формате 1С (UTF-8 BOM) в
        Y:\\Snegopat-mcp\\mcp\\forms\\<file> — редактор формы открывается мостом.
        Это НЕ XML выгрузки конфигурации; перевод XML <-> внутренний текст — вне MVP."""
        if not file:
            raise BridgeError("bad_args", "file обязателен ([A-Za-z0-9_.-])")
        return self._call(session, "form_save", self._a(container, path=path, prop=prop, file=file))

    def form_import(self, session: int | None = None, container: int | None = None,
                    path: str = "", prop: str = "Форма", file: str = "") -> dict:
        """Загрузить форму из внутреннего текста (файл из form_export). Правка применяется
        к открытой форме; isModified конфигурации НЕ выставляется — сохранение за вами.
        Редактор формы должен быть закрыт перед вызовом (иначе затрутся несохранённые правки)."""
        if not file:
            raise BridgeError("bad_args", "file обязателен")
        return self._call(session, "form_load", self._a(container, path=path, prop=prop, file=file))

    def form_get_xml(self, session: int, obj_path: str, form_name: str = "",
                     owner_path: str = "", cf_path: str = "",
                     temp_base: str = "", cf_max_age_s: float = 0) -> dict:
        """XML формы из cf последнего сохранённого состояния (этап 8 §3.1). ТЯЖЁЛОЕ:
        минуты (cf конфигурации + temp-база). obj_path — владелец («Справочник.X» /
        «ОбщаяФорма.X»); form_name — имя формы («ФормаЭлемента», пусто для общей);
        owner_path — для снимка состава К2 (по умолчанию obj_path); cf_path — готовый
        cf (иначе снимается свежий); temp_base — готовая temp-база из этого cf (иначе
        строится); cf_max_age_s — переиспользовать архив из кэша, если моложе (0 =
        всегда свежий). Кэш М2: повторное чтение той же формы — мгновенно (XML по
        хешу записи cf); temp_read переиспользуется для того же cf. При
        isModified=true — ОТКАЗ (несохранённые правки, К1).
        Ответ: xml, snapshot (передайте в form_set_xml), form_uuid, cache, cf, ms."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from form_xml import form_get_xml as _impl, FormXmlError
        try:
            return _impl(session, obj_path, form_name,
                         owner_path or obj_path,
                         cf_path or None,
                         temp_base=Path(temp_base) if temp_base else None,
                         cf_max_age_s=cf_max_age_s)
        except FormXmlError as e:
            raise BridgeError("form_xml", str(e))

    def form_set_xml(self, session: int, obj_path: str, xml: str, edit_marker: str = "",
                     owner_path: str = "",
                     expected_snapshot: dict | None = None) -> dict:
        """Записать правку XML формы в живую базу (этап 8 §3.2 ред. 4). ТЯЖЁЛОЕ (8–12 мин
        на конфигурации 2 ГБ); конфигуратор ОСТАЁТСЯ ОТКРЫТЫМ (загрузка cf2 — штатной
        командой «Загрузить конфигурацию из файла» через мост; пакетный /LoadCfg после
        kill упирался в «1Cv8.cdn. Схема не зарегистрирована» и удалён). Цепочка:
        cf → temp_write → правка → постусловия → cf2 → cfg_load_cf → update_db_cfg →
        верификация (cf живой == cf2 по тексту; edit_marker в живом form_save).
        xml — ПОЛНЫЙ текст Form.xml (правьте XML из form_get_xml); edit_marker —
        подстрока-признак правки (проверяется в выгрузке temp_write и живой форме);
        expected_snapshot — снимок из form_get_xml (расхождение состава владельца =
        отказ, К2). Валидация XML — на вас (скил form-validate) ДО вызова; запись при
        isModified=true отказана. update_db_cfg требует базу в own_bases конфига."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from form_xml import form_set_xml as _impl, FormXmlError

        def _edit(old_xml: str) -> str:
            if not xml.strip():
                raise FormXmlError("xml пуст — правка отказана")
            if old_xml == xml:
                raise FormXmlError("xml не отличается от текущего — правка отказана")
            return xml

        try:
            return _impl(session, obj_path, "", owner_path or obj_path, _edit,
                         edit_marker=edit_marker or None,
                         expected_snapshot=expected_snapshot)
        except FormXmlError as e:
            raise BridgeError("form_xml", str(e))

    # ---------- расширения (этап 13) ----------

    def ext_list(self, session: int | None = None) -> dict:
        """Расширения ЖИВОЙ сессии из session_info: container (индекс), identifier, root,
        isModified. ВИДИМЫ ТОЛЬКО ОТКРЫТЫЕ (дерево расширения открыто); созданное и не
        открытое расширение в списке НЕ появится (факт П1/П2, docs/stage-13-extensions.md).
        Записи в расширение — только со свежим ext_list + container=N; в ответе период
        heartbeat моста — не работайте по протухшему списку."""
        si = self._call(session, "session_info", {})
        exts = []
        for c in si.get("containers", []):
            ident = str(c.get("identifier") or "")
            if "Расширение конфигурации" in ident and "(база данных)" not in ident:
                exts.append({"container": c.get("index"), "identifier": ident,
                             "root": str(c.get("root") or ""),
                             "isModified": c.get("isModified"),
                             "isCurrent": c.get("isCurrent")})
        return {"extensions": exts, "heartbeatAge_s": si.get("heartbeatAge_s")}

    def ext_dump_cfe(self, session: int, name: str, file: str, base_path: str = "") -> dict:
        """Выгрузить СОХРАНЁННОЕ расширение в .cfe пакетно (/DumpCfg -Extension).
        Требования: расширение в ext_list с isModified=false (cfe снимает только
        сохранённое); file — [A-Za-z0-9_.-].cfe; base_path — база-источник (по умолчанию
        база сессии; пакетный вызов открывает независимое соединение — на занятой базе
        возможен отказ, тогда скопируйте базу). Загрузка в слот: mockup_init +
        /LoadCfg <file> -Extension <name> (docs/stage-13-extensions.md §9)."""
        import re as _re
        import subprocess as _sp
        import os as _os
        if not _re.fullmatch(r"[A-Za-z0-9_.\-]+\.cfe", file):
            raise BridgeError("bad_args", "file: [A-Za-z0-9_.-].cfe")
        exts = self.ext_list(session=session).get("extensions", [])
        mine = [e for e in exts if name in (e.get("root") or "") or name in (e.get("identifier") or "")]
        if not mine:
            raise BridgeError("bad_args",
                              f"расширение «{name}» не найдено среди открытых (ext_list)")
        if any(e.get("isModified") for e in mine):
            raise BridgeError("bad_args",
                              "расширение изменено (isModified=true) — сохраните сначала: "
                              "cfe снимает только сохранённое состояние")
        base = base_path or str(_session_base_path(self.bridge, session))
        out = r"Y:\Snegopat-mcp\backups\ext-%s" % file
        # пакетный вызов на базе, открытой живым конфигуратором, падает
        # «1Cv8.cdn. Схема не зарегистрирована» (28.10.2026) — работаем по КОПИИ базы
        # (1Cv8.1CD + служебные; занятые 1Cv8.cgr/1Cv8Log и .cfl пропускаются)
        import shutil as _sh
        work_base = r"Y:\Snegopat-mcp\temp\extdump-base"
        _sh.rmtree(work_base, ignore_errors=True)
        _os.makedirs(work_base, exist_ok=True)
        copied = []
        for _f in _os.listdir(base):
            if _f in ("1Cv8.cgr", "1Cv8Log") or _f.endswith(".cfl"):
                continue
            _s = _os.path.join(base, _f)
            if _os.path.isfile(_s):
                _sh.copy2(_s, _os.path.join(work_base, _f))
                copied.append(_f)
        if "1Cv8.1CD" not in copied:
            raise BridgeError("ext_dump_failed", "1Cv8.1CD базы скопировать не удалось (занят?)")
        r = _sp.run([r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe", "DESIGNER", "/DisableStartupDialogs",
                     "/F", work_base, "/DumpCfg", out, "-Extension", name,
                     "/Out", out + ".log", "/DumpResult", out + ".res"],
                    capture_output=True, timeout=600)
        res = ""
        try:
            res = open(out + ".res", encoding="utf-8-sig", errors="replace").read().strip()
        except OSError:
            pass
        if r.returncode != 0 or res not in ("0", ""):
            raise BridgeError("ext_dump_failed", f"rc={r.returncode}, res={res}")
        if not _os.path.isfile(out) or _os.path.getsize(out) == 0:
            raise BridgeError("ext_dump_failed", "cfe не создан")
        return {"file": out, "size": _os.path.getsize(out), "res": res}

    def ext_save(self, session: int, name: str) -> dict:
        """Сохранить расширение (штатное «Сохранить документ» при активном дереве
        расширения — Frame.FileSave). Проверено живьём (28.10.2026): isModified → False,
        правки попадают в cfe. Дерево расширения должно быть открыто (ext_list);
        перед сохранением правки агента — снимите cfe-точку отката (ext_dump_cfe)."""
        exts = self.ext_list(session=session).get("extensions", [])
        mine = [e for e in exts if name in (e.get("root") or "") or name in (e.get("identifier") or "")]
        if not mine:
            raise BridgeError("bad_args", f"расширение «{name}» не найдено среди открытых (ext_list)")
        title = mine[0]["identifier"].replace("Расширение конфигурации ", "").strip('"')
        self._call(session, "view_activate", {"title": title})
        r = self._call(session, "editor_commit", {"title": title, "num": 4})
        import time as _t
        _t.sleep(2)
        after = {e["root"]: e["isModified"] for e in self.ext_list(session=session)["extensions"]}
        if after.get(name, True):
            raise BridgeError("ext_save_failed", "isModified не сброшен после сохранения")
        return r

    def ext_create(self, session: int, name: str, prefix: str, synonym: str = "",
                   purpose: int = 1) -> dict:
        """Создать расширение: менеджер «Расширения конфигурации» → Ins → диалог
        (НЕмодальный) заполняет мастер-раннер по флаг-файлу → ОК (механика П2).
        ПРЕДУСЛОВИЕ: окно менеджера закрыто ИЛИ открыто без выделенной строки —
        раннер сработает на ОТКРЫТИИ диалога. Постусловие: расширение существует —
        пакетная проверка по КОПИИ базы (/DumpCfg -Extension; факт П1: созданное и
        не открытое расширение в openedCount НЕ появляется). ПОВТОРНЫЙ Ins при
        существующем имени даёт неуправляемую модалку «не уникально» (М-5) —
        существование проверяется ДО отправки Ins."""
        import json as _json
        import subprocess as _sp
        import os as _os
        import time as _t
        base = str(_session_base_path(self.bridge, session))
        # 1. негатив: расширение уже есть? (пакетная проверка по копии)
        if _ext_exists_packed(base, name):
            raise BridgeError("bad_args", f"расширение «{name}» уже есть в базе")
        # 2. флаг мастер-раннера на диалог «Новое расширение конфигурации»
        flag = r"Y:\Snegopat-mcp\mcp\mcp_stg_wizard.json"
        job = {"dialogs": [{"caption": "Новое расширение конфигурации", "steps": [
            {"set": {"EditName": name, "EditPrefix": prefix, "EditSynonym": synonym,
                     "EditUsePurpose": purpose},
             "report": ["EditName", "EditPrefix", "EditSynonym", "EditUsePurpose"]},
            {"click": "OkButton"}]}]}
        tmp = flag + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_json.dumps(job, ensure_ascii=False, indent=1))
        _os.replace(tmp, flag)
        try:
            self._call(session, "view_activate", {"title": "Расширения конфигурации"})
            self._call(session, "key_send", {"key": 0x2D})  # Ins — открывает диалог
            # 3. постусловие: пакетная проверка по копии (диалог закрыт раннером)
            deadline = _t.time() + 30
            ok = False
            while _t.time() < deadline and not ok:
                _t.sleep(3)
                try:
                    ok = _ext_exists_packed(base, name)
                except Exception:
                    ok = False
            if not ok:
                raise BridgeError("ext_create_failed",
                                  "расширение не создано за 30 с — проверьте wizard.log/модалки "
                                  "(повторный Ins при существующем имени даёт зомби-модалку)")
            return {"created": name, "verified": "packed-check"}
        finally:
            _os.replace(flag + ".clean", flag) if _os.path.isfile(flag + ".clean") else None
            with open(flag, "w", encoding="utf-8") as f:
                f.write(_json.dumps({"dialogs": []}))
            _os.remove(tmp) if _os.path.isfile(tmp) else None

    def ext_open(self, session: int, name: str) -> dict:
        """Открыть дерево расширения (контейнер появится в session_info): менеджер →
        Enter на выделенной строке. ОГРАНИЧЕНИЕ (М-4/факт П1): мост не умеет выделять
        строку грида менеджера по имени — работает только когда расширение УЖЕ
        выделено (единственное созданное/последнее созданное). Постусловие: контейнер
        с root==name в session_info."""
        import time as _t
        self._call(session, "view_activate", {"title": "Расширения конфигурации"})
        self._call(session, "key_send", {"key": 0x0D})  # Enter
        deadline = _t.time() + 20
        while _t.time() < deadline:
            _t.sleep(2)
            for e in self.ext_list(session=session)["extensions"]:
                if e["root"] == name:
                    return {"opened": name, "container": e["container"]}
        raise BridgeError("ext_open_failed",
                          "контейнер не появился: строка расширения не была выделена в менеджере "
                          "(выделите вручную либо откройте дерево вручную)")

    # ---------- служебное ----------

    def tree_activate(self, session: int | None = None, container: int | None = None,
                      path: str | None = None) -> dict:
        """Выделить объект в дереве метаданных конфигуратора (видно человеку)."""
        return self._call(session, "tree_activate", self._a(container, path=path))

    def tree_activate_group(self, session: int | None = None, container: int | None = None,
                            path: str | None = None, kind: str = "") -> dict:
        """Выделить узел группы подчинённых (например Реквизиты) — диагностика навигации."""
        return self._call(session, "tree_activate_group", self._a(container, path=path, kind=kind))

    def dialog_probe(self, session: int | None = None, cmd_group: str = "",
                     num: int = 0) -> dict:
        """ПРОБА диалога команды: открыть модальное окно команды (cfgstore: 150-153,
        config: 9, 29), перечислить контролы, закрыть Отменой. Ничего не выполняет.
        Для снятия новых диалогов (этап 5)."""
        return self._call(session, "dialog_probe", {"cmdGroup": cmd_group, "num": num})

    def bridge_wait(self, session: int | None = None, id: str = "") -> dict:
        """Состояние запроса по id: done | running | queued | unknown (после таймаута
        клиента длинной операции — операция могла завершиться; ответ берите повтором вызова)."""
        return self._call(session, "wait", {"id": id})

    def bridge_stats(self, session: int | None = None) -> dict:
        """Статистика моста: тики, busy, ошибки, последняя ошибка."""
        return self._call(session, "timer_stats", {})

    def focus_probe(self, session: int | None = None) -> dict:
        """Диагностика фокуса ввода: focusHwnd (0 — окно конфигуратора в фоне, операции
        дерева откажут), modalMode, активное представление."""
        return self._call(session, "focus_probe", {})

    # ---------- вспомогательное ----------

    def _pid(self, session: int | None) -> int:
        return self.bridge.pick_session(session)

    def _a(self, container: int | None, **kw) -> dict:
        args = {k: v for k, v in kw.items() if v is not None}
        if container is not None:
            args["container"] = container
        return args

    def _call(self, session: int | None, op: str, args: dict,
              pid: int | None = None, timeout: float | None = None) -> dict:
        if pid is None:
            pid = self._pid(session)
        return self.bridge.call(pid, op, args, timeout=timeout)

    # ---------- конструкторные виды (ОбщиеФормы/ОбщиеМакеты) ----------

    # флаг-файл читается мостом на каждом модальном окне; запись — атомарная,
    # под замком (sync-инструменты SDK идут в пуле потоков — ревью 25.10.2026)
    _wizard_lock = threading.Lock()

    @staticmethod
    def _constructor_fields(kind: str, name: str, comment: str | None) -> dict:
        """Поля конструктора, снятые дампером модалей 24.10.2026 (Ident в обоих)."""
        fields: dict = {"Ident": name}
        if comment:
            fields["Comment"] = comment
        return fields

    def _wizard_flag_path(self) -> Path:
        return Path(self.cfg.root) / "mcp_stg_wizard.json"

    def _set_wizard_dialog(self, caption: str, steps: list[dict]) -> None:
        """Прописать диалог в флаг-файл мастер-раннера (мост перечитывает его
        на каждом модальном окне). Запись атомарная (tmp + os.replace)."""
        with self._wizard_lock:
            self._write_wizard_dialogs(self._read_wizard_dialogs(), caption, steps)

    def _clear_wizard_dialog(self, caption: str) -> None:
        """Удалить диалог из флаг-файла: запись ОБЯЗАТЕЛЬНО снимается после
        создания — иначе мастер-раннер навсегда «закрывает» и РУЧНЫЕ конструкторы
        человека с этим заголовком (ревью 25.10.2026, HIGH)."""
        with self._wizard_lock:
            dialogs = self._read_wizard_dialogs()
            if any(d.get("caption") == caption for d in dialogs):
                self._write_wizard_dialogs(dialogs, caption, None)

    def _read_wizard_dialogs(self) -> list[dict]:
        flag = self._wizard_flag_path()
        if not flag.is_file():
            return []
        try:
            data = json.loads(flag.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, dict):
            return []
        dialogs = data.get("dialogs", [])
        return dialogs if isinstance(dialogs, list) else []

    def _write_wizard_dialogs(self, dialogs: list[dict], caption: str,
                              steps: list[dict] | None) -> None:
        dialogs = [d for d in dialogs if d.get("caption") != caption]
        if steps is not None:
            dialogs.append({"caption": caption, "steps": steps})
        flag = self._wizard_flag_path()
        flag.parent.mkdir(parents=True, exist_ok=True)
        tmp = flag.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"dialogs": dialogs}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, flag)

    def _wait_constructor_child(self, pid: int, container: int | None,
                                 owner_path: str, kind: str, expect: str,
                                 name: str, timeout_s: float = 15.0) -> dict:
        """Дождаться объекта, созданного конструктором (мастер-раннер закрывает его
        из простоя), и вернуть ответ в формате md_add_child."""
        base_path = (owner_path + "." if owner_path else "") + expect + "." + name
        deadline = time.time() + timeout_s
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                obj = self._call(pid, "md_object", self._a(container, path=base_path),
                                 pid=pid)
                if obj.get("cls") == expect:
                    return {"created": {"id": obj["id"], "name": obj["name"],
                                        "cls": obj["cls"]},
                            "renamed": True, "stray": [], "via": "wizard-flag"}
            except BridgeError as e:
                last_err = e
            time.sleep(0.4)
        raise BridgeError("constructor_timeout",
                          f"конструктор не создал {expect}.{name} за {timeout_s:.0f} с"
                          + (f"; последняя ошибка: {last_err}" if last_err else ""))

    def _focus_tree(self, pid: int, container: int | None, path: str | None) -> None:
        from . import winutils
        winutils.activate_window(pid, wait_s=1.0)  # best-effort снаружи
        # дерево расширения имеет заголовок = отображаемое имя расширения: для
        # неосновного контейнера принимаем ЛЮБОЙ заголовок, кроме дерева основной
        # конфигурации (этап 13, М-6 — уход клавиш в основное дерево = порча)
        ext_mode = container is not None and container > 0
        title = None
        for _ in range(3):
            self.bridge.call(pid, "tree_activate", self._a(container, path=path))
            av = self.bridge.call(pid, "active_view", {})
            title = (av or {}).get("title")
            if title and (str(title).startswith("Конфигурация") or
                          (ext_mode and not str(title).startswith("Конфигурация"))):
                break
        else:
            raise BridgeError("no_tree_focus", f"фокус не в дереве метаданных: активно «{title}»")
        if ext_mode and title and str(title).startswith("Конфигурация"):
            raise BridgeError("no_tree_focus",
                              "фокус в дереве ОСНОВНОЙ конфигурации при операции в расширении")
        # активация изнутри процесса (SendKeys+AppActivate) — цикл сообщений доставит
        # фокус между тиками; ждём подтверждения
        for _ in range(8):
            if self.bridge.call(pid, "focus_probe", {}).get("focusHwnd"):
                return
            self.bridge.call(pid, "activate_self", {})
            time.sleep(0.35)
        raise BridgeError("no_input_focus",
                          "окну конфигуратора не передан фокус ввода — операции дерева невозможны")


# ---------- сборка MCP-сервера ----------

# (метод Toolkit, имя инструмента) — описание берётся из docstring метода
_TOOLS = [
    "list_sessions", "session_info",
    "md_tree", "md_object", "md_children", "md_find", "module_get",
    "type_lookup", "md_counts", "md_walk",
    "module_set", "module_patch",
    "md_set_property", "md_set_property_internal", "md_set_synonym",
    "md_set_type", "md_set_type_internal",
    "md_add_child", "md_delete",
    "config_open", "cfg_archive_cf", "config_save", "syntax_check", "update_db_cfg",
    "cfgstore_capture", "cfgstore_commit",
    "form_export", "form_import", "form_get_xml", "form_set_xml",
    "ext_list", "ext_dump_cfe", "ext_save", "ext_create", "ext_open",
    "tree_activate", "tree_activate_group", "dialog_probe",
    "bridge_wait", "bridge_stats", "focus_probe",
]


def build_server(cfg: Config | None = None) -> MCPServer:
    cfg = cfg or load_config()
    tk = Toolkit(cfg)
    mcp = MCPServer(name="mcp-snegopat", version=__version__, instructions=INSTRUCTIONS)
    for name in _TOOLS:
        mcp.add_tool(getattr(tk, name), name=name)
    return mcp


def main() -> None:
    mcp = build_server()
    mcp.run("stdio")


if __name__ == "__main__":
    main()
