"""MCP-сервер (stdio) живого конфигуратора 1С через Снегопат.

Инструменты читают и правят конфигурацию в ОТКРЫТОМ конфигураторе человека:
дерево метаданных, свойства, модули, создание/удаление объектов (этап 7),
сохранение с архивной точкой отката, синтакс-проверка, хранилище и обновление
БД — с разрешениями из конфига сервера (инвариант 5 плана).

Запуск: mcp-snegopat (после pip install) или python -m mcp_snegopat.
"""
from __future__ import annotations

import json
import re
import sys
import os
import threading
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import __version__
from .bridge import Bridge, BridgeError, NoSessionError
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
    import uuid as _uuid
    base = str(base)
    work = "Y:/Snegopat-mcp/temp/extcheck-%s" % _uuid.uuid4().hex[:8]
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

    _SYN_LANGS = ("ru", "en")

    def _synonym_internal(self, synonyms: dict) -> str:
        """Внутреннее представление локализуемой строки: {1,"ru","…"},{1,"en","…"}.
        Кавычка/обратный слэш/переводы строки ломают сериализацию (ревью 25.10) —
        явный отказ. synonym (str) — как {"ru": synonym}."""
        if isinstance(synonyms, str):
            synonyms = {"ru": synonyms}
        bad_keys = [k for k in synonyms if k not in self._SYN_LANGS]
        if bad_keys:
            raise BridgeError("bad_args",
                              "языки синонима: %s (передано: %s)"
                              % (", ".join(self._SYN_LANGS), ", ".join(sorted(bad_keys))))
        parts = []
        for lang in self._SYN_LANGS:
            if lang not in synonyms:
                continue
            text = synonyms[lang]
            if not text:
                raise BridgeError("bad_args", "пустой текст синонима (%s)" % lang)
            bad = [c for c in text if c in '"\\\n\r\t']
            if bad:
                raise BridgeError("bad_args",
                                  f"синоним ({lang}) содержит недопустимые символы {bad!r} "
                                  "(кавычка, обратный слэш, переводы строк)")
            parts.append('{1,"%s","%s"}' % (lang, text))
        if not parts:
            raise BridgeError("bad_args", "synonyms обязателен")
        return '{"#",%s,\n%s\n}' % (self.SYN_TYPE_GUID, ",\n".join(parts))

    def md_set_synonym(self, session: int | None = None, container: int | None = None,
                       path: str = "", synonym="", synonyms: dict | None = None,
                       expect_id: str | None = None) -> dict:
        """Записать СИНОНИМ объекта. Строкой setProperty не пишется (спайк этапа 1) —
        пишется внутренним представлением локализуемой строки; постусловие сверяет
        внутреннее значение. Проверено живьём 24.10.2026. Многоязычность (№25):
        synonyms={"ru": "…", "en": "…"} — обе локали в одном представлении;
        synonym="…" — как {"ru": synonym}."""
        syn = synonyms if synonyms else synonym
        if not syn:
            raise BridgeError("bad_args", "synonym или synonyms обязателен")
        return self._call(session, "prop_set_internal",
                          self._a(container, path=path, prop="Синоним",
                                  internal=self._synonym_internal(syn),
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

    def md_rename(self, session: int | None = None, container: int | None = None,
                  path: str = "", new_name: str = "", uuid: str = "",
                  cascade: bool = False, dump_dir: str = "") -> dict:
        """Каскадное переименование (IDEAS №17): md_set_property(Имя) + переписывание
        литералов ссылок в МОДУЛЯХ ссылающихся объектов (EN-квалификаторы Document.X/
        DocumentRef.X…, рус менеджеры Документы.X, рус типы ДокументСсылка.X;
        ОбщийМодуль — голое имя при длине >= 5). Каскад ищет цели по XML-ВЫГРУЗКЕ
        (dump_dir — свежая от md_check_refs; при cascade=true без dump_dir — отказ:
        выгрузка снимается там же за минуты на 2 ГБ). Каждый модуль переписывается
        module_set с expected_hash (страж от параллельных правок). Формы НЕ
        переписываются автоматически — в ответе forms (их контур form_get_xml/
        form_set_xml). Типы реквизитов/подсистемы не ломаются — переименование
        сохраняет uuid. Верификация: post md_object по новому пути с uuid."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from ref_rewrite import rewrite_refs, module_targets_from_dump  # noqa: E402
        parts = path.split(".")
        if len(parts) != 2 or not new_name or not uuid:
            raise BridgeError("bad_args",
                              "path — Класс.Имя, new_name и uuid обязательны (сверка адресата)")
        cls, old = parts
        src = self._call(session, "md_object", self._a(container, path=path, uuid=uuid))
        res: dict = {"renamed": {k: src.get(k) for k in ("name", "id", "cls")},
                     "new_name": new_name, "cascade": bool(cascade)}
        rename = self.md_set_property(session=session, container=container,
                                      path=path, prop="Имя", value=new_name, uuid=uuid)
        res["set"] = rename.get("after")
        if not cascade:
            res["note"] = "каскад выключен: ссылки в модулях НЕ переписаны (md_check_refs)"
            return res
        if not dump_dir:
            raise BridgeError("bad_args",
                              "cascade=true требует dump_dir (свежая выгрузка от md_check_refs)")
        targets, forms = module_targets_from_dump(dump_dir, old)
        rewritten, failed = [], []
        allow_bare = cls == "ОбщийМодуль" and len(old) >= 5
        for t in targets:
            try:
                mod = self.module_get(session=session, container=container,
                                      path=t["path"], prop=t["prop"], with_text=True)
                text = mod.get("text") or ""
                new_text = rewrite_refs(text, cls, old, new_name, allow_bare=allow_bare)
                if new_text == text:
                    rewritten.append({**t, "status": "no-change"})
                    continue
                self.module_set(session=session, container=container, path=t["path"],
                                prop=t["prop"], text=new_text,
                                expected_hash=mod.get("hash"))
                rewritten.append({**t, "status": "rewritten"})
            except BridgeError as e:
                failed.append({**t, "error": str(e)[:120]})
        post = self._call(session, "md_object",
                          self._a(container, path=cls + "." + new_name, uuid=uuid))
        res.update({"rewritten": rewritten, "forms": forms, "failed": failed,
                    "post": {k: post.get(k) for k in ("name", "id", "cls")}})
        if forms:
            res["warning"] = ("модули форм с литералами (%d шт) НЕ переписаны "
                              "автоматически — form_get_xml/form_set_xml" % len(forms))
        return res

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
                  path: str = "", uuid: str = "", confirm: bool = False,
                  literal_action: str = "none", dump_dir: str = "") -> dict:
        """Удалить объект (только с явным uuid — инвариант 5). Перед удалением снимите
        md_children/md_object владельца; отчёт о ссылках частичный — штатный поиск ссылок
        уходит в окно поиска без API чтения. Отказ хранилища/поддержки — без побочных действий.
        Двухфазное подтверждение (№19): confirm=False (по умолчанию) — ТОЛЬКО план:
        объект найден, md_check_refs, предупреждения; удаление НЕ выполняется.
        confirm=True — само удаление.

        Каскадная зачистка (№17 завершение): literal_action="comment" — после
        удаления строки с литералами старого имени в ссылающихся модулях
        (цели из dump_dir — выгрузка от md_check_refs, снятая ДО удаления)
        закомментируются «// », каждый модуль — module_set с expected_hash-стражем.
        Best effort: литерал в середине многострочного выражения комментированием
        строки не лечится — после зачистки прогоните syntax_check.
        literal_action="none" (умолчание) — только отчёт."""
        if not uuid:
            raise BridgeError("bad_args", "uuid обязателен (удаление только по явному uuid)")
        if literal_action not in ("none", "comment"):
            raise BridgeError("bad_args", "literal_action: none|comment")
        if literal_action == "comment" and not dump_dir:
            raise BridgeError("bad_args",
                              "literal_action=comment требует dump_dir (выгрузка "
                              "от md_check_refs, снятая ДО удаления)")
        pid = self._pid(session)
        if not confirm:
            plan: dict = {"confirm_required": True, "path": path, "uuid": uuid,
                          "action": "md_delete", "literal_action": literal_action}
            try:
                obj = self._call(session, "md_object", self._a(container, path=path, uuid=uuid),
                                 pid=pid)
                plan["object"] = {k: obj.get(k) for k in ("name", "id", "cls")}
            except BridgeError as e:
                raise BridgeError("bad_args", "объект не найден: %s" % e)
            try:
                refs = self.md_check_refs(session=session,
                                          object_name=plan["object"].get("name") or "")
                plan["references"] = {k: refs.get(k) for k in
                                      ("literals_total", "objects", "roles_subscriptions")
                                      if k in refs}
                refs_n = refs.get("literals_total")
                if refs_n:
                    plan["warning"] = ("найдено ссылок: %s — удаление сломает ссылки" % refs_n)
            except (BridgeError, TypeError):
                plan["references"] = None
                refs_n = 0
            plan["next"] = ("повторите с confirm=true для удаления"
                            + ("; literal_action=comment + dump_dir — зачистка литералов"
                               if refs_n else ""))
            return plan
        old_name = path.split(".")[-1]
        self._focus_tree(pid, container, path)
        res = self._call(session, "md_delete",
                         self._a(container, path=path, uuid=uuid,
                                 noActivate=True, requireTree=True),
                         pid=pid, timeout=300.0)
        res["literal_action"] = literal_action
        if literal_action == "comment":
            import sys as _sys
            sys_tools = Path(__file__).resolve().parents[1] / "tools"
            if str(sys_tools) not in _sys.path:
                _sys.path.insert(0, str(sys_tools))
            from ref_rewrite import module_targets_from_dump  # noqa: E402
            targets, _forms = module_targets_from_dump(dump_dir, old_name)
            commented, failed = [], []
            # \b: литералы «Документы.Старое» после точки — тоже битые ссылки
            # после удаления; \b матчит (в отличие от rename, где точка-префикс
            # означает член-доступ живого объекта)
            needle = re.compile(r'\b' + re.escape(old_name) + r'\b')
            for t in targets:
                try:
                    mod = self.module_get(session=session, container=container,
                                          path=t["path"], prop=t["prop"],
                                          with_text=True)
                    text = mod.get("text") or ""
                    eol = "\r\n" if "\r\n" in text else "\n"
                    new_lines, changed = [], 0
                    for line in text.split(eol):
                        if (needle.search(line) and not line.lstrip().startswith('//')):
                            new_lines.append('// ' + line)
                            changed += 1
                        else:
                            new_lines.append(line)
                    if changed:
                        self.module_set(session=session, container=container,
                                        path=t["path"], prop=t["prop"],
                                        text=eol.join(new_lines),
                                        expected_hash=mod.get("hash"))
                        commented.append({**t, "lines_commented": changed})
                except BridgeError as e:
                    failed.append({**t, "error": str(e)[:120]})
            res.update({"literals_commented": commented, "literals_failed": failed})
        return res

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

    def update_db_cfg(self, session: int | None = None, container: int | None = None,
                      confirm: bool = False) -> dict:
        """Обновить конфигурацию БД. Только для баз из own_bases конфига сервера
        (нормализованное сравнение строки соединения; пустой список — отказ всегда) и только
        при allow_update_db. Незнакомые диалоги закрываются Отменой с отказом. НЕ ВЫПОЛНЕНО
        вживую (этап 5): перед первым использованием прогоните на своей тестовой базе.
        Двухфазное подтверждение (№19): confirm=False — план (права, isModified
        контейнера, предупреждение о применении изменений к БД); confirm=True —
        само обновление."""
        if not self.cfg.allow_update_db:
            raise BridgeError("forbidden",
                              "update_db_cfg запрещён конфигурацией сервера (allow_update_db)")
        if not self.cfg.own_bases:
            raise BridgeError("forbidden", "own_bases пуст — обновление БД запрещено (инвариант 5)")
        if not confirm:
            si = self.session_info(session=session)
            conts = [c for c in si.get("containers", [])
                     if container is None or c.get("index") == container]
            plan = {"confirm_required": True, "action": "update_db_cfg",
                    "own_bases_ok": True,
                    "containers": [{k: c.get(k) for k in ("index", "identifier", "isModified")}
                                   for c in conts]}
            mod = [c for c in plan["containers"] if c.get("isModified")]
            if mod:
                plan["warning"] = ("контейнер(ы) изменены: %s — на БД применятся все правки "
                                   "основной конфигурации" % ", ".join(str(c["index"]) for c in mod))
            plan["next"] = "повторите с confirm=true для обновления"
            return plan
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
        """Выгрузить расширение в .cfe пакетно (/DumpCfg -Extension) по КОПИИ базы.
        Если расширение открыто (ext_list) — гейт isModified=false (cfe снимает только
        сохранённое). Если НЕ открыто (факт М-4: созданное не появляется в контейнерах),
        но существует в базе (пакетная проверка) — снимается как есть (созданное/не
        редактированное расширение сохранено по определению; после правок дерево нужно
        открыть и сохранить — иначе cfe неполный). file — [A-Za-z0-9_.-].cfe.
        Загрузка в слот: mockup_init + /LoadCfg <file> -Extension <name>."""
        import re as _re
        import subprocess as _sp
        import os as _os
        if not _re.fullmatch(r"[A-Za-z0-9_.\-]+\.cfe", file):
            raise BridgeError("bad_args", "file: [A-Za-z0-9_.-].cfe")
        open_status = "not-open"
        exts = self.ext_list(session=session).get("extensions", [])
        mine = [e for e in exts if name == (e.get("root") or "")
                or name in (e.get("identifier") or "").replace("Расширение конфигурации ", "").strip('"')]
        if not mine:
            base0 = base_path or str(_session_base_path(self.bridge, session))
            if not _ext_exists_packed(base0, name):
                raise BridgeError("bad_args",
                                  f"расширение «{name}» не найдено ни среди открытых, ни в базе")
        else:
            open_status = "open"
            if any(e.get("isModified") for e in mine):
                raise BridgeError("bad_args",
                                  "расширение изменено (isModified=true) — сохраните сначала "
                                  "(ext_save): cfe снимает только сохранённое состояние")
        base = base_path or str(_session_base_path(self.bridge, session))
        out = r"Y:\Snegopat-mcp\backups\ext-%s" % file
        # пакетный вызов на базе, открытой живым конфигуратором, падает
        # «1Cv8.cdn. Схема не зарегистрирована» (28.10.2026) — работаем по КОПИИ базы
        # (1Cv8.1CD + служебные; занятые 1Cv8.cgr/1Cv8Log и .cfl пропускаются)
        import shutil as _sh
        import uuid as _uu
        # уникальный каталог: параллельные вызовы используют один и тот же
        # фиксированный каталог → взаимный rmtree чужой копии (ревью v1.0.0 H)
        work_base = "Y:/Snegopat-mcp/temp/extdump-%s" % _uu.uuid4().hex[:8]
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
        return {"file": out, "size": _os.path.getsize(out), "res": res,
                "open_status": open_status}

    def ext_build_from_cf(self, cf1: str, cf2: str, name: str, prefix: str,
                          work: str = "", timeout_s: int = 7200) -> dict:
        """Собрать расширение из сравнения двух cf (конвейер этапов 14–15):
        cf_diff (XML-канон платформы) -> cfe-init -> cfe-borrow изменённых
        владельцев -> наполнение (Adopted-реквизиты с префиксом, замещающие формы
        с полным контентом) -> перехватчики модулей -> загрузка в слот -> cfe +
        отчёт. cf1 — вендорский эталон, cf2 — доработанная. ДОЛГАЯ операция:
        малые пары — минуты, 2 ГБ — ~50 мин (timeout_s >= 3600). work — рабочий
        каталог (повторный вызов на том же work идемпотентен). Непереносимое —
        в отчёте: manual (cf_diff), new_forms_cf2, new_objects_cf2."""
        import json as _json
        import subprocess as _sp
        import sys as _sys
        from pathlib import Path as _P
        if not name or not prefix:
            raise BridgeError("bad_args", "name и prefix обязательны")
        p1, p2 = _P(cf1), _P(cf2)
        for p in (p1, p2):
            if not p.is_file() or p.stat().st_size < 1000:
                raise BridgeError("bad_args", f"cf не найден: {p}")
        w = _P(work) if work else _P(r"Y:\Snegopat-mcp\verify\build-ext-mcp") / name
        script = _P(__file__).resolve().parents[1] / "tools" / "build_ext.py"
        # ревью M9: один work — одна сборка (параллельные портили бы слоты/src/отчёт);
        # открытие "x" — атомарное создание: существующий lock = чужая сборка
        import os as _os
        w.mkdir(parents=True, exist_ok=True)
        lock = w / ".build-ext.lock"
        try:
            with open(lock, "x", encoding="utf-8") as fh:
                fh.write(str(_os.getpid()))
        except FileExistsError:
            raise BridgeError(
                "build_ext_busy",
                f"work занят другой сборкой ({lock}); если процесс умер — удалите lock")
        try:
            # ревью M7: subprocess.run(timeout) снимает только прямого потомка —
            # внук 1cv8.exe продолжал бы менять слот; снимаем всё дерево
            p = _sp.Popen([_sys.executable, str(script), str(p1), str(p2),
                           "--name", name, "--prefix", prefix, "--work", str(w)],
                          stdout=_sp.PIPE, stderr=_sp.PIPE, text=True,
                          encoding="utf-8", errors="replace")
            try:
                out, err = p.communicate(timeout=timeout_s)
            except _sp.TimeoutExpired:
                _sp.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
                p.wait()
                raise BridgeError(
                    "build_ext_failed",
                    f"таймаут {timeout_s} с — дерево процессов снято (taskkill /T); "
                    "увеличьте timeout_s (2 ГБ пара ~50 мин)")
        finally:
            try:
                lock.unlink()
            except OSError:
                pass
        out = (out or "") + (err or "")
        report_file = w / "report.json"
        if p.returncode != 0:
            tail = "\n".join(out.strip().splitlines()[-6:])
            raise BridgeError("build_ext_failed", tail[:1500])
        report = {}
        if report_file.is_file():
            report = _json.loads(report_file.read_text(encoding="utf-8"))
        return {"cfe": report.get("cfe"), "vendor_ok": report.get("vendor_ok"),
                "steps": report.get("steps", [])[-8:], "forms": report.get("forms", []),
                "new_forms_cf2": report.get("new_forms_cf2", []),
                "new_objects_cf2": report.get("new_objects_cf2"),
                "deleted_forms_cf2": report.get("deleted_forms_cf2", []),
                "warning": report.get("warning"),
                "manual": report.get("manual"), "report": str(report_file)}

    def md_check_refs(self, object_name: str, dump_dir: str = "",
                      session: int | None = None, work: str = "",
                      timeout_s: int = 3600, max_context: int = 5) -> dict:
        """Поиск ссылок на объект ПЕРЕД удалением (этап 17): скан литералов типов
        (cfg:CatalogRef.X и производные) по полной XML-выгрузке; собственное
        поддерево объекта исключается по путям. Если dump_dir не указан — свежая
        выгрузка снимается: cfg_archive_cf (нужна живая сессия) -> temp-база ->
        /DumpConfigToFiles; на 2 ГБ это ~25 мин (timeout_s >= 1800). НЕ сканируются
        (свой формат): права ролей, подписки; тексты запросов в модулях.
        work — каталог выгрузки (переиспользуется между вызовами)."""
        import re as _re
        import shutil as _sh
        import subprocess as _sp
        import sys as _sys
        from pathlib import Path as _P
        sys_tools = _P(__file__).resolve().parents[1] / "tools"
        if not _re.fullmatch(r"[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z0-9_]*\.[\wА-ЯЁа-яё]+", object_name):
            raise BridgeError("bad_args", "object_name: «Вид.Имя»")
        kind = object_name.split(".", 1)[0]
        _sys.path.insert(0, str(sys_tools))
        from ref_check import KIND_TYPES, scan_dump
        if kind not in KIND_TYPES:
            raise BridgeError("bad_args", f"неизвестный вид: {kind}")
        d = _P(dump_dir) if dump_dir else _P("")
        if not d or not d.is_dir():
            # снимаем свежую выгрузку: cf живой базы -> temp-база -> полный дамп
            if session is None:
                raise BridgeError("bad_args",
                                  "укажите dump_dir со свежей выгрузкой или session "
                                  "(живой конфигуратор) для снятия новой")
            base = str(_session_base_path(self.bridge, session))
            w = _P(work) if work else _P(r"Y:\Snegopat-mcp\verify\ref-check") / \
                _re.sub(r"[^\w]", "_", object_name)
            w.mkdir(parents=True, exist_ok=True)
            arch = self.cfg_archive_cf(session=session)
            cf = _P(arch["file"])
            temp_base = w / "temp-base"
            _sh.rmtree(temp_base, ignore_errors=True)
            r = _sp.run([_sys.executable, str(sys_tools / "mockup_init.py"),
                         "--base", str(temp_base), "--from", str(cf), "--reset"],
                        capture_output=True, text=True, timeout=timeout_s)
            if r.returncode != 0:
                raise BridgeError("ref_check_failed",
                                  f"temp-база: {r.stdout[-200:]}{r.stderr[-200:]}")
            dump = w / "dump"
            _sh.rmtree(dump, ignore_errors=True)
            plat = r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe"
            res = w / "dump.res"
            r = _sp.run([plat, "DESIGNER", "/DisableStartupDialogs", "/F", str(temp_base),
                         "/DumpConfigToFiles", str(dump), "/Out", str(w / "dump.log"),
                         "/DumpResult", str(res)], capture_output=True, timeout=timeout_s)
            rt = res.read_text(encoding="utf-8-sig", errors="replace").strip() \
                if res.is_file() else "?"
            if r.returncode != 0 or rt not in ("0", ""):
                raise BridgeError("ref_check_failed",
                                  f"выгрузка: rc={r.returncode}, res={rt}")
            _sh.rmtree(temp_base, ignore_errors=True)  # база больше не нужна, дамп остаётся
            d = dump
        res = scan_dump(d, object_name, max_context)
        if not res.get("ok"):
            raise BridgeError("ref_check_failed", res.get("err", "?"))
        res["dump"] = str(d)
        return res

    def md_journal(self, session: int, path: str = "", limit: int = 50,
                   src: str = "", kind: int | None = None) -> dict:
        """Журнал MetaDataEvent (этап 18): кто и что менял в метаданных —
        «human» или «agent» (мост сам помечает свои операции по занятости).
        Читает journal/<pid>.jsonl (пишет мост 1.1+, UTF-16, append-JSONL:
        ts, pid, src, kind/kindName (AddChild/PropChanged/DeleteChild/Changed/
        Save/Close/AfterSave), request, obj {id,name,cls}, parentId, container).
        limit — последние N записей; src — фильтр «human»/«agent»; kind — фильтр
        по номеру вида события. path — явный путь к журналу (иначе из session_info)."""
        import io as _io
        import json as _json
        p = path
        if not p:
            si = self.session_info(session=session)
            p = (si.get("journal") or {}).get("file", "")
        if not p or not Path(p).is_file():
            return {"entries": [], "file": p,
                    "note": "журнала нет (мост <1.1 или изменений не было)"}
        entries = []
        with _io.open(p, encoding="utf-16", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = _json.loads(line)
                except _json.JSONDecodeError:
                    continue  # оборванная хвостовая строка — журнал диагностический
                if src and e.get("src") != src:
                    continue
                if kind is not None and e.get("kind") != kind:
                    continue
                entries.append(e)
        return {"file": p, "total": len(entries), "entries": entries[-limit:],
                "kinds": {k: sum(1 for e in entries if e.get("kindName") == k)
                          for k in sorted({e.get("kindName", "?") for e in entries})}}

    def journal_dashboard(self, session: int | None = None, path: str = "",
                          out_file: str = "", title: str = "") -> dict:
        """HTML-дашборд журнала метаданных (№7): timeline human/agent,
        клиентские фильтры (поиск, src, kindName) — открыть в браузере,
        «что делалось в конфигурации» без ZCode. path — путь к journal/<pid>.jsonl
        (иначе из session_info); out_file — куда писать HTML (умолчание
        <журнал>-dashboard.html); title — заголовок страницы. Самодостаточный
        файл (инлайн CSS/JS, без внешних зависимостей)."""
        import sys as _sys
        tools_dir = Path(__file__).resolve().parents[1] / "tools"
        if str(tools_dir) not in _sys.path:
            _sys.path.insert(0, str(tools_dir))
        from journal_dashboard import load_entries, build_dashboard  # noqa: E402
        p = path
        if not p:
            si = self.session_info(session=session)
            p = (si.get("journal") or {}).get("file", "")
        if not p or not Path(p).is_file():
            raise BridgeError("bad_args",
                              "журнал не найден: укажите path или живую сессию")
        out = out_file or (str(Path(p).with_suffix("")) + "-dashboard.html")
        entries = load_entries(p)
        html = build_dashboard(entries, title or "")
        with open(out, "w", encoding="utf-8-sig", newline="\r\n") as fh:
            fh.write(html)
        return {"file": p, "html": out, "total": len(entries)}

    def journal_configure(self, enabled: bool | None = None,
                          kinds: list[int] | None = None,
                          max_mb: float | None = None, keep: int | None = None) -> dict:
        """Настроить журнал регистрации (этап 18/19): пишет <root>/mcp_bridge.cfg.json,
        который МОСТ читает без перезапуска конфигуратора (перечитывает по mtime
        на каждом событии): enabled (вкл/выкл записи), kinds (фильтр видов 0..6:
        0=AddChild,1=PropChanged,2=DeleteChild,3=Changed,4=Save,5=Close,6=AfterSave;
        пусто = все), max_mb/keep (ротация journal/<pid>.jsonl). Те же значения
        дублируются в конфиг сервера (для md_journal). Без аргументов — вернуть
        текущее состояние (файл конфига моста)."""
        import json as _json
        cfg_file = Path(self.cfg.root) / "mcp_bridge.cfg.json"
        current: dict = {}
        if cfg_file.is_file():
            try:
                current = _json.loads(cfg_file.read_text(encoding="utf-8-sig"))
            except Exception:
                current = {}
        j = current.get("journal") or {}
        if enabled is not None:
            j["enabled"] = bool(enabled)
        if kinds is not None:
            if not isinstance(kinds, list) or not all(isinstance(k, int) and 0 <= k <= 6 for k in kinds):
                raise BridgeError("bad_args", "kinds: список целых 0..6 (пусто = все виды)")
            j["kinds"] = kinds
        if max_mb is not None:
            if max_mb <= 0:
                raise BridgeError("bad_args", "max_mb должно быть положительным")
            j["maxFileMB"] = float(max_mb)
        if keep is not None:
            if keep < 1:
                raise BridgeError("bad_args", "keep >= 1")
            j["keepFiles"] = int(keep)
        current["journal"] = j
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(_json.dumps(current, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        # дублировать в конфиг сервера (md_journal читает эти умолчания)
        self.cfg.journal_enabled = j.get("enabled", True)
        self.cfg.journal_kinds = j.get("kinds", [])
        self.cfg.journal_max_mb = j.get("maxFileMB", 20.0)
        self.cfg.journal_keep = j.get("keepFiles", 3)
        return {"cfg_file": str(cfg_file), "journal": j,
                "note": "мост применяет без перезапуска (перечитывает по mtime)"}

    def doctor(self, session: int | None = None) -> dict:
        """Самодиагностика установки: платформа, стартер, дом (маркер mcp_bridge.home),
        флаг .on, конфиг/own_bases, DynamicWrapperX, журнал, маркер загрузки моста;
        при session — плюс живая сессия (bridge/home/журнал). verdict: ok/warn/fail,
        каждой проверке — подсказка. Работает и БЕЗ конфигуратора (session не указан)."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from doctor import run_filesystem_checks  # noqa: E402
        # cfg.root указывает на <дом>\mcp; чек-лист ожидает ДОМ (родителя)
        res = run_filesystem_checks(Path(self.cfg.root).parent,
                                    journal_enabled=self.cfg.journal_enabled)
        live = []
        if session is not None:
            try:
                si = self.session_info(session=session)
            except Exception as e:
                live = [{"name": "живая сессия", "status": "warn",
                         "detail": f"session {session} недоступна: {e}",
                         "hint": "откройте конфигуратор; стартер должен быть запущен"}]
            else:
                live = [{"name": "живая сессия", "status": "ok",
                         "detail": f"pid {si.get('pid')}, мост {si.get('bridge')}"},
                        {"name": "дом сессии == cfg.root",
                         "status": "ok" if si.get("home") == str(self.cfg.root) else "warn",
                         "detail": str(si.get("home"))},
                        {"name": "журнал сессии", "status": "ok",
                         "detail": str((si.get("journal") or {}).get("file"))}]
        res["checks"] = res["checks"] + live
        res["verdict"] = ("fail" if any(c["status"] == "fail" for c in res["checks"])
                          else "warn" if any(c["status"] == "warn" for c in res["checks"])
                          else "ok")
        return res

    def eventlog(self, action: str, base: str, events: list | None = None,
                 use: bool = True, out_file: str = "",
                 start: str = "", end: str = "") -> dict:
        """Настройки журнала регистрации ИБ через внешнее соединение (V83.COMConnector,
        БЕЗ конфигуратора). Это НЕ метаданные: через rootObject недоступны. Действия:
        get — использование всех событий + период разделения хранения (8.3.20+);
        set — включить/выключить события (events: имена вида '_$Data$_.Update' или 'all');
        split — период разделения хранения: Год|Месяц|День|Неделя|Час|Квартал|НеИспользовать
        (events=[<период>], монопольный доступ); dump — выгрузить журнал в XML (out_file,
        фильтр start/end YYYY-MM-DD). base — путь файловой ИБ; требуется монопольный
        доступ (закройте конфигуратор этой базы). По умолчанию все события журнала
        ВКЛЮЧЕНЫ платформой."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from eventlog_cfg import (cmd_get, cmd_set, cmd_split, cmd_dump,
                                  KNOWN_EVENTS, EventLogError)  # noqa: E402
        try:
            if action == "get":
                return cmd_get(base)
            if action == "set":
                if not events:
                    raise BridgeError("bad_args",
                                      "events обязателен: имена событий или 'all'")
                return cmd_set(base, events, use)
            if action == "split":
                if not events or len(events) != 1:
                    raise BridgeError(
                        "bad_args",
                        "events=[<период>] — Год|Месяц|День|Неделя|Час|Квартал|НеИспользовать")
                return cmd_split(base, events[0])
            if action == "dump":
                if not out_file:
                    raise BridgeError("bad_args", "out_file обязателен (путь XML)")
                return cmd_dump(base, out_file, start or None, end or None)
            raise BridgeError("bad_args",
                              "action: get|set|split|dump; события — см. KNOWN_EVENTS "
                              "в tools/eventlog_cfg.py")
        except EventLogError as e:
            raise BridgeError("eventlog_failed", str(e))

    def md_search_code(self, query: str, cf_path: str, regex: bool = False,
                       limit: int = 50) -> dict:
        """Поиск по текстам модулей конфигурации в cf — БЕЗ платформы (IDEAS №15).
        Тексты модулей читаются из .0-сериализаций cf (страницы text), uuid записи
        = uuid объекта, имя из сериализаций. Поиск по СОХРАНЁННОЙ конфигурации:
        cf_path — файл от cfg_archive_cf (пишет последнее сохранённое состояние —
        несохранённые правки module_set в cf не попадают). query — подстрока
        (регистронезависимо) или regex при regex=true; попадания только внутри
        страниц text (модульный текст), вне их — отбрасываются. Возвращает
        совпадения по объектам: {object, hits: [{line, col, fragment}]}. Популярный
        сценарий: cfg_archive_cf → md_search_code(query, cf_path) — «где вызывается
        Процедура X» по всей конфигурации за секунды."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from md_search_code import search  # noqa: E402
        if not query:
            raise BridgeError("bad_args", "query обязателен (подстрока или regex)")
        if not cf_path:
            raise BridgeError("bad_args",
                              "cf_path обязателен: файл от cfg_archive_cf "
                              "(сначала сохраните конфигурацию — поиск по сохранённой)")
        try:
            return search(cf_path, query, bool(regex), int(limit))
        except (ValueError, FileNotFoundError) as e:
            raise BridgeError("bad_args", str(e))

    def module_structure(self, session: int, container: int, path: str,
                         prop: str = "МодульОбъекта",
                         calls_of: str = "") -> dict:
        """Структура модуля (IDEAS №16): процедуры/функции с line_start/line_end,
        директивами (&НаСервере…) и Экспорт — для ТОЧНЫХ module_patch (правка по
        номерам строк). Дополнительно calls_of=«Имя» — вызовы процедуры в этом же
        модуле (MVP иерархии вызовов). Модуль берётся живым module_get(with_text);
        текст НЕ сохраняется. Парсер — tools/module_structure.py (эвристика
        начала строки «Процедура|Функция» + «(»; ограничения в docstring)."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from module_structure import parse, find_call_sites  # noqa: E402
        if not path:
            raise BridgeError("bad_args", "path обязателен (например «Справочник.Эмитенты»)")
        mod = self._call(session, "module_get",
                         self._a(container, path=path, prop=prop, withText=True))
        text = mod.get("text") or ""
        blocks = parse(text)
        res: dict = {"module": {"path": path, "prop": prop,
                                "lines": len(text.splitlines()),
                                "readonly": mod.get("readonly")},
                     "blocks": blocks}
        if calls_of:
            res["calls"] = find_call_sites(text, calls_of)
        return res

    _CLS_PLURAL = {"Справочник": "Справочники", "Документ": "Документы",
                   "Обработка": "Обработки", "Отчет": "Отчеты",
                   "Перечисление": "Перечисления", "РегистрСведений": "РегистрыСведений",
                   "РегистрНакопления": "РегистрыНакопления"}

    def _copy_attr(self, session: int | None, container: int | None,
                   src_path: str, dst_path: str) -> None:
        """Копия реквизита: md_add_child + тип из source (types → md_set_type,
        internal → md_set_type_internal)."""
        so = self._call(session, "md_object", self._a(container, path=src_path))
        tval = None
        for p in so.get("props", []):
            if p.get("name") == "Тип":
                tval = p.get("value")
                break
        self.md_add_child(session=session, container=container,
                          owner_path=dst_path.rsplit(".Реквизит.", 1)[0],
                          kind="Реквизиты", name=src_path.rsplit(".", 1)[-1])
        if isinstance(tval, dict) and "internal" in tval:
            self.md_set_type_internal(session=session, container=container,
                                      path=dst_path, internal=tval["internal"])
        elif isinstance(tval, dict) and tval.get("types"):
            self.md_set_type(session=session, container=container, path=dst_path,
                             types=tval["types"], length=tval.get("length"),
                             digits=tval.get("digits"), fraction=tval.get("fraction"))

    def md_clone(self, session: int | None = None, container: int | None = None,
                 path: str = "", new_name: str = "",
                 copy_modules: bool = True) -> dict:
        """Клон объекта (IDEAS №18, MVP): новый объект того же вида + реквизиты
        (типы переносятся: примитивные md_set_type, ссылочные internal-строкой),
        табличные части с их реквизитами, синоним «… (копия)», модули при
        copy_modules. НЕ копируются формы/макеты/СКД/подчинённые отчеты —
        формы переносятся отдельно форм_get_xml/form_set_xml. path — верхнеуровневый
        объект («Справочник.X»); new_name — имя клона (префикс/суффикс на вас)."""
        parts = path.split(".")
        if len(parts) != 2 or not new_name:
            raise BridgeError("bad_args",
                              "path — верхнеуровневый объект (Класс.Имя), new_name обязателен")
        cls, name = parts
        plural = self._CLS_PLURAL.get(cls)
        if not plural:
            raise BridgeError("bad_args", "клонирование вида «%s» не поддержано (MVP)" % cls)
        src = self._call(session, "md_object", self._a(container, path=path))
        new_path = cls + "." + new_name
        self.md_add_child(session=session, container=container, owner_path="",
                          kind=plural, name=new_name,
                          synonym=((src.get("synonymRu") or name) + " (копия)"))
        copied = {"attributes": 0, "tabular_parts": 0, "modules": 0}
        attrs = self._call(session, "md_children",
                           self._a(container, path=path, kind="Реквизиты"))
        for a in attrs.get("items", []):
            self._copy_attr(session, container,
                            path + ".Реквизит." + a["name"],
                            new_path + ".Реквизит." + a["name"])
            copied["attributes"] += 1
        tchs = self._call(session, "md_children",
                          self._a(container, path=path, kind="ТабличныеЧасти"))
        tch_skipped = 0
        for t in tchs.get("items", []):
            self.md_add_child(session=session, container=container,
                              owner_path=new_path, kind="ТабличныеЧасти", name=t["name"])
            copied["tabular_parts"] += 1
            tattrs = self._call(session, "md_children",
                                self._a(container, path=path + ".ТабличнаяЧасть." + t["name"],
                                        kind="Реквизиты"))
            for a in tattrs.get("items", []):
                try:
                    self._copy_attr(session, container,
                                    path + ".ТабличнаяЧасть." + t["name"] + ".Реквизит." + a["name"],
                                    new_path + ".ТабличнаяЧасть." + t["name"] + ".Реквизит." + a["name"])
                    copied["attributes"] += 1
                except BridgeError:
                    # гвард 26.10: ПЕРВЫЙ реквизит пустой ТЧ клавиатурой не создаётся
                    # (узел скрыт); XML-контур — docs/tch-first-attr-xml.md, вручную —
                    # конфигуратор. Остальные реквизиты ТЧ добавляются sibling'ом
                    # после ручного первого.
                    tch_skipped += 1
                    break
        if tch_skipped:
            copied["tch_attrs_skipped"] = tch_skipped
            copied["note"] = ("реквизиты табличных частей пропущены: первый реквизит "
                              "пустой ТЧ не автоматизирован (docs/tch-first-attr-xml.md)")
        if copy_modules:
            for m in src.get("modules", []):
                src_text = self.module_get(session=session, container=container,
                                           path=path, prop=m["prop"], with_text=True)
                text = src_text.get("text") or ""
                if not text:
                    continue
                new_mod = self.module_get(session=session, container=container,
                                          path=new_path, prop=m["prop"])
                self.module_set(session=session, container=container, path=new_path,
                                prop=m["prop"], text=text,
                                expected_hash=new_mod.get("hash"))
                copied["modules"] += 1
        return {"cloned": {"name": new_name, "cls": cls, "path": new_path},
                "source": path, "copied": copied}

    def yaxunit_run(self, base: str, epf: str, report: str,
                    tests_path: str = "", timeout: int = 3600) -> dict:
        """Прогон YAXUnit из командной строки (IDEAS №22, без конфигуратора):
        1cv8 ENTERPRISE /Execute YAxUnit.epf с конфигом (jUnit-отчёт, автозакрытие
        клиента). Возвращает сводку (тесты/провалы/ошибки + список упавших) или
        yaxunit_error, если отчёт не создан (висящий диалог — сторож убивает
        клиент; см. навык yaxunit-autotests: /CheckModules до прогона). Один прогон
        на станцию — инструмент не трогает чужие клиенты."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from yaxunit_run import run, YaxunitError  # noqa: E402
        try:
            return run(base, epf, report, tests_path or None, int(timeout))
        except YaxunitError as e:
            raise BridgeError("yaxunit_failed", str(e))

    def call_hierarchy(self, session: int | None = None,
                       container: int | None = None,
                       name: str = "", cf_path: str = "",
                       source_path: str = "") -> dict:
        """Иерархия вызовов процедуры/функции по всей конфигурации (№16 завершение).
        name — имя процедуры/функции; cf_path — cf от cfg_archive_cf (поиск вызовов
        через md_search_code, без платформы); source_path — модуль с определением
        («Справочник.X», prop по умолчанию МодульОбъекта) — отдаёт точные
        line_start/line_end из module_structure. Возвращает определение и вызовы
        по объектам (строки + фрагменты); вызовы через точку и строки-объявления
        исключены. Литералы строк дают возможные ложные срабатывания (note)."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from call_hierarchy import build, HierarchyError  # noqa: E402
        from md_search_code import search  # noqa: E402
        if not name or not cf_path:
            raise BridgeError("bad_args", "name и cf_path обязательны")
        if not re.fullmatch(r"[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*", name):
            raise BridgeError("bad_args", "name — идентификатор 1С без скобок")
        src_text = None
        if source_path:
            mod = self.module_get(session=session, container=container,
                                  path=source_path, prop="МодульОбъекта",
                                  with_text=True)
            src_text = mod.get("text") or ""
        search_res = search(cf_path, name, use_regex=False, limit=100)
        try:
            return build(src_text, source_path or None, search_res, name)
        except HierarchyError as e:
            raise BridgeError("bad_args", str(e))

    def search_live(self, session: int | None = None,
                    container: int | None = None, needle: str = "",
                    regex: bool = False, limit_objects: int = 500) -> dict:
        """Живой поиск по модулям конфигуратора БЕЗ config_save (№15 дополнение).
        Обход верхнего уровня (Справочники/Документы/Обработки/Отчеты/Общие модули),
        модули каждого объекта, поиск подстроки/regex. Медленнее md_search_code
        (каждый модуль — запрос к мосту), но не требует сохранения: правки видны
        сразу. Формы — отдельным контуром. Ограничение MVP: литералы строк в
        модулях дают superset попаданий."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from search_live import search_live as _search  # noqa: E402
        if not needle:
            raise BridgeError("bad_args", "needle обязателен")
        return _search(self, session, container, needle, bool(regex),
                       int(limit_objects))

    def blog_publish(self, md_file: str = "", title: str = "", slug: str = "",
                     status: str = "publish", action: str = "publish",
                     post_id: int = 0) -> dict:
        """Публиковать markdown-статью в блог chagbig.ru (WordPress REST).
        md_file — путь к md-файлу (первый заголовок # становится заголовком
        поста, если title не задан); конвертация md→Gutenberg-совместимый HTML
        (h2/h3/списки/код/жирный/ссылки). Реквизиты — Application Password в
        Y:/infra/deploy/projects/wp-chagbig.env. Идемпотентно по slug.
        Возвращает id/link поста. action="list" — последние посты; action="edit" +
        post_id — обновление существующего (title/content из md_file/status).
        Проверено живьём 13.11.2026 (пост id 72)."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from blog_publish import (publish, edit_post, list_posts,  # noqa: E402
                                  md_to_html, BlogError)
        if action == "list":
            return list_posts()
        if action == "edit":
            if not post_id:
                raise BridgeError("bad_args", "post_id обязателен для edit")
            content = ''
            if md_file:
                if not os.path.isfile(md_file):
                    raise BridgeError("bad_args", "md-файл не найден: " + md_file)
                content = md_to_html(io.open(md_file, encoding='utf-8-sig').read())
            try:
                return edit_post(post_id, title or None,
                                 content or None, status or None)
            except BlogError as e:
                raise BridgeError("blog_failed", str(e))
        if not md_file:
            raise BridgeError("bad_args", "md_file обязателен")
        if status not in ("publish", "draft"):
            raise BridgeError("bad_args", "status: publish|draft")
        try:
            return publish(md_file, title, slug, status)
        except BlogError as e:
            raise BridgeError("blog_failed", str(e))

    # ---------- №29 (v1.12): карта меню и переходов ----------

    MENU_HEADER_CLIENT = (190, 49)  # «строка меню» в клиентских координатах окна

    def menu_invoke(self, session: int | None = None, menu: str = "",
                    item: str = "", shot: bool = True) -> dict:
        """Активировать пункт меню конфигуратора ПО КАРТЕ (№29, v1.12).
        Карта — <root>/menu_map.json (снимается агентом: клик заголовка
        mouse_click'ом + скриншот + чтение имён; формат — в _meta карты).
        Цепочка: клик заголовка меню (mouse_click из тика моста, DWX) →
        ВНИЗ×(idx+1) (стрелки пропускают разделители, серые подсвечиваются) →
        ENTER. Постусловие: diff view_list до/после (новые окна = «что
        сработало»); пункт disabled — note «enter_ignored» (подсветка есть,
        активации нет — проверено пробами 14.11). Условие владельца: многие
        пункты активны только при открытом окне конфигурации (см. ext_open_v2).
        shot=True — приложить PNG экрана после активации."""
        # гварды карты ДО _pid: дёшево и без требования живой сессии
        map_path = Path(self.cfg.root) / "menu_map.json"
        if not map_path.is_file():
            raise BridgeError(
                "bad_args",
                "карты меню нет: %s — снимите её агентом (window_shot по "
                "каждому заголовку + имена/активность в JSON)" % map_path)
        card = json.loads(map_path.read_text(encoding="utf-8-sig"))
        if menu not in card or menu.startswith("_") or menu == "headers_px":
            known = ", ".join(k for k in card if not k.startswith("_")
                              and k != "headers_px")
            raise BridgeError("bad_args", "нет меню %r в карте (есть: %s)"
                              % (menu, known))
        # навигационная последовательность: [(VK_DOWN, n)] и один RIGHT для подменю
        # верхний уровень: idx — номер не-sep строки (стрелка пропускает разделители,
        # N-е нажатие = N-й пункт); подменю: родитель → RIGHT → ВНИЗ×(child_idx+1);
        # MRQ-строки («Файл», недавние обработки) — обычные позиции, поиск по
        # ПОДСТРОКЕ имени файла (пути приватны)
        def _find(item_name: str):
            for i, it in enumerate(card[menu]):
                if it.get("mrq"):
                    continue
                if it.get("name", "").lower() == item_name.lower():
                    return [(0x28, i + 1)], it
            if menu == "Файл":  # MRQ/недавние: подстрока по имени строки
                # (в карте MRQ-строки без спец-флага — подстрока по всем)
                for i, it in enumerate(card[menu]):
                    if item_name.lower() in it.get("name", "").lower():
                        return [(0x28, i + 1)], it
            # подменю: родитель с items → RIGHT → ВНИЗ×(child+1)
            for i, it in enumerate(card[menu]):
                for j, ch in enumerate(it.get("items") or []):
                    if ch.get("name", "").lower() == item_name.lower():
                        return ([(0x28, i + 1), (0x27, 1), (0x28, j + 1)], ch)
            return None, None
        nav, it = _find(item)
        if nav is None:
            known = "; ".join(it.get("name", "") for it in card[menu]
                              if not it.get("mrq"))[:300]
            raise BridgeError("bad_args", "нет пункта %r в меню %r (есть: %s)"
                              % (item, menu, known))
        it = dict(it)
        it["_nav"] = nav
        pid = self._pid(session)
        from .winutils import activate_window, main_window
        hwnd = main_window(pid)
        activate_window(pid)
        time.sleep(0.6)
        views_before = {v.get("title") or "" for v in
                        self.bridge.call(pid, "view_list", {}, timeout=30)}
        # заголовок меню — по карте headers_px (клиентские x при maximized):
        # у КАЖДОГО меню свой x («Файл» ≠ «Конфигурация» — иначе открывается
        # не то меню, ловушка MRQ-прогона v1.13)
        # headers_px — латинские ключи, меню — русское имя
        _MENU_KEYS = {"Файл": "file", "Правка": "edit", "Конфигурация": "config",
                      "Снегопат": "snegopat", "Отладка": "debug",
                      "Администрирование": "admin", "Сервис": "service",
                      "Окна": "windows", "Справка": "help"}
        header_x = int(card.get("headers_px", {}).get(
            _MENU_KEYS.get(menu, menu.lower()), 190))
        menu_x, menu_y = self.MENU_HEADER_CLIENT
        try:
            rect = self.bridge.call(pid, "control_rect", {"hwnd": int(hwnd)},
                                    timeout=30)
            menu_x = int(rect["left"]) + 7 + header_x
            menu_y = int(rect["top"]) + self.MENU_HEADER_CLIENT[1]
        except Exception:
            menu_x = header_x - 7  # maximized-дефолт
        # Esc-сброс перед открытием: хвост открытых меню/подменю сдвигает
        # стартовую подсветку (проверено MRQ-пробой v1.13). Esc со станции
        # в foreground главного окна доходит (пробы 15.11)
        import ctypes as _ct
        u32_ = _ct.windll.user32 if sys.platform == "win32" else None
        if u32_ is not None:
            activate_window(pid)
            time.sleep(0.4)
            u32_.keybd_event(0x1B, 0, 0, 0)
            time.sleep(0.06)
            u32_.keybd_event(0x1B, 0, 2, 0)
            time.sleep(0.5)
        self.bridge.call(pid, "mouse_click", {"x": menu_x, "y": menu_y},
                         timeout=30)
        time.sleep(0.7)
        import ctypes
        u32 = ctypes.windll.user32 if sys.platform == "win32" else None
        if u32 is None:
            raise BridgeError("bad_args", "menu_invoke: клавиатура — только Windows")
        def _key(vk, d=0.12):
            u32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.05)
            u32.keybd_event(vk, 0, 2, 0)
            time.sleep(d)
        nav = it.pop("_nav", [(0x28, 1)])  # _nav ставит _find всегда
        for vk, n in nav:
            for _ in range(n):
                _key(vk)
        _key(0x0D, 0.25)
        time.sleep(2.0)
        views_after = {v.get("title") or "" for v in
                       self.bridge.call(pid, "view_list", {}, timeout=30)}
        new_views = sorted(views_after - views_before)
        result = {"menu": menu, "item": it["name"],
                  "item_enabled": bool(it.get("enabled")), "ok": True,
                  "new_views": new_views}
        if not new_views and not it.get("submenu"):
            result["note"] = ("новых окон нет — пункт disabled или действие "
                              "без окна; проверьте скриншот")
        if shot and sys.platform == "win32":
            time.sleep(0.6)
            try:
                from window_shot import shot_by_title
                shot_path = str(Path(self.cfg.root) /
                                f"menu-{pid}-{int(time.time())}.png")
                shot_by_title(pid, "Конфигуратор", shot_path)
                result["shot"] = shot_path
            except Exception:
                pass
        return result

    def state_snapshot(self, session: int | None = None) -> dict:
        """Fingerprint состояния сеанса конфигуратора (№29, v1.12): views
        (открытые окна с заголовками), containers (конфигурации/расширения с
        isModified), modalMode (мост заблокирован модалкой), running (долгая
        операция). Основа графа переходов «состояние_до → действие →
        состояние_после»: агент логирует тройку при каждом действии — карта
        достраивается из эксплуатации без отдельного разбора."""
        pid = self._pid(session)
        si = self.session_info(session=pid)
        views = self.bridge.call(pid, "view_list", {}, timeout=30)
        titled = [v.get("title") for v in views if (v.get("title") or "").strip()]
        return {"pid": pid, "modal": si.get("modalMode"),
                "running": si.get("running"),
                "containers": si.get("containers"),
                "views": titled, "view_count": len(titled)}

    # ---------- №28 (v1.12): мышиный контур из тика моста + ext_open_v2 ----------

    def mouse_click(self, session: int | None = None, x: int = 0, y: int = 0,
                    shot: bool = False) -> dict:
        """Клик мышью из тика моста по ЭКРАННЫМ координатам (№28, v1.12).
        Клики из UI-потока конфигуратора НЕ блокируются extra-info-гвардом 1С
        (клики со станции — keybd/mouse_event через ctypes — игнорируются
        кастомным UI, проверено 14.11.2026). Координаты агент снимает
        window_shot'ом. shot=True — приложить PNG полного экрана после клика
        (проверка «что сработало»). Приемлемо для меню и списков конфигуратора;
        клик по пункту ВЫПАВШЕГО меню не подхватывается popup-циклом — для меню
        использовать клавиатуру (стрелки+Enter в открытом меню работают)."""
        pid = self._pid(session)
        sys_path_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_path_tools) not in sys.path:
            sys.path.insert(0, str(sys_path_tools))
        try:
            from .winutils import activate_window
            activate_window(pid)
            time.sleep(0.6)
            res = self.bridge.call(pid, "mouse_click", {"x": int(x), "y": int(y)},
                                   timeout=30)
        except BridgeError:
            raise
        except Exception as e:
            raise BridgeError("mouse_click_failed", str(e))
        if shot:
            time.sleep(1.0)
            try:
                from window_shot import shot_by_title
                ws = self.bridge.call(pid, "view_list", {}, timeout=30)
                res["shot"] = shot_by_title(
                    pid, "Конфигуратор",
                    str(Path(self.cfg.root) / f"click-{pid}-{int(time.time())}.png"))
            except Exception:
                pass
        return res

    def type_text(self, session: int | None = None, text: str = "",
                  delay_ms: int = 30) -> dict:
        """Посимвольный UNICODE-ввод в окно фокуса (хвост №28, v1.13):
        SendInput KEYEVENTF_UNICODE — раскладка не важна (русские имена
        напрямую). Для выделения строки в списках конфигуратора (менеджер
        расширений ищет строку по вводимым символам): активировать окно,
        поставить фокус в список (focus_probe для проверки), затем
        type_text(имя). Управляющие символы пропускаются. Windows-only.
        Внимание: ввод идёт в foreground — при занятой станции уйдёт в чужое
        окно (сервер активирует окно сессии перед вводом)."""
        pid = self._pid(session)
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in sys.path:
            sys.path.insert(0, str(sys_tools))
        if not text:
            raise BridgeError("bad_args", "type_text: text обязателен")
        from .winutils import activate_window
        activate_window(pid)
        time.sleep(0.6)
        try:
            from type_text import send_unicode
            res = send_unicode(text, delay_ms=delay_ms)
        except RuntimeError as e:
            raise BridgeError("bad_args", str(e))
        except OSError as e:
            raise BridgeError("type_text_failed", str(e))
        res["pid"] = pid
        return res

    def ext_open_v2(self, session: int | None = None, name: str = "",
                    shot: bool = True) -> dict:
        """Открыть менеджер «Расширения конфигурации» АВТОМАТИЧЕСКИ (№28, v1.12;
        снимает ручной клик из этапа 13/v1.5). Условие (подтверждено владельцем
        и пробами): окно конфигурации должно быть открыто — иначе пункт меню
        «Расширения конфигурации» DISABLED. Цепочка: окно конфигурации
        (клавиатура END+Enter в меню — если дерево ещё не открыто) → меню
        «Конфигурация» (клик из тика моста) → ВНИЗ×6 → ENTER (клавиатура в
        popup работает). Постусловие: view_list содержит view «Расширения
        конфигурации». name — НЕ выделяет строку (выделение по имени требует
        раскладки; агент кликает mouse_click по координатам из скриншота).
        shot=True — приложить PNG экрана после открытия."""
        pid = self._pid(session)
        sys_path_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_path_tools) not in sys.path:
            sys.path.insert(0, str(sys_path_tools))
        from .winutils import activate_window
        u32 = None
        if sys.platform == "win32":
            import ctypes
            u32 = ctypes.windll.user32
        def _key(vk, d=0.12):
            if u32 is None:
                raise BridgeError("bad_args", "ext_open_v2: клавиатура — только Windows")
            u32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.05)
            u32.keybd_event(vk, 0, 2, 0)
            time.sleep(d)
        def _views():
            return self.bridge.call(pid, "view_list", {}, timeout=30)
        def _has_manager(views):
            return any((v.get("title") or "").strip() == "Расширения конфигурации"
                       for v in views)
        steps: list[dict] = []
        try:
            activate_window(pid)
            time.sleep(0.6)
            views = _views()
            if _has_manager(views):
                return {"ok": True, "already_open": True, "steps": steps}
            # окно конфигурации: ЖИВОЕ дерево несёт модификатор в заголовке —
            # «Конфигурация <п>» (не изменялась) или «Конфигурация <!>»
            # (изменялась); панель-заглушка в «Панели окон» (0/20/*) зовётся
            # просто «Конфигурация» — её нельзя считать открытой (проба
            # 14948, v1.12: без модификатора пункт меню оставался DISABLED)
            tree_open = any((v.get("title") or "").strip()
                            in ("Конфигурация <п>", "Конфигурация <!>")
                            for v in views)
            if not tree_open:
                self.bridge.call(pid, "mouse_click", {"x": 183, "y": 42},
                                 timeout=30)
                steps.append({"step": "menu", "ok": True})
                time.sleep(0.7)
                _key(0x23)   # END -> «Окно конфигурации» (последний пункт)
                _key(0x0D)   # ENTER
                steps.append({"step": "open_config_window", "ok": True})
                time.sleep(2.0)
            # координаты пункта меню «Конфигурация»: клиентские (190, 49) от
            # главного окна — через control_rect моста (окно maximized или нет)
            menu_x, menu_y = 183, 42
            try:
                from .winutils import main_window
                hwnd = main_window(pid)
                if hwnd:
                    rect = self.bridge.call(pid, "control_rect", {"hwnd": int(hwnd)},
                                            timeout=30)
                    menu_x = int(rect["left"]) + 190
                    menu_y = int(rect["top"]) + 49
            except Exception:
                pass  # maximized-дефолт (183, 42)
            activate_window(pid)
            time.sleep(0.5)
            self.bridge.call(pid, "mouse_click", {"x": menu_x, "y": menu_y},
                             timeout=30)
            steps.append({"step": "menu", "ok": True})
            time.sleep(0.7)
            for _ in range(6):
                _key(0x28)  # DOWN (пропускает разделители; серые подсвечиваются)
            _key(0x0D, 0.25)  # ENTER на «Расширения конфигурации»
            steps.append({"step": "extensions_item", "ok": True})
            time.sleep(2.2)
            views = _views()
            opened = _has_manager(views)
            steps.append({"step": "postcondition", "ok": opened,
                          "detail": "view «Расширения конфигурации» в view_list"})
            result = {"ok": opened, "already_open": False, "steps": steps,
                      "name": name or None}
            if shot and u32 is not None:
                time.sleep(0.6)
                try:
                    from window_shot import shot_by_title
                    shot_path = str(Path(self.cfg.root) /
                                    f"extmgr-{pid}-{int(time.time())}.png")
                    shot_by_title(pid, "Конфигуратор", shot_path)
                    result["shot"] = shot_path
                except Exception:
                    pass
            return result
        except BridgeError:
            raise
        except Exception as e:
            raise BridgeError("ext_open_v2_failed", str(e))

    def window_shot(self, session: int | None = None,
                    title_contains: str = "Конфигуратор",
                    out_file: str = "") -> dict:
        """Скриншот окна конфигуратора в PNG (№24, сторона станции — PrintWindow
        через ctypes/Pillow, мост не участвует). list=True — список видимых окон
        процесса (выбор цели). Иначе — снимок окна с title_contains в имени
        (по умолчанию — главное окно «Конфигуратор»; «Модуль» — открытый редактор).
        out_file — путь PNG (создаётся каталог). PrintWindow захватывает даже
        перекрытое окно (PW_RENDERFULLCONTENT)."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from window_shot import list_windows, shot_by_title  # noqa: E402
        pid = self._pid(session)
        if not out_file:
            out_file = str(Path(self.cfg.root) / f"window-{pid}-{int(time.time())}.png")
        if title_contains in ("list", "список"):
            return {"windows": list_windows(pid)}
        try:
            res = shot_by_title(pid, title_contains, out_file)
        except FileNotFoundError as e:
            raise BridgeError("bad_args", str(e))
        except OSError as e:
            raise BridgeError("window_shot_failed", str(e))
        return res

    def enterprise_smoke(self, base: str, wait_s: float = 30, shot_file: str = "",
                         close: bool = True, user: str = "", password: str = "",
                         extra_args: str = "") -> dict:
        """Enterprise-смоук (№6): запустить клиентский сеанс 1cv8c на файловой
        базе, дождаться главного окна (стабильный заголовок), снять PNG
        (PrintWindow, фокус не нужен), корректно закрыть (WM_CLOSE → taskkill
        → force, close_stage в ответе показывает, чем закрылось). Ловушка:
        при несохранённых изменениях клиент может остаться на диалоге —
        тогда сработает каскад taskkill (close_stage != wm_close). Требует
        Windows и установленную платформу. Проверка приёмки форм: форма
        открылась — окно видно, скриншот приложен."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from enterprise_smoke import launch_enterprise  # noqa: E402
        try:
            return launch_enterprise(base, wait_s=wait_s, shot_file=shot_file,
                                     close=close,
                                     user=user or None, password=password or None,
                                     extra_args=extra_args)
        except FileNotFoundError as e:
            raise BridgeError("bad_args", str(e))
        except OSError as e:
            raise BridgeError("enterprise_smoke_failed", str(e))

    def spec_check(self, claims: list, session: int | None = None,
                   container: int | None = None, base: str = "",
                   cf: str = "") -> dict:
        """Сверка утверждений ТЗ/спеки с метаданными конфигурации (№130, v1.9.0).
        Разделение ролей: агент разбирает текст ТЗ на структурированные
        утверждения (claims), инструмент проверяет их против живой конфигурации.
        claim = {"text": str, "objects": ["Справочник.X", ...],
                 "queries": ["ВЫБРАТЬ …"] (опц.)}. По каждому объекту:
        exists/missing/unknown_kind (md_object/md_find), similar («похожий
        механизм уже есть», md_find, до 5), impact (кто упоминает в коде —
        md_search_code по cf, cf снимается агентом через cfg_archive_cf и
        передаётся параметром cf); queries — query_validate (base обязателен,
        иначе пропуск). Вердикт per-claim: ok | missing_objects | warnings.
        Без cf/base соответствующие проверки пропускаются с note."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from spec_check import verify_claims  # noqa: E402
        if not isinstance(claims, list) or not claims:
            raise BridgeError("bad_args",
                              "claims: непустой список "
                              '{"text", "objects", "queries"}')
        try:
            return verify_claims(self, session, claims, container=container,
                                 base=base, cf=cf)
        except NoSessionError as e:
            raise BridgeError("bad_args", str(e))
        except BridgeError:
            raise
        except (ValueError, KeyError) as e:
            raise BridgeError("bad_args", str(e))

    def bridge_configure(self, default_timeout_s: float | None = None,
                         long_timeout_s: float | None = None,
                         heartbeat_stale_s: float | None = None,
                         op_timeouts: dict | None = None,
                         long_ops: list | None = None,
                         cfg_cmd_whitelist: list | None = None) -> dict:
        """Конфигурация моста/сервера без правки файлов руками (№10).
        Серверные поля (default_timeout_s/long_timeout_s/heartbeat_stale_s/
        op_timeouts/long_ops) применяются К ТЕКУЩЕМУ ПРОЦЕССУ сразу
        (resolve_timeout в bridge.call) и сохраняются в файл конфига
        (MCP_SNEGOPAT_CONFIG), если тот существует; cfg_cmd_whitelist — в
        mcp_bridge.cfg.json, мост применяет БЕЗ перезапуска конфигуратора
        (перечитка по mtime при событиях метаданных; пустой массив/отсутствие
        = встроенный белый список разведки). op_timeouts — {"op": секунд}
        точечно (перекрывает умолчания); long_ops — дополнительные «долгие»
        ops к встроенному LONG_OPS."""
        import json as _json
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from bridge_configure import configure  # noqa: E402
        applied: dict = {}
        # 1. мостовой белый список — через configure (валидация + journal-секция цела)
        if cfg_cmd_whitelist is not None:
            try:
                res_b = configure(None,
                                  str(Path(self.cfg.root) / "mcp_bridge.cfg.json"),
                                  None, None, None, None, None,
                                  cfg_cmd_whitelist)
            except ValueError as e:
                raise BridgeError("bad_args", str(e))
            applied["cfg_cmd_whitelist"] = list(cfg_cmd_whitelist)
            applied["bridge_cfg_file"] = res_b.get("bridge_cfg")
        # 2. серверные поля — валидация и runtime-применение
        for name, val in (("default_timeout_s", default_timeout_s),
                          ("long_timeout_s", long_timeout_s),
                          ("heartbeat_stale_s", heartbeat_stale_s)):
            if val is not None:
                if not isinstance(val, (int, float)) or val <= 0:
                    raise BridgeError("bad_args", f"{name}: положительное число")
                setattr(self.cfg, name, float(val))
                applied[name] = float(val)
        if op_timeouts is not None:
            if not isinstance(op_timeouts, dict):
                raise BridgeError("bad_args", "op_timeouts: {\"op\": секунд}")
            clean: dict = {}
            for k, v in op_timeouts.items():
                if not isinstance(v, (int, float)) or v <= 0:
                    raise BridgeError("bad_args", f"op_timeouts[{k!r}]: положительное число")
                clean[str(k)] = float(v)
            self.cfg.op_timeouts = clean
            applied["op_timeouts"] = clean
        if long_ops is not None:
            if not isinstance(long_ops, list) or not all(isinstance(x, str) and x for x in long_ops):
                raise BridgeError("bad_args", "long_ops: список имён ops")
            self.cfg.long_ops = list(long_ops)
            applied["long_ops"] = list(long_ops)
        # 3. персистенция серверных полей (если файл конфига известен)
        persist_note = "файл конфига не задан (MCP_SNEGOPAT_CONFIG) — персистенция вручную"
        env_path = os.environ.get("MCP_SNEGOPAT_CONFIG")
        if env_path and Path(env_path).is_file():
            server_keys = {k: v for k, v in applied.items()
                           if k in ("default_timeout_s", "long_timeout_s",
                                    "heartbeat_stale_s", "op_timeouts", "long_ops")}
            if server_keys:
                try:
                    current = _json.loads(Path(env_path).read_text(encoding="utf-8-sig"))
                except Exception:
                    current = {}
                current.update(server_keys)
                Path(env_path).write_text(
                    _json.dumps(current, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                persist_note = "сохранено в " + env_path
        applied["note"] = ("серверные поля применены немедленно; " + persist_note
                           if applied else "ничего не изменено")
        return applied

    def query_validate(self, query: str, base: str, mode: str = "check",
                       limit: int = 5) -> dict:
        """Валидация запроса 1С через внешнее соединение (IDEAS №21, без конфигуратора).
        Язык запросов read-only — исполнение безопасно по данным. mode=check (по
        умолчанию): запрос оборачивается в «ВЫБРАТЬ ПЕРВЫЕ 1 * ИЗ (...)» — синтаксис
        и метаданные дёшево (не работает для запросов с ИТОГИ); mode=execute —
        как есть, вернутся колонки и до limit строк (тяжёлый запрос выполнится
        целиком). base — путь файловой ИБ (не занятая конфигуратором). Ошибка
        запроса = {ok:false, error: текст 1С} — это ВЕРДИКТ валидации."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from query_validate import validate, QueryError  # noqa: E402
        if not query or not query.strip():
            raise BridgeError("bad_args", "query обязателен")
        try:
            return validate(base, query, mode, int(limit))
        except QueryError as e:
            raise BridgeError("bad_args", str(e))

    def infobase(self, action: str, path: str, confirm: bool = False) -> dict:
        """Создание/удаление файловых инфобаз (IDEAS №23, без конфигуратора).
        action=create: пакетный CREATEINFOBASE (идемпотентно — существующая база
        вернёт already_exists). action=delete: ДВУХФАЗНО (№19) — confirm=False даёт
        план (существует ли база, занятость .cfl, гвард own_bases — MCP-стенды
        защищены), confirm=True удаляет каталог ИБ целиком. Гварды: в каталоге
        обязан быть 1Cv8.1CD."""
        import sys as _sys
        sys_tools = Path(__file__).resolve().parents[1] / "tools"
        if str(sys_tools) not in _sys.path:
            _sys.path.insert(0, str(sys_tools))
        from ib_mgmt import create, plan_delete, delete, IbError  # noqa: E402
        if not path:
            raise BridgeError("bad_args", "path обязателен")
        try:
            if action == "create":
                return create(path)
            if action == "delete":
                own = list(self.cfg.own_bases or [])
                return delete(path, own) if confirm else plan_delete(path, own)
            raise BridgeError("bad_args", "action: create|delete")
        except IbError as e:
            raise BridgeError("ib_failed", str(e))

    def ext_save(self, session: int, name: str) -> dict:
        """Сохранить расширение (штатное «Сохранить документ» при активном дереве
        расширения — Frame.FileSave). Проверено живьём (28.10.2026): isModified → False,
        правки попадают в cfe. Дерево расширения должно быть открыто (ext_list);
        перед сохранением правки агента — снимите cfe-точку отката (ext_dump_cfe)."""
        exts = self.ext_list(session=session).get("extensions", [])
        mine = [e for e in exts if name == (e.get("root") or "")
                or name in (e.get("identifier") or "").replace("Расширение конфигурации ", "").strip('"')]
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
        существование проверяется ДО отправки Ins.
        (ред. ревью v1.0.0: флаг через _wizard_lock — не затирает чужие диалоги;
        дедлайн адаптивный — копия базы 2 ГБ занимает минуты.)"""
        import subprocess as _sp
        import os as _os
        import time as _t
        base = str(_session_base_path(self.bridge, session))
        if _ext_exists_packed(base, name):
            raise BridgeError("bad_args", f"расширение «{name}» уже есть в базе")
        self._set_wizard_dialog("Новое расширение конфигурации", [
            {"set": {"EditName": name, "EditPrefix": prefix, "EditSynonym": synonym,
                     "EditUsePurpose": purpose},
             "report": ["EditName", "EditPrefix", "EditSynonym", "EditUsePurpose"]},
            {"click": "OkButton"}])
        try:
            self._call(session, "view_activate", {"title": "Расширения конфигурации"})
            self._call(session, "activate_self", {})
            self._call(session, "key_send", {"key": 0x09})  # Tab — фокус в грид
            self._call(session, "key_send", {"key": 0x2D})  # Ins — открывает диалог
            # дедлайн адаптивный: копия базы в _ext_exists_packed на 2 ГБ — минуты
            size = _os.path.getsize(_os.path.join(base, "1Cv8.1CD"))                 if _os.path.isfile(_os.path.join(base, "1Cv8.1CD")) else 0
            deadline = _t.time() + 60 + (size // (100 * 1024 * 1024)) * 60
            ok = False
            while _t.time() < deadline and not ok:
                _t.sleep(5)
                try:
                    ok = _ext_exists_packed(base, name)
                except Exception:
                    ok = False
            if not ok:
                raise BridgeError("ext_create_failed",
                                  "расширение не создано — проверьте wizard.log/модалки "
                                  "(повторный Ins при существующем имени даёт зомби-модалку)")
            return {"created": name, "verified": "packed-check"}
        finally:
            self._clear_wizard_dialog("Новое расширение конфигурации")

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

    # мостовые op-ии, ИЗМЕНЯЮЩИЕ метаданные/БД — гейт read_only (№20, пресет
    # «Analysis Only»); композиции (md_clone, md_rename) покрыты автоматически —
    # идут через эти же op
    _WRITE_BRIDGE_OPS = frozenset({
        "module_set", "module_patch",
        "prop_set", "prop_set_internal", "prop_set_type", "prop_set_type_internal",
        "md_add_child", "md_delete",
        "config_save", "update_db_cfg",
        "cfgstore_commit", "editor_commit",
        "form_write", "cfg_load_cf",
    })

    def _call(self, session: int | None, op: str, args: dict,
              pid: int | None = None, timeout: float | None = None) -> dict:
        if pid is None:
            pid = self._pid(session)
        if getattr(self.cfg, "read_only", False) and op in self._WRITE_BRIDGE_OPS:
            raise BridgeError(
                "forbidden",
                "сервер в режиме read_only (конфиг read_only=true / пресет "
                "«Analysis Only»): операция записи %s запрещена" % op)
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
        try:
            winutils.activate_window(pid, wait_s=1.0)  # best-effort снаружи
        except (AttributeError, OSError):
            pass  # Linux/CI: нет ctypes.windll — фокус не нужен (fake-мост)
        # дерево расширения имеет заголовок = отображаемое имя расширения: для
        # неосновного контейнера принимаем ЛЮБОЙ заголовок, кроме дерева основной
        # конфигурации (этап 13, М-6 — уход клавиш в основное дерево = порча)
        ext_mode = container is not None
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

# №20 (v1.6.0): пресеты — cfg.presets сокращает видимый набор инструментов
# (экономия контекста агента, сценарии «только ревью»). Незнакомое имя = отказ
# при сборке. Пусто = все инструменты. read_only — независимый гейт в _call.
PRESETS: dict[str, set[str]] = {
    # анализ/ревью: только чтение и диагностика
    "analysis": {
        "list_sessions", "session_info", "doctor", "bridge_wait", "bridge_stats",
        "focus_probe", "md_tree", "md_object", "md_children", "md_find",
        "md_counts", "md_walk", "module_get", "type_lookup", "md_search_code",
        "module_structure", "call_hierarchy", "search_live", "md_check_refs",
        "md_journal", "journal_dashboard", "eventlog", "query_validate",
        "window_shot",
    },
    # структурные правки метаданных: analysis + запись метаданных/модулей + конфигурация
    "meta": {
        "list_sessions", "session_info", "doctor", "bridge_wait", "bridge_stats",
        "focus_probe", "md_tree", "md_object", "md_children", "md_find",
        "md_counts", "md_walk", "module_get", "module_set", "module_patch",
        "type_lookup", "md_search_code", "module_structure", "call_hierarchy",
        "search_live", "md_check_refs", "md_journal",
        "md_set_property", "md_set_property_internal", "md_set_synonym",
        "md_set_type", "md_set_type_internal", "md_add_child", "md_delete",
        "md_rename", "md_clone",
        "config_open", "cfg_archive_cf", "config_save", "syntax_check",
        "update_db_cfg", "tree_activate", "tree_activate_group", "dialog_probe",
    },
    # форм-контур: чтение + form_get_xml/form_set_xml + конфигурация
    "forms": {
        "list_sessions", "session_info", "doctor", "bridge_wait", "bridge_stats",
        "focus_probe", "md_tree", "md_object", "md_children", "md_find",
        "md_counts", "md_walk", "module_get", "type_lookup", "md_search_code",
        "module_structure", "call_hierarchy", "search_live", "md_journal",
        "form_export", "form_import", "form_get_xml", "form_set_xml",
        "config_open", "cfg_archive_cf", "config_save", "syntax_check",
        "update_db_cfg", "tree_activate", "tree_activate_group", "dialog_probe",
    },
}


def apply_presets(tools: list[str], presets: list[str]) -> list[str]:
    """№20: пересечь список инструментов с пресетами. Порядок исходного списка
    сохраняется; незнакомое имя пресета — ValueError со списком доступных."""
    unknown = [p for p in presets if p not in PRESETS]
    if unknown:
        raise ValueError(
            "неизвестные пресеты: %s (доступны: %s)"
            % (", ".join(unknown), ", ".join(sorted(PRESETS))))
    if not presets:
        return list(tools)
    allowed: set[str] = set()
    for p in presets:
        allowed |= PRESETS[p]
    return [t for t in tools if t in allowed]


# (метод Toolkit, имя инструмента) — описание берётся из docstring метода
_TOOLS = [    "list_sessions", "session_info",
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
    "ext_build_from_cf", "md_check_refs", "md_journal", "journal_configure", "doctor",
    "eventlog",
    "md_search_code", "module_structure", "journal_dashboard", "bridge_configure",
    "spec_check", "mouse_click", "ext_open_v2", "menu_invoke", "state_snapshot",
    "type_text",
    "query_validate", "infobase", "md_clone", "yaxunit_run", "md_rename", "call_hierarchy", "search_live", "blog_publish", "window_shot",
    "enterprise_smoke",
    "tree_activate", "tree_activate_group", "dialog_probe",
    "bridge_wait", "bridge_stats", "focus_probe",
]


def build_server(cfg: Config | None = None) -> MCPServer:
    cfg = cfg or load_config()
    tk = Toolkit(cfg)
    mcp = MCPServer(name="mcp-snegopat", version=__version__, instructions=INSTRUCTIONS)
    for name in apply_presets(_TOOLS, cfg.presets):
        mcp.add_tool(getattr(tk, name), name=name)
    return mcp


def _auth_asgi(app, token: str):
    """№27: ASGI-обёртка — только Authorization: Bearer <token> проходит.
    Чистый ASGI (без зависимостей сверх SDK), 401 с JSON-RPC-формой ошибки."""

    async def wrapped(scope, receive, send):
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            got = headers.get(b"authorization", b"").decode("latin-1", "replace")
            if got != "Bearer " + token:
                body = ('{"jsonrpc":"2.0","id":null,"error":{"code":-32001,'
                        '"message":"unauthorized"}}'.encode("utf-8"))
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length",
                                         str(len(body)).encode("ascii"))]})
                await send({"type": "http.response.body", "body": body})
                return
        await app(scope, receive, send)

    return wrapped


def _is_local_host(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in ("127.0.0.1", "localhost", "::1")


def run_http(cfg: Config) -> None:
    """№13: MCP over streamable-http (mcp-sdk 2.3+). Приложение SDK
    (streamable_http_app) при необходимости обёрнуто auth-мидлвэром (№27).
    Безопасный дефолт: НЕлокальный слушатель без auth_token — отказ."""
    if not _is_local_host(cfg.http_host) and not cfg.auth_token:
        raise SystemExit(
            "http_host %s — нелокальный слушатель, а auth_token пуст: "
            "удалённая станция без auth запрещена (задайте auth_token в "
            "конфиге)" % cfg.http_host)
    mcp = build_server(cfg)
    app = mcp.streamable_http_app()
    if cfg.auth_token:
        app = _auth_asgi(app, cfg.auth_token)
    import uvicorn
    uvicorn.run(app, host=cfg.http_host, port=int(cfg.http_port),
                log_level="warning")


def _parse_http_args(argv: list[str] | None) -> dict:
    """CLI-ключи транспорта: --http [host:port], --auth-token <токен>.
    --http без значения = http_enabled из конфига/умолчания."""
    import argparse
    ap = argparse.ArgumentParser(prog="mcp_snegopat", description=__doc__)
    ap.add_argument("--http", nargs="?", const="", default=None, metavar="HOST:PORT",
                    help="включить streamable-http (умолчание — stdio); "
                         "HOST:PORT переопределяет конфиг")
    ap.add_argument("--auth-token", default=None,
                    help="Bearer-токен для HTTP-транспорта (№27)")
    a = ap.parse_args(argv)
    out: dict = {}
    if a.http is not None:
        out["http_enabled"] = True
        if a.http:
            host, _, port = a.http.partition(":")
            if host:
                out["http_host"] = host
            if port:
                out["http_port"] = int(port)
    if a.auth_token:
        out["auth_token"] = a.auth_token
    return out


def main(argv: list[str] | None = None) -> None:
    cfg = load_config()
    overrides = _parse_http_args(
        argv if argv is not None else sys.argv[1:])
    for k, v in overrides.items():
        setattr(cfg, k, v)
    if cfg.http_enabled:
        run_http(cfg)
        return
    mcp = build_server(cfg)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
