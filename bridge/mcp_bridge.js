//engine: JScript
//uname: mcp_bridge
//dname: MCP: мост конфигуратора
//descr: Мост MCP для живого конфигуратора (проект mcp-configurator, этапы 2-5). Фиксированный список операций, без исполнения произвольного кода. Работает только при наличии mcp_bridge.on (инвариант 8 плана).
//addin: global

// Протокол (транспорт A, план §4.2), каталоги — ПО СЕССИИ, чтобы два открытых конфигуратора
// не перехватывали запросы друг друга:
//   sessions\<pid>\req\<id>.json   {op, args, deadline}   — кладёт MCP-сервер (атомарно: .tmp -> rename)
//   sessions\<pid>\resp\<id>.json  {id, ok, result | error{code, message, where}, ms, tick}
//   sessions\<pid>\session.json    — реестр сессии: контейнеры, heartbeat, running, stat
//   mcp_bridge.on — файл включения у корня (инвариант 8)
// Исполнение только из тика таймера, только при !busy и windows.modalMode == 0 (инвариант 2).
// Повтор id возвращает прежний ответ из кэша (идемпотентность, план §4.2).
// Долгие операции помечаются running в сессии; ответ появляется по завершении — таймаут клиента
// не означает «не выполнено» (план §4.2).

global.connectGlobals(SelfScript);

// ---------- конфигурация ----------

var ROOT = "Y:\\Snegopat-mcp\\mcp\\";
var TIMER_MS = 150;
var RESP_CACHE_MAX = 256;
var ENABLE_FILE = ROOT + "mcp_bridge.on";
var SESSION_DIR = ROOT + "sessions\\" + Designer.processId() + "\\";
var REQ_DIR = SESSION_DIR + "req\\";
var RESP_DIR = SESSION_DIR + "resp\\";
var BACKUPS_DIR = "Y:\\Snegopat-mcp\\backups\\";
var FORMS_DIR = ROOT + "forms\\";
var BRIDGE_VERSION = "1.0";

// Операции, помечаемые running в сессии (план §4.2: долгие блокируют мост на минуты)
var LONG_OPS = {
    config_open: 1, cfg_archive_cf: 1, config_save: 1, syntax_check: 1,
    update_db_cfg: 1, cfgstore_capture: 1, cfgstore_commit: 1
};

var CFG_GROUP = "{F10CBB81-F679-11D4-9DD3-0050BAE2BC79}";
var STG_GROUP = "{5679B714-7E65-4ED4-92BF-6DDD604C6EA3}";

var fso = new ActiveXObject("Scripting.FileSystemObject");
var busy = false;
var timerId = 0;
var stat = { ticks: 0, ticksInModal: 0, ticksBusy: 0, done: 0, replayed: 0, errors: 0, lastError: null, startedAt: new Date().getTime() };
var myPid = Designer.processId();
var sessionFile = SESSION_DIR + "session.json";
var running = null;      // {id, op, startedAt} пока выполняется долгая операция
var respCache = {};      // id -> текст ответа (идемпотентность)
var respCacheOrder = []; // порядок вставки для вытеснения

// ---------- файлы ----------

function readUtf8(path) {
    var s = new ActiveXObject("ADODB.Stream");
    s.Type = 2; s.Charset = "utf-8"; s.Open();
    s.LoadFromFile(path);
    var t = s.ReadText(-1);
    s.Close();
    if (t.charCodeAt(0) == 0xFEFF) t = t.substr(1);
    return t;
}

function writeUtf8Atomic(path, text) {
    var tmp = path + ".tmp";
    var s = new ActiveXObject("ADODB.Stream");
    s.Type = 2; s.Charset = "utf-8"; s.Open();
    s.WriteText(text);
    s.SaveToFile(tmp, 2);
    s.Close();
    // Замена через удаление+переименование; файл мог быть открыт читателем (Python)
    // в этот момент — тогда пара попыток, в конце — прямая перезапись
    for (var i = 0; i < 3; i++) {
        try {
            if (fso.FileExists(path)) fso.DeleteFile(path, true);
            fso.MoveFile(tmp, path);
            return;
        } catch (e) {
            var t0 = new Date().getTime();
            while (new Date().getTime() - t0 < 20) {}
        }
    }
    try { if (fso.FileExists(tmp)) fso.DeleteFile(tmp, true); } catch (e2) {}
    var s2 = new ActiveXObject("ADODB.Stream");
    s2.Type = 2; s2.Charset = "utf-8"; s2.Open();
    s2.WriteText(text);
    s2.SaveToFile(path, 2);
    s2.Close();
}

// ---------- текст (JScript 5 = ES3) ----------

function inArray(arr, v) { for (var i = 0; i < arr.length; i++) if (arr[i] === v) return true; return false; }

// Приведение переводов строк к \n (инвариант 4: хеш после нормализации).
function normEol(t) { return String(t).replace(/\r\n/g, "\n").replace(/\r/g, "\n"); }

// FNV-1a 32 по кодовым единицам UTF-16 нормализованного текста; та же функция в сервере Python.
function fnv1a(t) {
    var h = 0x811c9dc5;
    for (var i = 0; i < t.length; i++) {
        h ^= t.charCodeAt(i);
        h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return ("0000000" + h.toString(16)).slice(-8);
}

function eolInfo(t) {
    t = String(t);
    var crlf = (t.match(/\r\n/g) || []).length;
    var cr = (t.match(/\r/g) || []).length - crlf;
    var lf = (t.match(/\n/g) || []).length - crlf;
    return { length: t.length, crlf: crlf, lf: lf, cr: cr, endsWithEol: /[\r\n]$/.test(t), bom: t.charCodeAt(0) == 0xFEFF };
}

// ---------- строка соединения и own_bases (инвариант 5) ----------

// Нормализация пути: \ вместо /, нижний регистр, сетевой диск -> UNC, без хвостового \.
function normFilePath(p) {
    var s = String(p).replace(/\//g, "\\").toLowerCase();
    var m = /^([a-z]:)(\\.*)$/.exec(s);
    if (m) {
        try {
            var share = String(fso.GetDrive(m[1]).ShareName);
            if (share && share.length) s = share.toLowerCase() + m[2];
        } catch (e) {}
    }
    while (s.length > 3 && /\\$/.test(s)) s = s.substr(0, s.length - 1);
    return s;
}

// Нормализованный ключ строки соединения: file:<путь> | srv:<сервер>/<база>
function normConn(conn) {
    var s = String(conn), parts = {}, re = /([A-Za-z]+)\s*=\s*([^;]*)(;|$)/g, m;
    while ((m = re.exec(s)) != null) {
        var v = m[2].replace(/^\s*"(.*)"\s*$/, "$1").replace(/^\s*'(.*)'\s*$/, "$1").replace(/\s+$/, "");
        parts[m[1].toLowerCase()] = v;
    }
    if (parts.file !== undefined) return "file:" + normFilePath(parts.file);
    if (parts.srvr !== undefined) return "srv:" + parts.srvr.toLowerCase() + "/" + String(parts.ref || "").toLowerCase();
    return "raw:" + s.toLowerCase();
}

// ---------- метаданные ----------

var CLASS_PLURAL = {
    "справочник": "Справочники", "документ": "Документы", "общиймодуль": "ОбщиеМодули",
    "обработка": "Обработки", "отчет": "Отчеты", "перечисление": "Перечисления",
    "регистрсведений": "РегистрыСведений", "регистрнакопления": "РегистрыНакопления",
    "планвидовхарактеристик": "ПланыВидовХарактеристик", "константа": "Константы",
    "общаяформа": "ОбщиеФормы", "подсистема": "Подсистемы", "роль": "Роли",
    "форма": "Формы", "реквизит": "Реквизиты", "табличнаячасть": "ТабличныеЧасти",
    "макет": "Макеты", "команда": "Команды", "журналдокументов": "ЖурналыДокументов",
    "бизнеспроцесс": "БизнесПроцессы", "задача": "Задачи", "планобмена": "ПланыОбмена",
    "плансчетов": "ПланыСчетов", "планвидоврасчета": "ПланыВидовРасчета",
    "регистрбухгалтерии": "РегистрыБухгалтерии", "регистррасчета": "РегистрыРасчета",
    "измерение": "Измерения", "ресурс": "Ресурсы", "реквизитстандартный": "СтандартныеРеквизиты",
    "значениеперечисления": "ЗначенияПеречисления", "последовательность": "Последовательности",
    "параметрсеанса": "ПараметрыСеанса", "общийреквизит": "ОбщиеРеквизиты",
    "критерийотбора": "КритерииОтбора", "подписканасобытие": "ПодпискиНаСобытия",
    "регламентноезадание": "РегламентныеЗадания", "бот": "Боты",
    "функциональнаяопция": "ФункциональныеОпции",
    "параметрфункциональныхопций": "ПараметрыФункциональныхОпций",
    "определяемыйтип": "ОпределяемыеТипы", "хранилищенастроек": "ХранилищаНастроек",
    "общаякоманда": "ОбщиеКоманды", "группакоманд": "ГруппыКоманд"
};

function container(idx) {
    if (idx === undefined || idx === null) return metadata.current;
    var c = metadata.getContainer(idx);
    if (!c) throw new Error("нет контейнера " + idx);
    return c;
}

// Поиск подчинённого без учёта регистра перебором индексов
// (childObject(вид, имя) сравнивает с учётом регистра — находка ревью ред. 1).
function childByName(obj, kind, name) {
    var n = obj.childObjectsCount(kind);
    var lname = String(name).toLowerCase();
    for (var i = 0; i < n; i++) {
        var ch = obj.childObject(kind, i);
        if (ch && String(ch.name).toLowerCase() == lname) return ch;
    }
    return null;
}

// "Справочник.Имя.Форма.ФормаЭлемента" -> IV8MDObject. Каждый вызов разрешает заново (инвариант 4а).
function resolve(args) {
    var root = container(args.container).rootObject;
    if (!root) throw new Error("контейнер без открытой конфигурации");
    if (!args.path) return root;
    var parts = String(args.path).split(".");
    if (parts.length % 2 != 0) throw new Error("путь из пар Класс.Имя: " + args.path);
    var obj = root;
    for (var i = 0; i < parts.length; i += 2) {
        var kind = CLASS_PLURAL[parts[i].toLowerCase()];
        if (!kind) throw new Error("неизвестный класс " + parts[i]);
        var ch = childByName(obj, kind, parts[i + 1]);
        if (!ch) throw new Error("не найден " + parts.slice(0, i + 2).join("."));
        obj = ch;
    }
    if (args.uuid && String(obj.id).toLowerCase() != String(args.uuid).toLowerCase())
        throw new Error("uuid не совпал: " + obj.id + " против " + args.uuid);
    return obj;
}

function objInfo(obj) {
    return { name: String(obj.name), id: String(obj.id), cls: String(obj.mdclass.name(1)) };
}

function safeStr(v) {
    try {
        if (v === null || v === undefined) return null;
        var t = typeof v;
        if (t == "string" || t == "number" || t == "boolean") return v;
        return String(v);
    } catch (e) { return "?" + e.description; }
}

function cmdState(num) {
    var s = getCmdState(CFG_GROUP, num);
    return s ? { enabled: s.enabled, checked: s.checked } : null;
}

// Тип свойства "Тип": ОписаниеТипов -> читаемое представление, при сбое — внутренняя строка
function typeToStr(v) {
    try {
        var arr = v.Типы(), names = [];
        for (var i = 0; i < arr.Количество(); i++) names.push(String(arr.Получить(i)));
        var r = { types: names };
        try { r.length = v.КвалификаторыСтроки.Длина; } catch (e) {}
        try { r.digits = v.КвалификаторыЧисла.Разрядность; r.fraction = v.КвалификаторыЧисла.РазрядностьДробнойЧасти; } catch (e) {}
        return r;
    } catch (e) {
        try { return { internal: String(ЗначениеВСтрокуВнутр(v)) }; } catch (e2) { return "?" + e.description; }
    }
}

// ---------- операции ----------

var OPS = {};

OPS.ping = function (a) { return { pong: true, bridge: BRIDGE_VERSION, pid: myPid }; };

OPS.session_info = function (a) {
    return { pid: myPid, conn: sessionBase.conn, platform: sessionBase.platform,
        bridge: BRIDGE_VERSION, modalMode: windows.modalMode, running: running,
        containers: containersInfo(), stat: stat };
};

// Счёт объектов верхнего уровня по всем видам корня.
OPS.md_counts = function (a) {
    var root = container(a.container).rootObject;
    var mdc = root.mdclass;
    var res = {};
    for (var i = 0; i < mdc.childsClassesCount; i++) {
        var cls = mdc.childClassAt(i);
        res[String(cls.name(1, true))] = root.childObjectsCount(i);
    }
    return res;
};

// Полный обход дерева (диагностика слотов childObjectsCount/childObject).
OPS.md_walk = function (a) {
    var t0 = new Date().getTime();
    var total = 0, maxDepth = 0, bad = 0;
    (function walk(obj, depth) {
        total++;
        if (depth > maxDepth) maxDepth = depth;
        var mdc = obj.mdclass;
        for (var i = 0; i < mdc.childsClassesCount; i++) {
            var n = obj.childObjectsCount(i);
            for (var j = 0; j < n; j++) {
                var ch = obj.childObject(i, j);
                if (!ch || !ch.id) { bad++; continue; }
                walk(ch, depth + 1);
            }
        }
    })(container(a.container).rootObject, 0);
    return { total: total, maxDepth: maxDepth, bad: bad, ms: new Date().getTime() - t0 };
};

// Дети узла по видам, с пагинацией (ERP: десятки тысяч объектов верхнего уровня).
// kinds — фильтр видов (рус. множественное: "Справочники", "Реквизиты", ...);
// limit 0 — только счётчики. Пагинация общая по порядку видов.
OPS.md_tree = function (a) {
    var obj = resolve(a);
    var mdc = obj.mdclass;
    var kindsFilter = a.kinds || null;
    if (kindsFilter) for (var kf = 0; kf < kindsFilter.length; kf++) kindsFilter[kf] = String(kindsFilter[kf]).toLowerCase();
    var kinds = [], flat = [];
    for (var i = 0; i < mdc.childsClassesCount; i++) {
        var kindName = String(mdc.childClassAt(i).name(1, true));
        if (kindsFilter && !inArray(kindsFilter, kindName.toLowerCase())) continue;
        var n = obj.childObjectsCount(i);
        kinds.push({ kind: kindName, count: n });
        for (var j = 0; j < n; j++) flat.push({ kind: kindName, j: j });
    }
    var total = flat.length;
    var offset = a.offset || 0;
    var limit = a.limit === undefined ? 100 : a.limit;
    if (limit > 1000) limit = 1000;
    var items = [];
    for (var q = offset; q < total && items.length < limit; q++) {
        var ch = obj.childObject(flat[q].kind, flat[q].j);
        if (!ch) { items.push({ kind: flat[q].kind, error: "null" }); continue; }
        items.push({ kind: flat[q].kind, name: String(ch.name), id: String(ch.id), cls: String(ch.mdclass.name(1)) });
    }
    return { obj: objInfo(obj), kinds: kinds, total: total, offset: offset, limit: limit,
        items: items, hasMore: offset + items.length < total };
};

// Список подчинённых заданного вида (имена и uuid) — сверки до/после.
OPS.md_children = function (a) {
    var obj = resolve(a);
    var n = obj.childObjectsCount(a.kind), res = [];
    for (var i = 0; i < n; i++) { var ch = obj.childObject(a.kind, i); res.push({ name: String(ch.name), id: String(ch.id) }); }
    return { obj: objInfo(obj), kind: a.kind, count: n, items: res };
};

// Поиск по подстроке имени (без учёта регистра) по всему дереву, с пределом.
// Путь в ответе — относительно корня, в формате resolve() ("Справочник.Имя.Реквизит.Х").
OPS.md_find = function (a) {
    var mask = String(a.mask || "").toLowerCase();
    if (!mask) throw new Error("пустая маска поиска");
    var wantKind = a.kind ? String(a.kind).toLowerCase() : null;
    var limit = a.limit || 50;
    if (limit > 500) limit = 500;
    var t0 = new Date().getTime();
    var matches = [], truncated = false;
    // стек: {obj, path} — path сам объект относительно корня (у корня — "")
    var stack = [{ obj: container(a.container).rootObject, path: "" }];
    while (stack.length) {
        var cur = stack.pop();
        var obj = cur.obj;
        var kindHere = String(obj.mdclass.name(1, true)).toLowerCase();
        if (cur.path && String(obj.name).toLowerCase().indexOf(mask) >= 0 && (!wantKind || kindHere == wantKind)) {
            matches.push({ path: cur.path, name: String(obj.name), id: String(obj.id), cls: String(obj.mdclass.name(1)) });
            if (matches.length >= limit) { truncated = true; break; }
        }
        var mdc = obj.mdclass;
        for (var i = 0; i < mdc.childsClassesCount; i++) {
            var n = obj.childObjectsCount(i);
            for (var j = n - 1; j >= 0; j--) {
                var ch = obj.childObject(i, j);
                if (ch && ch.id)
                    stack.push({ obj: ch,
                        path: cur.path
                            ? cur.path + "." + String(ch.mdclass.name(1)) + "." + String(ch.name)
                            : String(ch.mdclass.name(1)) + "." + String(ch.name) });
            }
        }
    }
    return { mask: a.mask, kind: a.kind || null, matches: matches, truncated: truncated, ms: new Date().getTime() - t0 };
};

// Объект: класс, uuid, имя, синоним, комментарий, свойства со значениями, модули с хешами.
OPS.md_object = function (a) {
    var obj = resolve(a);
    var info = objInfo(obj);
    info.comment = safeStr(obj.comment);
    info.synonymRu = safeStr(obj.synonym("ru"));
    var mdc = obj.mdclass, props = [], modules = [];
    for (var i = 0; i < mdc.propertiesCount; i++) {
        var p = mdc.propertyAt(i), pname = String(p.name(1)), isMod = false;
        try { isMod = obj.isPropModule(pname); } catch (e) {}
        if (isMod) {
            props.push({ name: pname, isModule: true });
            try {
                var mt = String(obj.getModuleText(pname));
                modules.push({ prop: pname, hash: fnv1a(normEol(mt)),
                    lines: mt.length ? normEol(mt).split("\n").length : 0 });
            } catch (e) { modules.push({ prop: pname, error: String(e.description) }); }
        } else {
            var rec = { name: pname };
            try {
                if (pname == "Тип") rec.value = typeToStr(obj.property(pname));
                else rec.value = safeStr(obj.property(pname));
            } catch (e) { rec.value = "?" + e.description; }
            props.push(rec);
        }
    }
    info.props = props;
    info.modules = modules;
    return info;
};

OPS.prop_get = function (a) {
    var obj = resolve(a);
    var v = obj.property(a.prop);
    var res = { obj: objInfo(obj), type: typeof v, value: safeStr(v) };
    if (String(a.prop) == "Тип") res.value = typeToStr(v);
    return res;
};

// Чтение свойства-объекта (Состав подсистемы, Владельцы, ...) внутренней строкой.
OPS.prop_get_internal = function (a) {
    var obj = resolve(a);
    return { obj: objInfo(obj), internal: String(ЗначениеВСтрокуВнутр(obj.property(a.prop))) };
};

// Запись простого свойства с ожидаемым значением и постусловием (инварианты 3, 4).
OPS.prop_set = function (a) {
    var obj = resolve(a);
    var before = safeStr(obj.property(a.prop));
    if (a.expected !== undefined && before !== a.expected)
        throw new Error("текущее значение не совпало с ожидаемым: " + before);
    var okSet = obj.setProperty(a.prop, a.value);
    // После переименования старый путь недействителен — постусловие ищет по uuid
    var obj2 = obj;
    if (String(a.prop).toLowerCase() == "имя") {
        var pp = String(a.path).split("."); pp[pp.length - 1] = a.value;
        obj2 = resolve({ container: a.container, path: pp.join("."), uuid: String(obj.id) });
    } else obj2 = resolve(a);
    var after = safeStr(obj2.property(a.prop));
    if (after !== a.value) throw new Error("постусловие: значение после записи не совпало: " + after);
    return { obj: objInfo(obj2), setResult: okSet, before: before, after: after,
        isModified: container(a.container).isModified };
};

// Запись свойства-объекта внутренним представлением с ожидаемым значением.
OPS.prop_set_internal = function (a) {
    var obj = resolve(a);
    if (a.expectId && String(obj.id) != a.expectId) throw new Error("uuid не совпал");
    var before = String(ЗначениеВСтрокуВнутр(obj.property(a.prop)));
    if (a.expectBefore !== undefined && before != a.expectBefore) throw new Error("текущее значение не совпало с ожидаемым");
    var ok = obj.setProperty(a.prop, ЗначениеИзСтрокиВнутр(a.internal));
    var after = String(ЗначениеВСтрокуВнутр(resolve(a).property(a.prop)));
    if (after != String(a.internal)) throw new Error("постусловие: внутреннее значение после записи не совпало");
    return { obj: objInfo(obj), set: ok, before: before, after: after };
};

// Тип реквизита: простые типы через v8New("ОписаниеТипов", ...).
// Ссылочные типы так роняют процесс — только prop_set_type_internal (этап 7).
// Квалификаторы — только при наличии: null/undefined в v8New("КвалификаторыЧисла")
// вешал движок (24.10.2026).
function typeDescr(t) {
    var hasDigits = t.digits !== undefined && t.digits !== null;
    var hasLen = t.length !== undefined && t.length !== null;
    var nq = hasDigits ? v8New("КвалификаторыЧисла", t.digits, t.fraction || 0) : undefined;
    var sq = hasLen ? v8New("КвалификаторыСтроки", t.length) : undefined;
    return v8New("ОписаниеТипов", t.types.join(","), nq, sq);
}
OPS.prop_set_type = function (a) {
    var obj = resolve(a);
    if (a.expectId && String(obj.id) != a.expectId) throw new Error("uuid не совпал");
    var before = typeToStr(obj.property("Тип"));
    var ok = obj.setProperty("Тип", typeDescr(a.type));
    var after = typeToStr(resolve(a).property("Тип"));
    return { obj: objInfo(obj), before: before, set: ok, after: after };
};

// Ссылочные и составные типы — внутренней строкой вида {"#",f5c65050-…,{…}}.
// GUID прикладного типа — из type_lookup (typeList контейнера).
OPS.type_lookup = function (a) {
    var lst = container(a.container).typeList("", 1), res = [], want = String(a.name || "").toLowerCase();
    for (var i = 0; i < lst.length; i++) {
        var t = lst.item(i);
        if (!a.name || String(t.name).toLowerCase() == want) res.push({ name: String(t.name), clsid: String(t.clsid) });
        if (res.length > (a.limit || 20)) break;
    }
    return { total: lst.length, found: res };
};
OPS.prop_set_type_internal = function (a) {
    var obj = resolve(a);
    if (a.expectId && String(obj.id) != a.expectId) throw new Error("uuid не совпал");
    if (!/^\{"#",f5c65050-3bbb-11d5-b988-0050bae0a95d,/.test(String(a.internal))) throw new Error("не описание типов");
    var before = typeToStr(obj.property("Тип"));
    var td = ЗначениеИзСтрокиВнутр(a.internal);
    var ok = obj.setProperty("Тип", td);
    var after = typeToStr(resolve(a).property("Тип"));
    return { obj: objInfo(obj), before: before, set: ok, after: after };
};

// ---------- модули ----------

// Текст модуля: хеш, размер, перевод строк; withText — сам текст; openEditor — буфер и readOnly.
OPS.module_get = function (a) {
    var obj = resolve(a);
    if (!obj.isPropModule(a.prop)) throw new Error("свойство не модуль: " + a.prop);
    var text = String(obj.getModuleText(a.prop));
    var norm = normEol(text);
    var res = { obj: objInfo(obj), prop: a.prop, hash: fnv1a(norm),
        lines: norm.length ? norm.split("\n").length : 0, length: text.length, eol: eolInfo(text) };
    if (a.openEditor) {
        var w = obj.openModule(a.prop);
        if (!w) throw new Error("openModule вернул null");
        res.readOnly = w.readOnly;
        var buf = String(w.text);
        res.bufferHash = fnv1a(normEol(buf));
        res.bufferSame = res.bufferHash == res.hash;
    }
    if (a.withText) res.text = text;
    return res;
};

// Замена всего текста модуля через редактор: выделение (1,1)-(n+1,1), selectedText=.
// getModuleText — живой буфер вместе с несохранённой правкой человека (опыт 3а):
// expected_hash защищает и от неё, и от параллельной правки.
OPS.module_set = function (a) {
    var obj = resolve(a);
    if (!obj.isPropModule(a.prop)) throw new Error("свойство не модуль: " + a.prop);
    var w = obj.openModule(a.prop);
    if (!w) throw new Error("openModule вернул null");
    if (w.readOnly) throw new Error("модуль только для чтения");
    var bufText = String(w.text), propText = String(obj.getModuleText(a.prop));
    if (normEol(bufText) != normEol(propText)) throw new Error("в редакторе несохранённая правка: буфер и свойство расходятся");
    var curHash = fnv1a(normEol(bufText));
    if (a.expected_hash !== curHash) throw new Error("expected_hash не совпал: текущий " + curHash);
    var t0 = new Date().getTime();
    var n = w.linesCount;
    w.setSelection(1, 1, n + 1, 1);
    w.selectedText = a.text;
    var ms = new Date().getTime() - t0;
    var obj2 = resolve(a);
    var after = String(w.text), afterProp = String(obj2.getModuleText(a.prop));
    var want = normEol(a.text);
    if (normEol(after) != want || normEol(afterProp) != want)
        throw new Error("постусловие: текст после записи не совпал с переданным");
    return { obj: objInfo(obj2), ms: ms, linesBefore: n, linesAfter: w.linesCount,
        hash: fnv1a(normEol(afterProp)), isModified: container(a.container).isModified };
};

// Точечные правки по номерам строк (1-based): edits = [{line, from, to}].
// Строка сверяется точно; хеш модуля обязателен. Меньше трафика, меньше шанс затереть.
OPS.module_patch = function (a) {
    var obj = resolve(a);
    if (!obj.isPropModule(a.prop)) throw new Error("свойство не модуль: " + a.prop);
    var edits = a.edits || [];
    if (!edits.length) throw new Error("пустой список правок");
    var w = obj.openModule(a.prop);
    if (!w) throw new Error("openModule вернул null");
    if (w.readOnly) throw new Error("модуль только для чтения");
    var bufText = String(w.text), propText = String(obj.getModuleText(a.prop));
    if (normEol(bufText) != normEol(propText)) throw new Error("в редакторе несохранённая правка: буфер и свойство расходятся");
    var curHash = fnv1a(normEol(bufText));
    if (a.expected_hash !== curHash) throw new Error("expected_hash не совпал: текущий " + curHash);
    var norm = normEol(bufText);
    var trailingEol = /\n$/.test(norm);
    var lines = norm.split("\n");
    if (trailingEol) lines.pop(); // хвостовой пустой элемент от завершающего \n
    for (var i = 0; i < edits.length; i++) {
        var e = edits[i], li = e.line - 1;
        if (!(li >= 0 && li < lines.length)) throw new Error("правка " + i + ": строка вне модуля: " + e.line);
        if (String(lines[li]) != String(e.from)) throw new Error("правка " + i + ": строка " + e.line + " не совпала: «" + lines[li] + "»");
        lines[li] = String(e.to);
    }
    var newText = lines.join("\n") + (trailingEol || lines.length == 0 ? "\n" : "");
    var t0 = new Date().getTime();
    var n = w.linesCount;
    w.setSelection(1, 1, n + 1, 1);
    w.selectedText = newText;
    var ms = new Date().getTime() - t0;
    var obj2 = resolve(a);
    var after = String(w.text), afterProp = String(obj2.getModuleText(a.prop));
    var want = normEol(newText);
    if (normEol(after) != want || normEol(afterProp) != want)
        throw new Error("постусловие: текст после правок не совпал с собранным");
    return { obj: objInfo(obj2), ms: ms, applied: edits.length, linesAfter: w.linesCount,
        hash: fnv1a(normEol(afterProp)), isModified: container(a.container).isModified };
};

// ---------- формы (внутренний текст, этап 8 плана: через открытый редактор) ----------

function formFile(name) {
    if (!/^[A-Za-z0-9_.\-]+$/.test(String(name))) throw new Error("недопустимое имя файла " + name);
    if (!fso.FolderExists(FORMS_DIR)) fso.CreateFolder(FORMS_DIR);
    return FORMS_DIR + name;
}
function extPropWithEditor(a) {
    var obj = resolve(a);
    try { obj.editProperty(a.prop); } catch (e) {}
    var ep = resolve(a).getExtProp(a.prop);
    if (!ep) throw new Error("getExtProp вернул null");
    return ep;
}
OPS.form_save = function (a) {
    var path = formFile(a.file);
    var ep = extPropWithEditor(a);
    var f = v8files.open("file://" + path, 0x2 | 0x8 | 0x10);
    if (!f) throw new Error("не открыт файл " + path);
    var t0 = new Date().getTime();
    ep.saveToFile(f);
    f.close();
    return { file: path, size: fso.GetFile(path).Size, ms: new Date().getTime() - t0 };
};
OPS.form_load = function (a) {
    var path = formFile(a.file);
    if (!fso.FileExists(path)) throw new Error("нет файла " + path);
    var ep = extPropWithEditor(a);
    var f = v8files.open("file://" + path, 0x8);
    if (!f) throw new Error("не открыт файл " + path);
    var t0 = new Date().getTime();
    var ok = ep.loadFromFile(f);
    f.close();
    // loadFromFile не выставляет isModified (опыт 4а) — правку надо отмечать сохранением
    return { file: path, loaded: ok, ms: new Date().getTime() - t0, isModified: container(a.container).isModified };
};
OPS.extprop_probe = function (a) {
    var obj = resolve(a);
    var ep = obj.getExtProp(a.prop);
    if (!ep) return { obj: objInfo(obj), extProp: null };
    var r = { obj: objInfo(obj), idData: safeStr(ep.idData), idEditor: safeStr(ep.idEditor) };
    try { r.probe = safeStr(ep.probe()); } catch (e) { r.probe = "?" + e.description; }
    return r;
};

// ---------- конфигурация: открыть, архив, сохранить ----------

// Открыть основную конфигурацию (без этого metadata.current — конфигурация БД).
OPS.config_open = function (a) {
    var before = { open: cmdState(2), close: cmdState(4), save: cmdState(3) };
    var sent = false;
    if (before.open && before.open.enabled) sent = sendCommand(CFG_GROUP, 2);
    return { before: before, sent: sent, after: { open: cmdState(2), close: cmdState(4), save: cmdState(3) },
        currentIdentifier: safeStr(metadata.current.identifier), openedCount: metadata.openedCount };
};

// Архив cf штатной командой «Сохранить конфигурацию в файл» (Config, 7):
// путь подставляется перехватом onSelectFile, вопрос о несохранённых правках — «Нет»
// (в cf уходит последнее сохранённое состояние = точка отката; решение владельца 16.10.2026).
var pendingArchive = null;
function onSelectFileMcp(fd) {
    try {
        if (!pendingArchive) return;
        var ev = { mode: fd.mode, defExt: safeStr(fd.defExt), title: safeStr(fd.title), initial: safeStr(fd.initialFileName) };
        pendingArchive.selectEvents.push(ev);
        if (fd.mode == 1 && !pendingArchive.used) {
            fd.addSelectedFile(pendingArchive.path);
            fd.result = 1;
            pendingArchive.used = true;
            ev.action = "подставлен путь";
        } else {
            fd.result = 2;
            ev.action = "отмена (не наш диалог)";
        }
    } catch (e) { if (pendingArchive) pendingArchive.errors.push("onSelectFile: " + e.description); }
}
function onMsgBoxMcp(mb) {
    try {
        if (!pendingArchive) return;
        var ev = { caption: safeStr(mb.caption), text: safeStr(mb.text), type: mb.type };
        pendingArchive.msgBoxes.push(ev);
        mb.cancel = true;
        if ((mb.type & 0x0F) == 3 && /была изменена\. Сохранить изменения\?$/.test(String(mb.text))) { mb.result = 7; ev.action = "Нет"; }
        else if ((mb.type & 0x0F) == 4 && /^Обнаружены возможные ошибки в конфигурации\. Продолжить\?$/.test(String(mb.text)))
            { if (pendingArchive.acceptWarnings) { mb.result = 6; ev.action = "Да (acceptWarnings)"; } else { mb.result = 7; ev.action = "Нет"; } }
        else if ((mb.type & 0x0F) == 0) { mb.result = 1; ev.action = "ОК"; }
        else { mb.result = 2; ev.action = "Отмена"; }
    } catch (e) { if (pendingArchive) pendingArchive.errors.push("onMessageBox: " + e.description); }
}
function onSaveMsg(p) { try { if (pendingArchive && pendingArchive.messages) pendingArchive.messages.push(String(p.text)); } catch (e) {} }
function backupFile(name) {
    if (!/^[A-Za-z0-9_.\-]+\.cf$/.test(String(name))) throw new Error("недопустимое имя cf " + name);
    var path = BACKUPS_DIR + String(name);
    if (fso.FileExists(path)) throw new Error("файл уже есть " + path);
    return path;
}
OPS.cfg_archive_cf = function (a) {
    var path = backupFile(a.file);
    var st = cmdState(7);
    if (!st || !st.enabled) throw new Error("команда «Сохранить конфигурацию в файл» недоступна");
    pendingArchive = { path: path, used: false, selectEvents: [], msgBoxes: [], errors: [] };
    var sent = false, t0 = new Date().getTime();
    try {
        events.connect(Designer, "onSelectFile", SelfScript.self, "onSelectFileMcp");
        events.connect(windows, "onMessageBox", SelfScript.self, "onMsgBoxMcp");
        sent = sendCommand(CFG_GROUP, 7);
    } finally {
        try { events.disconnect(Designer, "onSelectFile", SelfScript.self, "onSelectFileMcp"); } catch (e) {}
        try { events.disconnect(windows, "onMessageBox", SelfScript.self, "onMsgBoxMcp"); } catch (e) {}
    }
    var pa = pendingArchive; pendingArchive = null;
    var res = { file: path, sent: sent, ms: new Date().getTime() - t0, pathUsed: pa.used,
        selectEvents: pa.selectEvents, msgBoxes: pa.msgBoxes, errors: pa.errors,
        exists: fso.FileExists(path) };
    if (res.exists) res.size = fso.GetFile(path).Size;
    if (!res.exists || !res.size || !pa.used)
        throw new Error("архив cf не создан (exists=" + res.exists + ", used=" + pa.used + ")");
    return res;
};

// Сохранение конфигурации (Config, 3). Только при архиве cf, снятом в этой сессии (инвариант 5).
OPS.config_save = function (a) {
    var arch = String(a.archive || "");
    if (!/^Y:\\Snegopat-mcp\\backups\\[A-Za-z0-9_.\-]+\.cf$/.test(arch) || !fso.FileExists(arch) || fso.GetFile(arch).Size == 0)
        throw new Error("нет архива cf перед сохранением: " + arch);
    var c = container(a.container);
    var before = c.isModified;
    var st = cmdState(3);
    if (!st || !st.enabled) throw new Error("команда «Сохранить конфигурацию» недоступна (isModified=" + before + ")");
    pendingArchive = { path: "", used: true, selectEvents: [], msgBoxes: [], errors: [], messages: [], acceptWarnings: !!a.acceptWarnings };
    var sent = false, t0 = new Date().getTime();
    try {
        events.connect(windows, "onMessageBox", SelfScript.self, "onMsgBoxMcp");
        events.connect(windows, "onMessage", SelfScript.self, "onSaveMsg");
        sent = sendCommand(CFG_GROUP, 3);
    } finally {
        try { events.disconnect(windows, "onMessageBox", SelfScript.self, "onMsgBoxMcp"); } catch (e) {}
        try { events.disconnect(windows, "onMessage", SelfScript.self, "onSaveMsg"); } catch (e) {}
    }
    var pa = pendingArchive; pendingArchive = null;
    var res = { sent: sent, ms: new Date().getTime() - t0, isModifiedBefore: before, isModifiedAfter: c.isModified,
        msgBoxes: pa.msgBoxes, messages: pa.messages, errors: pa.errors, archive: arch };
    if (sent && c.isModified) throw new Error("постусловие: конфигурация всё ещё изменена после сохранения");
    return res;
};

// ---------- этап 7: создание и удаление через дерево метаданных ----------

// Выделить объект в дереве.
OPS.tree_activate = function (a) {
    var obj = resolve(a);
    obj.activateInTree();
    return objInfo(obj);
};
// Послать клавиши навигации дерева БЕЗ Insert — отдельным тиком: раскрытие узла
// («+») завершается асинхронно, и нажатия из того же вызова уходят мимо.
// Пара tree_nav (раскрытие) -> пауза -> md_add_child (ход + Insert) надёжнее
// одного пакета клавиш (проверено на узле «Общие», 24.10.2026).
OPS.tree_nav = function (a) {
    var keys = a.keys || [];
    var hwnd = winApi.getFocus();
    if (!hwnd) hwnd = ensureInputFocus();
    if (!hwnd) throw new Error("нет фокуса ввода: активируйте окно конфигуратора");
    for (var k = 0; k < keys.length; k++) {
        if (!inArray([0x25, 0x26, 0x27, 0x28, 0x24, 0x23, 0x6B, 0x6D], keys[k]))
            throw new Error("не клавиша навигации: " + keys[k]);
        winApi.sendMessage(hwnd, 0x100, keys[k], 0x01000001);
        winApi.sendMessage(hwnd, 0x101, keys[k], 0xC1000001);
    }
    return { sent: keys.length };
};
// Выделить узел группы подчинённых вида kind (activateInTreeEx с GUID класса подчинённых).
OPS.tree_activate_group = function (a) {
    var obj = resolve(a);
    var mdc = obj.mdclass, guid = null, names = [];
    for (var i = 0; i < mdc.childsClassesCount; i++) {
        var cc = mdc.childClassAt(i), nm = String(cc.name(1, true));
        names.push(nm);
        if (nm.toLowerCase() == String(a.kind).toLowerCase()) guid = String(cc.id);
    }
    if (!guid) throw new Error("у класса нет подчинённых вида " + a.kind + "; есть: " + names.join(", "));
    var ok = obj.activateInTreeEx(guid);
    return { obj: objInfo(obj), kind: a.kind, classGuid: guid, ok: ok };
};

var addCapture = null;
function onMdAdd(e) {
    try {
        if (!addCapture || e.kind != 0) return;
        var rec = { request: e.request };
        try { rec.id = String(e.obj.id); rec.name = String(e.obj.name); rec.cls = String(e.obj.mdclass.name(1)); } catch (x) {}
        try { rec.parentId = String(e.parentObj.id); } catch (x) {}
        addCapture.events.push(rec);
    } catch (x) {}
}
function listControls(form) {
    var res = [];
    try {
        for (var i = 0; i < form.controlsCount; i++) {
            var c = form.getControl(i), r = { i: i };
            try { r.name = safeStr(c.name); } catch (e) {}
            try { r.value = safeStr(c.value); } catch (e) { r.value = "?"; }
            res.push(r);
        }
    } catch (e) { res.push({ error: String(e.description) }); }
    return res;
}
// Модальные окна при создании (конструктор формы и т.п.): политика wizard — нажать кнопки
// конструктора; любой незнакомый диалог — Отмена (инвариант 6).
function onAddModal(d) {
    try {
        if (!addCapture) return;
        var ev = { stage: d.stage, caption: safeStr(d.caption) };
        if (d.stage == 1) {
            var wz = addCapture.wizard;
            if (addCapture.modalPolicy == "wizard" && wz && wz.caption == String(d.caption)) {
                ev.set = [];
                var steps = wz.steps || [];
                for (var si = 0; si < steps.length; si++) {
                    var st = steps[si];
                    try {
                        if (st.click) { d.form.sendEvent(d.form.getControl(st.click).id, 0); ev.set.push("click " + st.click); }
                        if (st.set) for (var sn in st.set) if (st.set.hasOwnProperty(sn)) {
                            d.form.getControl(sn).value = st.set[sn]; ev.set.push(sn + "=" + safeStr(d.form.getControl(sn).value));
                        }
                    } catch (e) { ev.set.push("шаг " + si + ": " + e.description); }
                }
                for (var ai = 0; ai < (wz.report || []).length; ai++) {
                    try { ev.after = ev.after || []; ev.after.push(wz.report[ai] + "=" + safeStr(d.form.getControl(wz.report[ai]).value)); } catch (e) {}
                }
                if (steps.length) { ev.action = "шаги выполнены"; addCapture.modals.push(ev); return; }
                for (var nm in wz.values) if (wz.values.hasOwnProperty(nm)) {
                    try { d.form.getControl(nm).value = wz.values[nm]; ev.set.push(nm + "=" + safeStr(d.form.getControl(nm).value)); }
                    catch (e) { ev.set.push(nm + ": " + e.description); }
                }
                if (wz.click) {
                    try { d.form.sendEvent(d.form.getControl(wz.click).id, 0); ev.action = "нажата " + wz.click; }
                    catch (e) { d.cancel = true; d.result = 2; ev.action = "Отмена (кнопка не нажалась: " + e.description + ")"; }
                } else { d.cancel = true; d.result = 1; ev.action = "ОК"; }
            } else { d.cancel = true; d.result = 2; ev.action = "Отмена"; }
        }
        if (d.stage == 2) { try { ev.result = safeStr(d.result); } catch (e) {} }
        addCapture.modals.push(ev);
    } catch (e) {}
}

// Подъём окна конфигуратора изнутри процесса (когда фокуса нет): SendKeys("%") даёт
// процессу право на foreground, AppActivate поднимает окно. AttachThreadInput/SetFocus
// снаружи не применяются — после них создание окон 1cv8 зависало (23.10.2026).
function ensureInputFocus() {
    var hwnd = winApi.getFocus();
    if (hwnd) return hwnd;
    try {
        var sh = new ActiveXObject("WScript.Shell");
        sh.SendKeys("%");
        if (sh.AppActivate(myPid)) {
            // AppActivate синхронен; фокус обновляется сразу
            hwnd = winApi.getFocus();
        }
    } catch (e) { stat.lastError = "AppActivate: " + e.description; }
    return hwnd || 0;
}

// Создать подчинённый объект. Два способа навигации (этап 7):
//  sibling — выделить соседний объект того же вида и Insert;
//  firstChild — выделить владельца, navKeys (клавиши дерева) до узла группы и Insert.
// Созданный объект принимается только при совпадении класса и владельца в mdeAdd; прочее — stray.
OPS.md_add_child = function (a) {
    var owner = resolve({ container: a.container, path: a.ownerPath });
    var ownerId = String(owner.id);
    var kind = a.kind;
    var before = owner.childObjectsCount(kind);
    var sib = a.firstChild ? null : childByName(owner, kind, a.sibling);
    if (!sib && !a.firstChild) throw new Error("не найден соседний объект " + a.sibling);
    if (!a.noActivate) (sib || owner).activateInTree();
    if (a.requireTree) {
        var av = null; try { av = windows.getActiveView(); } catch (e) {}
        var avTitle = av ? safeStr(av.title) : null;
        if (!avTitle || String(avTitle).indexOf("Конфигурация") != 0) throw new Error("фокус не в дереве метаданных: активно «" + avTitle + "»");
    }
    addCapture = { events: [], modals: [], modalPolicy: a.modalPolicy || "cancel", wizard: a.wizard || null };
    var sent = false, hwnd = 0;
    try {
        events.connect(windows, "onDoModal", SelfScript.self, "onAddModal");
        events.connect(metadata, "MetaDataEvent", SelfScript.self, "onMdAdd");
        hwnd = ensureInputFocus();
        // hwnd у представлений в SnegAPI на 8.3.12+ всегда 0: нажатия идут в окно
        // фокуса ввода; без него (окно конфигуратора в фоне и AppActivate не помог) — отказ
        if (!hwnd) throw new Error("нет фокуса ввода: активируйте окно конфигуратора (сервер делает это перед операциями дерева)");
        var nav = a.navKeys || [];
        for (var k = 0; k < nav.length; k++) {
            // только клавиши навигации: стрелки, Home/End, серые «+»/«−»
            if (!inArray([0x25, 0x26, 0x27, 0x28, 0x24, 0x23, 0x6B, 0x6D], nav[k])) throw new Error("не клавиша навигации: " + nav[k]);
            winApi.sendMessage(hwnd, 0x100, nav[k], 0x01000001);
            winApi.sendMessage(hwnd, 0x101, nav[k], 0xC1000001);
        }
        winApi.sendMessage(hwnd, 0x100, 0x2D, 0x01520001);
        winApi.sendMessage(hwnd, 0x101, 0x2D, 0xC1520001);
        sent = true;
    } finally {
        try { events.disconnect(metadata, "MetaDataEvent", SelfScript.self, "onMdAdd"); } catch (e) {}
        try { events.disconnect(windows, "onDoModal", SelfScript.self, "onAddModal"); } catch (e) {}
    }
    var cap = addCapture; addCapture = null;
    var owner2 = resolve({ container: a.container, path: a.ownerPath });
    var after = owner2.childObjectsCount(kind);
    var created = null;
    for (var i = 0; i < cap.events.length; i++) {
        var ev = cap.events[i];
        if (!ev.request && ev.parentId == ownerId) created = ev;
    }
    var res = { sent: sent, method: "key", hwnd: hwnd, before: before, after: after,
        events: cap.events, created: created, modals: cap.modals };
    res.stray = [];
    for (var s2 = 0; s2 < cap.events.length; s2++) { var e2 = cap.events[s2]; if (!e2.request && e2.parentId != ownerId) res.stray.push(e2); }
    if (created && a.expectClass && created.cls != a.expectClass) { res.classMismatch = created.cls; res.wrong = created; created = null; res.created = null; }
    if (!created) return res;
    // Новый объект — заново по uuid среди подчинённых владельца (инвариант 4а)
    var obj = null;
    for (var j = 0; j < after; j++) { var ch = owner2.childObject(kind, j); if (String(ch.id) == created.id) { obj = ch; break; } }
    res.foundById = !!obj;
    if (obj && a.newName) {
        res.renameResult = obj.setProperty("Имя", a.newName);
        var obj2 = childByName(resolve({ container: a.container, path: a.ownerPath }), kind, a.newName);
        res.renamed = !!obj2 && String(obj2.id) == created.id;
        if (!res.renamed) throw new Error("переименование не подтверждено: объект " + created.id + " не найден под именем " + a.newName);
        if (obj2 && a.comment) { obj2.setProperty("Комментарий", a.comment); res.comment = safeStr(obj2.property("Комментарий")); }
    }
    return res;
};

// Удаление клавишей Delete в дереве. Только с явным uuid (инвариант 5).
var delCapture = null;
var a_delModalAllowed = [];
function onDelBox(m) {
    try {
        if (!delCapture) return;
        var ev = { caption: safeStr(m.caption), text: safeStr(m.text), type: m.type };
        delCapture.boxes.push(ev);
        m.cancel = true;
        var t = m.type & 0x0F;
        if ((t == 4 || t == 3 || t == 1) && /удал/i.test(String(m.text))) { m.result = (t == 1 ? 1 : 6); ev.action = "Да"; }
        else { m.result = 2; ev.action = "Отмена"; }
    } catch (e) {}
}
function onDelModal(d) {
    try {
        if (!delCapture) return;
        var ev = { stage: d.stage, caption: safeStr(d.caption) };
        delCapture.modals.push(ev);
        if (d.stage == 1 && !inArray(a_delModalAllowed, String(d.caption))) { d.cancel = true; d.result = 2; ev.action = "Отмена (неизвестный диалог)"; }
    } catch (e) {}
}
function onDelMd(e) { try { if (delCapture && e.kind == 2) delCapture.md.push({ request: e.request, id: e.obj ? String(e.obj.id) : null }); } catch (x) {} }
OPS.md_delete = function (a) {
    var obj = resolve(a);
    if (!a.uuid) throw new Error("удаление только с явным uuid");
    var id = String(obj.id), parentPath = String(a.path).split(".").slice(0, -2).join("."), kind = CLASS_PLURAL[String(a.path).split(".").slice(-2)[0].toLowerCase()];
    var owner = resolve({ container: a.container, path: parentPath });
    var before = owner.childObjectsCount(kind);
    if (!a.noActivate) obj.activateInTree();
    if (a.requireTree) {
        var av = null; try { av = windows.getActiveView(); } catch (e) {}
        var avTitle = av ? safeStr(av.title) : null;
        if (!avTitle || String(avTitle).indexOf("Конфигурация") != 0) throw new Error("фокус не в дереве метаданных: активно «" + avTitle + "»");
    }
    delCapture = { boxes: [], modals: [], md: [] };
    a_delModalAllowed = a.allowModals || [];
    try {
        events.connect(windows, "onMessageBox", SelfScript.self, "onDelBox");
        events.connect(windows, "onDoModal", SelfScript.self, "onDelModal");
        events.connect(metadata, "MetaDataEvent", SelfScript.self, "onDelMd");
        var hwnd = ensureInputFocus();
        if (!hwnd) throw new Error("нет фокуса ввода: активируйте окно конфигуратора (сервер делает это перед операциями дерева)");
        winApi.sendMessage(hwnd, 0x100, 0x2E, 0x01530001);
        winApi.sendMessage(hwnd, 0x101, 0x2E, 0xC1530001);
    } finally {
        try { events.disconnect(windows, "onMessageBox", SelfScript.self, "onDelBox"); } catch (e) {}
        try { events.disconnect(windows, "onDoModal", SelfScript.self, "onDelModal"); } catch (e) {}
        try { events.disconnect(metadata, "MetaDataEvent", SelfScript.self, "onDelMd"); } catch (e) {}
    }
    var cap = delCapture; delCapture = null;
    var owner2 = resolve({ container: a.container, path: parentPath });
    var after = owner2.childObjectsCount(kind), still = false;
    for (var i = 0; i < after; i++) if (String(owner2.childObject(kind, i).id) == id) still = true;
    if (still) throw new Error("постусловие: объект всё ещё присутствует после удаления");
    return { id: id, before: before, after: after, boxes: cap.boxes, modals: cap.modals, md: cap.md };
};

// ---------- этап 5: проверка, хранилище, обновление БД ----------

// Синтаксическая проверка (Config, 9): результат — из окна сообщений (onMessage).
var synCapture = null;
function onSynMsg(p) { try { if (synCapture) synCapture.messages.push(String(p.text)); } catch (e) {} }
function onSynBox(m) {
    try {
        if (!synCapture) return;
        var ev = { caption: safeStr(m.caption), text: safeStr(m.text), type: m.type };
        synCapture.boxes.push(ev);
        m.cancel = true;
        var t = m.type & 0x0F;
        if (t == 0) { m.result = 1; ev.action = "ОК"; }
        else { m.result = 2; ev.action = "Отмена"; synCapture.refused.push(safeStr(m.caption) + ": " + safeStr(m.text)); }
    } catch (e) {}
}
OPS.syntax_check = function (a) {
    var st = cmdState(9);
    if (!st || !st.enabled) throw new Error("команда «Синтаксическая проверка» недоступна");
    synCapture = { messages: [], boxes: [], refused: [] };
    var sent = false, t0 = new Date().getTime();
    try {
        events.connect(windows, "onMessage", SelfScript.self, "onSynMsg");
        events.connect(windows, "onMessageBox", SelfScript.self, "onSynBox");
        sent = sendCommand(CFG_GROUP, 9);
    } finally {
        try { events.disconnect(windows, "onMessage", SelfScript.self, "onSynMsg"); } catch (e) {}
        try { events.disconnect(windows, "onMessageBox", SelfScript.self, "onSynBox"); } catch (e) {}
    }
    var cap = synCapture; synCapture = null;
    var errs = [];
    for (var i = 0; i < cap.messages.length; i++) if (/ошибка|\{ошибка\}/i.test(cap.messages[i])) errs.push(cap.messages[i]);
    var res = { sent: sent, ms: new Date().getTime() - t0, messages: cap.messages,
        boxes: cap.boxes, errorLines: errs, hasErrors: errs.length > 0 };
    if (cap.refused.length) { res.refused = cap.refused; throw new Error("проверка прервана незнакомым окном: " + cap.refused.join(" | ")); }
    return res;
};

// Диагностика диалога команды: открыть модальное окно команды из белого списка,
// перечислить контролы, закрыть Отменой. Ничего не выполняет.
var PROBE_CMDS = {
    cfgstore: { group: STG_GROUP, nums: [150, 151, 152, 153] },
    config: { group: CFG_GROUP, nums: [9, 29] }
};
var probeCapture = null;
function onProbeModal(d) {
    try {
        if (!probeCapture) return;
        var ev = { stage: d.stage, caption: safeStr(d.caption) };
        if (d.stage == 1) {
            try { ev.controls = listControls(d.form); } catch (e) { ev.controlsErr = String(e.description); }
            d.cancel = true; d.result = 2; ev.action = "Отмена (проба)";
        }
        probeCapture.modals.push(ev);
    } catch (e) {}
}
function onProbeBox(m) {
    try { if (probeCapture) { probeCapture.boxes.push({ caption: safeStr(m.caption), text: safeStr(m.text), type: m.type }); m.cancel = true; m.result = 2; } } catch (e) {}
}
OPS.dialog_probe = function (a) {
    var pc = PROBE_CMDS[String(a.cmdGroup || "").toLowerCase()];
    if (!pc) throw new Error("неизвестная группа команды пробы: " + a.cmdGroup);
    if (!inArray(pc.nums, a.num)) throw new Error("номер команды не разрешён для пробы: " + a.num);
    var st = getCmdState(pc.group, a.num);
    probeCapture = { modals: [], boxes: [], state: st ? { enabled: st.enabled, checked: st.checked } : null };
    try {
        events.connect(windows, "onDoModal", SelfScript.self, "onProbeModal");
        events.connect(windows, "onMessageBox", SelfScript.self, "onProbeBox");
        if (st && st.enabled) probeCapture.sent = sendCommand(pc.group, a.num);
    } finally {
        try { events.disconnect(windows, "onDoModal", SelfScript.self, "onProbeModal"); } catch (e) {}
        try { events.disconnect(windows, "onMessageBox", SelfScript.self, "onProbeBox"); } catch (e) {}
    }
    var cap = probeCapture; probeCapture = null;
    return cap;
};

// Хранилище: состояние команд захвата/помещения у объекта (косвенный статус, план §4.3).
function stgState(num) {
    var s = getCmdState(STG_GROUP, num);
    return s ? { enabled: s.enabled, checked: s.checked, text: safeStr(s.text) } : null;
}
function stgAutoconnectLoaded() {
    try { return !!addins.byUniqueName("stg_autoconnect"); } catch (e) { return false; }
}
var stgCapture = null;
function onStgModal(d) {
    try {
        if (!stgCapture) return;
        var ev = { stage: d.stage, caption: safeStr(d.caption) };
        stgCapture.modals.push(ev);
        if (d.stage != 1) return;
        var cap = String(d.caption);
        if (stgCapture.mode == "capture" && /захват.*хранилище/i.test(cap)) {
            try { d.form.getControl("GetRecursive").value = !!stgCapture.recursive; ev.setRecursive = true; }
            catch (e) { ev.setRecursiveErr = String(e.description); }
            d.cancel = true; d.result = 1; ev.action = "ОК";
        } else if (stgCapture.mode == "commit" && /помещени.*хранилище/i.test(cap)) {
            // Поле комментария помещения — EnrollComment (снято dialog_probe-эквивалентом,
            // 24.10.2026); без комментария отказ (инвариант 5)
            var cands = ["EnrollComment", "Comment", "Комментарий", "Description"], set = false, names = [];
            for (var ci = 0; ci < d.form.controlsCount; ci++) {
                var c = null; try { c = d.form.getControl(ci); } catch (x) { continue; }
                var cn = safeStr(c && c.name); names.push(cn);
                if (!set && cn && inArray(cands, String(cn))) {
                    try { c.value = stgCapture.comment; set = safeStr(c.value) == String(stgCapture.comment); ev.commentSet = String(cn); } catch (x) { ev.commentErr = String(x.description); }
                }
            }
            ev.controls = names;
            if (!set) { d.cancel = true; d.result = 2; ev.action = "Отмена (поле комментария не найдено/не записано)"; stgCapture.refused.push("комментарий"); }
            else { d.cancel = true; d.result = 1; ev.action = "ОК"; }
        } else {
            d.cancel = true; d.result = 2; ev.action = "Отмена (незнакомый диалог)";
            stgCapture.refused.push(cap);
        }
    } catch (e) {}
}
function onStgBox(m) {
    try {
        if (!stgCapture) return;
        stgCapture.boxes.push({ caption: safeStr(m.caption), text: safeStr(m.text), type: m.type });
        m.cancel = true;
        m.result = ((m.type & 0x0F) == 0) ? 1 : 2;
    } catch (e) {}
}
function stgCommonChecks() {
    if (stgAutoconnectLoaded()) throw new Error("загружен stg_autoconnect.js — порядок обработчиков диалогов не гарантирован (инвариант 5)");
}

// Захват: активировать объект в дереве -> команда 151 -> диалог с проверкой заголовка
// (не «OK в любом окне», как в образце CfgStore.js:105-123 — план §4.3).
OPS.cfgstore_capture = function (a) {
    stgCommonChecks();
    var obj = resolve(a);
    if (String(obj.id) == String(container(a.container).rootObject.id)) throw new Error("захват корня конфигурации не поддерживается");
    obj.activateInTree();
    var before = { capture: stgState(151), store: stgState(152) };
    if (!before.capture || !before.capture.enabled) throw new Error("команда «Захватить в хранилище» недоступна (объект уже захвачен или нет хранилища)");
    stgCapture = { mode: "capture", recursive: !!a.recursive, modals: [], boxes: [], refused: [] };
    var sent = false, t0 = new Date().getTime();
    try {
        events.connect(windows, "onDoModal", SelfScript.self, "onStgModal");
        events.connect(windows, "onMessageBox", SelfScript.self, "onStgBox");
        sent = sendCommand(STG_GROUP, 151);
    } finally {
        try { events.disconnect(windows, "onDoModal", SelfScript.self, "onStgModal"); } catch (e) {}
        try { events.disconnect(windows, "onMessageBox", SelfScript.self, "onStgBox"); } catch (e) {}
    }
    var cap = stgCapture; stgCapture = null;
    var after = { capture: stgState(151), store: stgState(152) };
    var res = { sent: sent, ms: new Date().getTime() - t0, before: before, after: after,
        modals: cap.modals, boxes: cap.boxes };
    if (cap.refused.length) { res.refused = cap.refused; throw new Error("захват прерван незнакомым окном: " + cap.refused.join(" | ")); }
    // ВНИМАНИЕ: getCmdState(151/152) на 8.3.27.2074 обе команды держит доступными всегда —
    // косвенный постусловие по ним НЕ работает (проверено 24.10.2026). Факт захвата
    // проверяется помещением: его диалог перечисляет захваченные объекты.
    res.capturedIndirect = null;
    return res;
};

// Помещение: один объект за вызов (активировать -> команда 152), комментарий обязателен.
// Список объектов в диалоге (ReviseObjectList — грид) надёжно не записывается — поэтому
// только по одному (решение по итогам первого опыта этапа 5, план §5).
OPS.cfgstore_commit = function (a) {
    stgCommonChecks();
    var comment = String(a.comment || "");
    if (!comment || !comment.length) throw new Error("комментарий обязателен (инвариант 5)");
    var obj = resolve(a);
    obj.activateInTree();
    var before = { capture: stgState(151), store: stgState(152) };
    if (!before.store || !before.store.enabled) throw new Error("команда «Поместить в хранилище» недоступна (нет захваченных объектов или нет хранилища)");
    stgCapture = { mode: "commit", comment: comment, modals: [], boxes: [], refused: [] };
    var sent = false, t0 = new Date().getTime();
    try {
        events.connect(windows, "onDoModal", SelfScript.self, "onStgModal");
        events.connect(windows, "onMessageBox", SelfScript.self, "onStgBox");
        sent = sendCommand(STG_GROUP, 152);
    } finally {
        try { events.disconnect(windows, "onDoModal", SelfScript.self, "onStgModal"); } catch (e) {}
        try { events.disconnect(windows, "onMessageBox", SelfScript.self, "onStgBox"); } catch (e) {}
    }
    var cap = stgCapture; stgCapture = null;
    var after = { capture: stgState(151), store: stgState(152) };
    var res = { sent: sent, ms: new Date().getTime() - t0, before: before, after: after,
        modals: cap.modals, boxes: cap.boxes };
    if (cap.refused.length) { res.refused = cap.refused; throw new Error("помещение прервано: " + cap.refused.join(" | ")); }
    return res;
};

// Обновление конфигурации БД (Config, 29) — только для own_bases (инвариант 5):
// нормализованное сравнение строки соединения, разрешённые диалоги — по подстроке заголовка,
// всё незнакомое закрывается Отменой и даёт отказ с заголовком (инвариант 6).
var updCapture = null;
function matchAllowed(caption, allowed) {
    var c = String(caption).toLowerCase();
    for (var i = 0; i < allowed.length; i++) if (c.indexOf(String(allowed[i]).toLowerCase()) >= 0) return true;
    return false;
}
function onUpdModal(d) {
    try {
        if (!updCapture) return;
        var ev = { stage: d.stage, caption: safeStr(d.caption) };
        updCapture.modals.push(ev);
        if (d.stage != 1) return;
        if (matchAllowed(d.caption, updCapture.allowed)) { d.cancel = true; d.result = 1; ev.action = "ОК (разрешённый диалог)"; }
        else { d.cancel = true; d.result = 2; ev.action = "Отмена (незнакомый диалог)"; updCapture.refused.push(safeStr(d.caption)); }
    } catch (e) {}
}
function onUpdBox(m) {
    try {
        if (!updCapture) return;
        var ev = { caption: safeStr(m.caption), text: safeStr(m.text), type: m.type };
        updCapture.boxes.push(ev);
        m.cancel = true;
        var t = m.type & 0x0F;
        if (t == 0) { m.result = 1; ev.action = "ОК"; }
        else if (matchAllowed(m.caption, updCapture.allowed)) { m.result = 1; ev.action = "ОК (разрешённый)"; }
        else { m.result = 2; ev.action = "Отмена"; updCapture.refused.push(safeStr(m.caption) + ": " + safeStr(m.text)); }
    } catch (e) {}
}
OPS.update_db_cfg = function (a) {
    var own = a.own_bases || [];
    if (!own.length) throw new Error("список own_bases пуст — обновление конфигурации БД запрещено");
    var mine = normConn(СтрокаСоединенияИнформационнойБазы());
    var matched = false;
    for (var i = 0; i < own.length; i++) if (normConn(String(own[i])) == mine) matched = true;
    if (!matched) throw new Error("база не входит в own_bases (" + mine + ") — отказ");
    var st = cmdState(29);
    if (!st || !st.enabled) throw new Error("команда «Обновить конфигурацию БД» недоступна");
    updCapture = { allowed: a.allowedDialogs || ["реорганизация информации", "принять"], modals: [], boxes: [], refused: [] };
    var sent = false, t0 = new Date().getTime();
    try {
        events.connect(windows, "onDoModal", SelfScript.self, "onUpdModal");
        events.connect(windows, "onMessageBox", SelfScript.self, "onUpdBox");
        sent = sendCommand(CFG_GROUP, 29);
    } finally {
        try { events.disconnect(windows, "onDoModal", SelfScript.self, "onUpdModal"); } catch (e) {}
        try { events.disconnect(windows, "onMessageBox", SelfScript.self, "onUpdBox"); } catch (e) {}
    }
    var cap = updCapture; updCapture = null;
    var res = { sent: sent, ms: new Date().getTime() - t0, modals: cap.modals, boxes: cap.boxes };
    if (cap.refused.length) { res.refused = cap.refused; throw new Error("обновление прервано незнакомым окном: " + cap.refused.join(" | ")); }
    return res;
};

// ---------- представления (диагностика) ----------

function viewInfo(v, path) {
    var r = { path: path };
    try { r.title = safeStr(v.title); } catch (e) {}
    try { r.cls = safeStr(v.wndClass); } catch (e) {}
    try { r.id = safeStr(v.id); } catch (e) {}
    try { r.isContainer = v.isContainer; } catch (e) {}
    return r;
}
function walkViews(v, path, out, depth) {
    out.push(viewInfo(v, path));
    if (depth > 6) return;
    var lst = null;
    try { lst = v.enumChilds(); } catch (e) { return; }
    if (!lst) return;
    for (var i = 0; i < lst.count; i++) walkViews(lst.item(i), path + "/" + i, out, depth + 1);
}
OPS.view_list = function (a) { var out = []; walkViews(windows.mainView, "0", out, 0); return out; };
OPS.active_view = function (a) { var v = windows.getActiveView(); return v ? viewInfo(v, "?") : null; };

// ---------- трасса (диагностика) ----------

var trace = null;
function traceMsg(p) { try { if (trace) trace.messages.push(String(p.text)); } catch (e) {} }
function traceMd(e) {
    try {
        if (!trace) return;
        var r = { kind: e.kind, request: e.request };
        try { if (e.obj) r.obj = { name: String(e.obj.name), id: String(e.obj.id), cls: String(e.obj.mdclass.name(1)) }; } catch (x) { r.objErr = x.description; }
        try { if (e.parentObj) r.parent = { name: String(e.parentObj.name), id: String(e.parentObj.id) }; } catch (x) { r.parentErr = x.description; }
        trace.md.push(r);
    } catch (x) {}
}
function traceModal(d) { try { if (trace) trace.modals.push({ stage: d.stage, caption: safeStr(d.caption) }); } catch (e) {} }
function traceBox(m) { try { if (trace) trace.boxes.push({ caption: safeStr(m.caption), text: safeStr(m.text), type: m.type }); } catch (e) {} }
OPS.trace_start = function (a) {
    if (trace) throw new Error("трасса уже идёт");
    trace = { messages: [], md: [], modals: [], boxes: [], startedAt: new Date().getTime() };
    events.connect(windows, "onMessage", SelfScript.self, "traceMsg");
    events.connect(metadata, "MetaDataEvent", SelfScript.self, "traceMd");
    events.connect(windows, "onDoModal", SelfScript.self, "traceModal");
    events.connect(windows, "onMessageBox", SelfScript.self, "traceBox");
    setCmdTrace(true);
    return "трасса включена";
};
OPS.trace_stop = function (a) {
    setCmdTrace(false);
    try { events.disconnect(windows, "onMessage", SelfScript.self, "traceMsg"); } catch (e) {}
    try { events.disconnect(metadata, "MetaDataEvent", SelfScript.self, "traceMd"); } catch (e) {}
    try { events.disconnect(windows, "onDoModal", SelfScript.self, "traceModal"); } catch (e) {}
    try { events.disconnect(windows, "onMessageBox", SelfScript.self, "traceBox"); } catch (e) {}
    var t = trace; trace = null;
    return t;
};

// ---------- служебное ----------

// Состояние запроса по id: done (кэш) | running | queued | unknown.
OPS.wait = function (a) {
    var id = String(a.id || "");
    if (!/^[A-Za-z0-9._-]+$/.test(id)) throw new Error("недопустимый id: " + id);
    if (respCache.hasOwnProperty(id)) return { id: id, state: "done" };
    if (running && running.id == id) return { id: id, state: "running" };
    if (fso.FileExists(REQ_DIR + id + ".json")) return { id: id, state: "queued" };
    return { id: id, state: "unknown" };
};

OPS.timer_stats = function (a) { return stat; };

// Поднять окно конфигуратора ОТДЕЛЬНОЙ операцией: эффект AppActivate доходит
// очередью сообщений после тика, поэтому активация и нажатия — в разных тиках
// (сервер ждёт focusHwnd перед операциями дерева).
OPS.activate_self = function (a) {
    var had = winApi.getFocus();
    var ok = false;
    try {
        var sh = new ActiveXObject("WScript.Shell");
        if (!had) sh.SendKeys("%");
        ok = !!sh.AppActivate(myPid);
    } catch (e) { stat.lastError = "activate_self: " + e.description; }
    return { hadFocus: !!had, appActivate: ok, focus: winApi.getFocus() || 0 };
};

// Диагностика фокуса ввода: hwnd окна фокуса, модальность, активное представление.
// Нажатия клавиш (md_add_child/md_delete) идут в окно фокуса; 0 — окно конфигуратора
// в фоне, сервер должен активировать его (winutils.activate_window).
OPS.focus_probe = function (a) {
    var av = null; try { av = windows.getActiveView(); } catch (e) {}
    return { focusHwnd: winApi.getFocus() || 0, modalMode: windows.modalMode,
        activeTitle: av ? safeStr(av.title) : null };
};

// ---------- сессия ----------

function connString() {
    try { return СтрокаСоединенияИнформационнойБазы(); } catch (e) { return "?" + e.description; }
}
function platformVersion() {
    try { return v8New("СистемнаяИнформация").ВерсияПриложения; } catch (e) { return "?" + e.description; }
}
var sessionBase = { pid: myPid, conn: connString(), platform: platformVersion(), bridge: BRIDGE_VERSION };

function containersInfo() {
    var conts = [];
    for (var i = 0; i < metadata.openedCount; i++) {
        var c = null;
        try { c = metadata.getContainer(i); } catch (e) { conts.push({ index: i, error: String(e.description) }); continue; }
        var rec = { index: i, identifier: safeStr(c.identifier), isModified: !!c.isModified,
            root: c.rootObject ? String(c.rootObject.name) : null,
            rootId: c.rootObject ? String(c.rootObject.id) : null };
        try { rec.master = c.masterContainer ? safeStr(c.masterContainer.identifier) : null; } catch (e) { rec.master = null; }
        try { rec.isCurrent = !!(c.rootObject && metadata.current.rootObject && c.rootObject.isSame(metadata.current.rootObject)); } catch (e) { rec.isCurrent = false; }
        conts.push(rec);
    }
    return conts;
}

function writeSession() {
    try {
        var o = { pid: sessionBase.pid, conn: sessionBase.conn, platform: sessionBase.platform,
            bridge: BRIDGE_VERSION, heartbeat: new Date().getTime(), stat: stat,
            modal: windows.modalMode, containers: containersInfo() };
        if (running) o.running = running;
        writeUtf8Atomic(sessionFile, JSON.stringify(o));
    } catch (e) { stat.lastError = "writeSession: " + e.description; }
}

// ---------- цикл ----------

function cacheResponse(id, text) {
    respCache[id] = text;
    respCacheOrder.push(id);
    while (respCacheOrder.length > RESP_CACHE_MAX) {
        var old = respCacheOrder.shift();
        delete respCache[old];
    }
}

function nextRequest() {
    var e = new Enumerator(fso.GetFolder(REQ_DIR).Files);
    var best = null;
    for (; !e.atEnd(); e.moveNext()) {
        var f = e.item();
        if (!/\.json$/i.test(f.Name)) continue;
        if (best === null || f.Name < best.Name) best = f;
    }
    return best;
}

function whereCtx(op, args) {
    var w = { op: String(op) };
    if (args) {
        if (args.container !== undefined) w.container = args.container;
        if (args.path !== undefined) w.path = args.path;
        if (args.uuid !== undefined) w.uuid = args.uuid;
        if (args.ownerPath !== undefined) w.ownerPath = args.ownerPath;
        if (args.prop !== undefined) w.prop = args.prop;
    }
    return w;
}

function handle(file) {
    var id = file.Name.replace(/\.json$/i, "");
    // имя запроса — только безопасные символы (файл ответа пишется под этим именем)
    if (!/^[A-Za-z0-9._-]+$/.test(id)) {
        stat.errors++; stat.lastError = "плохое имя запроса: " + file.Name;
        try { fso.DeleteFile(file.Path, true); } catch (e) {}
        return;
    }
    var path = file.Path;
    var respPath = RESP_DIR + id + ".json";
    // идемпотентность: повтор id — прежний ответ, без выполнения.
    // Файл запроса удаляется и здесь, иначе он обрабатывается каждый тик и блокирует
    // запросы, сортирующиеся после него (nextRequest берёт старейший)
    if (respCache.hasOwnProperty(id)) {
        try { fso.DeleteFile(path, true); } catch (e) {}
        writeUtf8Atomic(respPath, respCache[id]);
        stat.replayed++;
        return;
    }
    var resp = { id: id, ok: false, tick: stat.ticks };
    var t0 = new Date().getTime();
    var req = null;
    try {
        req = JSON.parse(readUtf8(path));
        try { fso.DeleteFile(path, true); } catch (eDel) {}
        var op = req && typeof req.op == "string" ? req.op : null;
        resp.where = whereCtx(op, req ? req.args : null);
        if (!op || !OPS.hasOwnProperty(op))
            resp.error = { code: "unknown_op", message: "неизвестная операция " + op, where: resp.where };
        else if (req.deadline && new Date().getTime() > req.deadline)
            resp.error = { code: "deadline", message: "истёк срок запроса", where: resp.where };
        else {
            // Трассировка текущей операции в реестре: при зависании/падении видно,
            // какая операция выполнялась (журнал движка буферизуется и обрывается раньше).
            running = { id: id, op: op, startedAt: t0 };
            writeSession();
            try {
                resp.result = OPS[op](req.args || {});
                resp.ok = true;
            } finally {
                if (running) { running = null; writeSession(); }
            }
        }
    } catch (e) {
        stat.errors++;
        resp.error = { code: "exception", message: String(e.description || e.message || e),
            where: resp.where || { op: req && req.op } };
        try { if (fso.FileExists(path)) fso.DeleteFile(path, true); } catch (e2) {}
    }
    resp.ms = new Date().getTime() - t0;
    var text;
    try { text = JSON.stringify(resp); }
    catch (e) { text = JSON.stringify({ id: id, ok: false, error: { code: "serialize", message: String(e.description) } }); }
    writeUtf8Atomic(respPath, text);
    cacheResponse(id, text);
    stat.done++;
}

function onTick(timerID) {
    try {
        stat.ticks++;
        if (busy) { stat.ticksBusy++; return; }
        if (windows.modalMode != 0) { stat.ticksInModal++; if (stat.ticksInModal % 20 == 1) writeSession(); return; }
        if (!fso.FileExists(ENABLE_FILE)) return;
        busy = true;
        try {
            var f = nextRequest();
            if (f) handle(f);
            if (stat.ticks % 10 == 0 || f) writeSession();
        } finally {
            busy = false;
        }
    } catch (e) {
        stat.errors++;
        stat.lastError = "tick: " + (e.description || e.message || e);
    }
}

if (fso.FileExists(ENABLE_FILE)) {
    if (!fso.FolderExists(SESSION_DIR)) fso.CreateFolder(SESSION_DIR);
    if (!fso.FolderExists(REQ_DIR)) fso.CreateFolder(REQ_DIR);
    if (!fso.FolderExists(RESP_DIR)) fso.CreateFolder(RESP_DIR);
    writeSession();
    timerId = createTimer(TIMER_MS, SelfScript.self, "onTick");
}

// Режим снятия модальных диалогов (флаг mcp_dump_modals.on рядом с mcp_bridge.on):
// обработчик исполняется из простоя ДАЖЕ при открытом модальном окне (спайк, опыт 5),
// поэтому снимает caption и контролы любого диалога — и команд моста, и чужих
// (в т.ч. штатных мастеров пакетных операций, где тики заблокированы инвариантом 2).
// Только запись, ничего не закрывает и не правит.
var dumpModalsOn = false;
function onDumpModal(d) {
    try {
        if (!dumpModalsOn || d.stage != 1) return;
        var rec = { caption: safeStr(d.caption), controls: null, err: null };
        try { rec.controls = listControls(d.form); } catch (e) { rec.err = String(e.description); }
        writeUtf8Atomic(SESSION_DIR + "modals.jsonl", readModalsLog() + JSON.stringify(rec) + "\n");
    } catch (e) {}
}
function readModalsLog() {
    try { return readUtf8(SESSION_DIR + "modals.jsonl"); } catch (e) { return ""; }
}

// Универсальный мастер-раннер (флаг mcp_stg_wizard.json): автоматизация штатных
// модальных диалогов по шагам {click/set/clickIndex/report}, как конструктор форм
// этапа 7. Формат: {"dialogs": [{"caption": "...", "steps": [...]}, ...]}.
// Нужен для мастеров, которые пакетная команда открывает сама (например
// /ConfigurationRepositoryCreate — командные ключи его поля не заполняют,
// проверено 24.10.2026). Все действия и значения — только из флаг-файла.
var wizardJob = null;
function loadWizardJob() {
    try {
        if (!fso.FileExists(ROOT + "mcp_stg_wizard.json")) return null;
        var j = JSON.parse(readUtf8(ROOT + "mcp_stg_wizard.json"));
        if (j.dialogs) return j;
        if (j.caption) return { dialogs: [j] };  // старый формат — один диалог
        return null;
    } catch (e) { stat.lastError = "wizard.json: " + e.description; return null; }
}
function reportControl(form, name, out) {
    try { out.push(name + "=" + safeStr(form.getControl(name).value)); }
    catch (e) { out.push(name + ": " + e.description); }
}
function reportAllControls(form, out) {
    try {
        for (var i = 0; i < form.controlsCount; i++) {
            var c = form.getControl(i);
            var nm = safeStr(c.name);
            if (nm) out.push(nm + "=" + safeStr(c.value));
        }
    } catch (e) { out.push("reportAll: " + e.description); }
}
function onWizardModal(d) {
    try {
        if (d.stage != 1) return;
        // флаг перечитывается на каждом диалоге: правки флаг-файла действуют
        // и в уже запущенном конфигураторе
        wizardJob = loadWizardJob();
        if (!wizardJob) return;
        var job = null;
        for (var dj = 0; dj < wizardJob.dialogs.length; dj++)
            if (String(wizardJob.dialogs[dj].caption) == String(d.caption)) { job = wizardJob.dialogs[dj]; break; }
        if (!job) return;
        var log = { caption: safeStr(d.caption), steps: [], done: false };
        try {
            var steps = job.steps || [];
            for (var si = 0; si < steps.length; si++) {
                var st = steps[si], out = [];
                try {
                    if (st.set) for (var sn in st.set) if (st.set.hasOwnProperty(sn)) {
                        d.form.getControl(sn).value = st.set[sn];
                        reportControl(d.form, sn, out);
                    }
                    if (st.click) {
                        d.form.sendEvent(d.form.getControl(st.click).id, 0);
                        out.push("нажата " + st.click);
                    }
                    // у message box имена контролов пустые — клик по индексу
                    if (st.clickIndex !== undefined) {
                        d.form.sendEvent(d.form.getControl(st.clickIndex).id, 0);
                        out.push("нажат контрол[" + st.clickIndex + "]");
                    }
                    if (st.report) for (var ri = 0; ri < st.report.length; ri++)
                        reportControl(d.form, st.report[ri], out);
                    if (st.reportAll) reportAllControls(d.form, out);
                } catch (e) { out.push("ошибка шага " + si + ": " + e.description); }
                log.steps.push({ step: si, out: out });
            }
            log.done = true;
        } catch (e) { log.err = String(e.description); }
        writeUtf8Atomic(SESSION_DIR + "wizard.log", readModalsWizardLog() + JSON.stringify(log) + "\n");
    } catch (e) {}
}
function readModalsWizardLog() {
    try { return readUtf8(SESSION_DIR + "wizard.log"); } catch (e) { return ""; }
}
function setDumpModals() {
    try {
        var want = fso.FileExists(ROOT + "mcp_dump_modals.on");
        if (want && !dumpModalsOn) {
            events.connect(windows, "onDoModal", SelfScript.self, "onDumpModal");
            dumpModalsOn = true;
        } else if (!want && dumpModalsOn) {
            events.disconnect(windows, "onDoModal", SelfScript.self, "onDumpModal");
            dumpModalsOn = false;
        }
        wizardJob = loadWizardJob();
        if (wizardJob) {
            try { events.disconnect(windows, "onDoModal", SelfScript.self, "onWizardModal"); } catch (e) {}
            events.connect(windows, "onDoModal", SelfScript.self, "onWizardModal");
        }
    } catch (e) { stat.lastError = "dumpModals: " + e.description; }
}
setDumpModals();

function macrosСтатусМоста() {
    Message("mcp_bridge " + BRIDGE_VERSION + ": pid " + myPid + ", тиков " + stat.ticks + ", в модальном " + stat.ticksInModal + ", при busy " + stat.ticksBusy + ", выполнено " + stat.done + ", повторов " + stat.replayed + ", ошибок " + stat.errors + (stat.lastError ? ", последняя: " + stat.lastError : ""));
}
