# -*- coding: utf-8 -*-
"""Настройки журнала регистрации ИБ через V83.COMConnector (внешнее соединение).

Пробел 2 отчёта 08.11.2026: журнал регистрации — НЕ метаданные конфигурации,
через SnegAPI (rootObject.mdclass) недоступен в принципе; управление — штатные
методы встроенного языка, доступные во внешнем соединении (Сервер/толстый
клиент/внешнее соединение):
  Получить/УстановитьИспользованиеСобытияЖурналаРегистрации (8.2+),
  Получить/УстановитьПериодРазделенияХраненияДанныхЖурналаРегистрации (8.3.20+),
  ВыгрузитьЖурналРегистрации (8.0+, приёмник = строка-путь XML).

Требования: платформа 1С 64-bit с зарегистрированным V83.COMConnector
(comcntr.dll), pywin32. Если typelib comcntr не зарегистрирован (частая
ситуация на машинах с несколькими версиями платформы) — выполняется
per-user регистрация oleaut32.RegisterTypeLibForUser (без прав администратора).
Для файловой ИБ нужен монопольный доступ: закройте конфигуратор этой базы.
"""
import argparse
import ctypes
import json
import os
import sys
from ctypes import byref, c_void_p, c_wchar_p

COM_PROGID = 'V83.COMConnector'
# Libid typelib comcntr; 1С регистрирует его как версию 1.0 (метаданные dll
# объявляют 0.3 — LoadRegTypeLib с 0.3 не находит даже после регистрации).
TYPELIB_ID = '{98AC3B5B-5323-418F-8F07-E32F231D2393}'
TYPELIB_VERSIONS = ((1, 0), (0, 3))

# Полный набор имён событий (проверен на 8.3.27: 68/68 валидны). Имена вида
# "<группа>.<событие>" из документации Глобальный контекст.
KNOWN_EVENTS = [
    '_$Access$_.Access', '_$Access$_.AccessDenied',
    '_$Data$_.DataHistorySettingsUpdate', '_$Data$_.Delete',
    '_$Data$_.DeletePredefinedData', '_$Data$_.DeleteVersions', '_$Data$_.New',
    '_$Data$_.NewPredefinedData', '_$Data$_.NewVersion', '_$Data$_.Post',
    '_$Data$_.PredefinedDataInitialization',
    '_$Data$_.PredefinedDataInitializationDataNotFound',
    '_$Data$_.SetPredefinedDataInitialization',
    '_$Data$_.SetStandardODataInterfaceContent',
    '_$Data$_.TotalsMaxPeriodUpdate', '_$Data$_.TotalsMinPeriodUpdate',
    '_$Data$_.Unpost', '_$Data$_.Update', '_$Data$_.UpdatePredefinedData',
    '_$Data$_.VersionCommentUpdate',
    '_$Debug$_.DebugTargetAttach', '_$Debug$_.DebugTargetDetach',
    '_$Debug$_.ExpressionEvaluation',
    '_$InfoBase$_.AdditionalAuthenticationSettingsUpdate',
    '_$InfoBase$_.AdditionalAuthenticationSettingsUpdateError',
    '_$InfoBase$_.AdministrationParametersChange',
    '_$InfoBase$_.ConfigExtensionUpdate', '_$InfoBase$_.ConfigUpdate',
    '_$InfoBase$_.ConfigUpdateError',
    '_$InfoBase$_.DBConfigBackgroundUpdateCancel',
    '_$InfoBase$_.DBConfigBackgroundUpdateError',
    '_$InfoBase$_.DBConfigBackgroundUpdateFinish',
    '_$InfoBase$_.DBConfigBackgroundUpdateResume',
    '_$InfoBase$_.DBConfigBackgroundUpdateStart',
    '_$InfoBase$_.DBConfigBackgroundUpdateSuspend',
    '_$InfoBase$_.DBConfigExtensionDelete',
    '_$InfoBase$_.DBConfigExtensionDeleteError',
    '_$InfoBase$_.DBConfigExtensionInsert',
    '_$InfoBase$_.DBConfigExtensionInsertError',
    '_$InfoBase$_.DBConfigExtensionUpdate',
    '_$InfoBase$_.DBConfigUpdate',
    '_$InfoBase$_.EventLogSettingsUpdate',
    '_$InfoBase$_.InfoBaseLanguageUpdate',
    '_$InfoBase$_.MasterNodeInfoBaseConfigUpdate',
    '_$InfoBase$_.RegionalSettingsUpdate',
    '_$InfoBase$_.SecurityProfileChange',
    '_$InfoBase$_.SessionStandardODataInterfaceUpdate',
    '_$InfoBase$_.SpecialOfferUpdate',
    '_$InfoBase$_.UpdatePredefinedData',
    '_$InfoBase$_.UserLanguageUpdate',
    '_$InfoBase$_.UserPasswordUpdate',
    '_$InfoBase$_.UsersUpdate',
    '_$Job$_.Fail', '_$Job$_.FailScheduledJobs', '_$Job$_.Schedule',
    '_$Session$_.Authentication', '_$Session$_.AuthenticationError',
    '_$Session$_.Connection', '_$Session$_.ConnectionError',
    '_$Session$_.DataSeparation', '_$Session$_.Disconnection',
    '_$Session$_.FunctionalOption', '_$Session$_.Language',
    '_$Session$_.Start', '_$Session$_.Finish',
    '_$User$_.New', '_$User$_.Delete', '_$User$_.Update',
]

SPLIT_PERIOD_NAMES = ['Год', 'День', 'Квартал', 'Месяц', 'Неделя',
                      'НеИспользовать', 'Час']


class EventLogError(RuntimeError):
    pass


def comcntr_dll_path():
    """Путь к comcntr.dll из регистрации V83.COMConnector (HKCR->HKCU/HKLM)."""
    import winreg  # lazy: winapi только Windows (юнит-тесты и linux-CI без него)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, 'Software' + '\\' + 'Classes' + '\\' +
                                COM_PROGID + '\\' + 'CLSID') as k:
                clsid = winreg.QueryValueEx(k, '')[0]
            with winreg.OpenKey(hive, 'Software' + '\\' + 'Classes' + '\\' +
                                'CLSID' + '\\' + clsid + '\\' + 'InprocServer32') as k:
                return winreg.QueryValueEx(k, '')[0]
        except OSError:
            continue
    raise EventLogError('%s не зарегистрирован (запустите regsvr32 comcntr.dll)'
                        % COM_PROGID)


def ensure_typelib():
    """Гарантировать зарегистрированный typelib comcntr (per-user, без админа).

    Возвращает True, если была выполнена регистрация (исправление), False —
    если typelib уже был зарегистрирован.
    """
    try:
        import pythoncom
        import pywintypes
    except ImportError:
        raise EventLogError('нет pywin32 (pythoncom): pip install pywin32')

    libid = pywintypes.IID(TYPELIB_ID)
    for major, minor in TYPELIB_VERSIONS:
        try:
            pythoncom.LoadRegTypeLib(libid, major, minor, 0)
            return False
        except Exception:
            continue
    dll = comcntr_dll_path()
    oleaut = ctypes.OleDLL('oleaut32')
    ptlb = c_void_p()
    oleaut.LoadTypeLibEx(c_wchar_p(dll), 2, byref(ptlb))  # REGKIND_NONE
    if not ptlb.value:
        raise EventLogError('LoadTypeLibEx не вернул ITypeLib: %s' % dll)
    oleaut.RegisterTypeLibForUser(ptlb, c_wchar_p(dll),
                                  c_wchar_p(os.path.dirname(dll)))
    for major, minor in TYPELIB_VERSIONS:
        try:
            pythoncom.LoadRegTypeLib(libid, major, minor, 0)
            return True
        except Exception:
            continue
    raise EventLogError('typelib comcntr не зарегистрировался per-user '
                        '(нужен regsvr32 comcntr.dll от администратора)')


def _connect(base=None, conn_str=None):
    if not conn_str:
        if not base:
            raise EventLogError('нужен base (путь файловой ИБ) или conn_str')
        base = os.path.abspath(base)
        if not os.path.isfile(os.path.join(base, '1Cv8.1CD')):
            raise EventLogError('файловая ИБ не найдена: %s' % base)
        conn_str = 'File="%s";' % base
    ensure_typelib()
    try:
        import win32com.client
    except ImportError:
        raise EventLogError('нет pywin32 (win32com): pip install pywin32')
    try:
        cn = win32com.client.Dispatch(COM_PROGID)
        return cn.Connect(conn_str)
    except Exception as e:
        raise EventLogError('COMConnect: %s (база занята конфигуратором? '
                            'нужен монопольный доступ)' % str(e)[:200])


def _split_period_name(conn, value):
    try:
        mgr = getattr(conn, 'ПериодРазделенияХраненияДанныхЖурналаРегистрации')
        for name in SPLIT_PERIOD_NAMES:
            if getattr(mgr, name) == value:
                return name
    except Exception:
        pass
    return None


def cmd_get(base=None, conn_str=None):
    """Текущие настройки: использование всех известных событий + период."""
    conn = _connect(base, conn_str)
    events = {}
    for name in KNOWN_EVENTS:
        try:
            u = conn.ПолучитьИспользованиеСобытияЖурналаРегистрации(name)
            events[name] = bool(u.Использование)
        except Exception:
            events[name] = None  # имя не поддержано этой версией платформы
    split = None
    try:
        split = _split_period_name(
            conn, conn.ПолучитьПериодРазделенияХраненияДанныхЖурналаРегистрации())
    except Exception:
        pass  # 8.3.20-: метода нет
    return {'events': events, 'split_period': split}


def cmd_set(base, events, use):
    """Включить/выключить перечисленные события (или 'all')."""
    if events in ('all', ['all']):
        events = [n for n in KNOWN_EVENTS]
    for name in events:
        if name not in KNOWN_EVENTS:
            raise EventLogError('неизвестное событие %s (см. KNOWN_EVENTS)'
                                % name)
    conn = _connect(base)
    use_obj = conn.NewObject('ИспользованиеСобытияЖурналаРегистрации',
                             bool(use))
    done, failed = [], []
    for name in events:
        try:
            conn.УстановитьИспользованиеСобытияЖурналаРегистрации(
                name, use_obj)
            done.append(name)
        except Exception as e:
            failed.append({'event': name, 'error': str(e)[:150]})
    # постусловие: перечитать
    after = {}
    for name in done:
        u = conn.ПолучитьИспользованиеСобытияЖурналаРегистрации(name)
        after[name] = bool(u.Использование)
    return {'use': bool(use), 'done': done, 'failed': failed, 'after': after}


def cmd_split(base, period):
    """Установить период разделения хранения журнала (требует монопольный
    доступ; только последовательный формат журнала; 8.3.20+)."""
    if period not in SPLIT_PERIOD_NAMES:
        raise EventLogError('период: один из %s' % ', '.join(SPLIT_PERIOD_NAMES))
    conn = _connect(base)
    mgr = getattr(conn, 'ПериодРазделенияХраненияДанныхЖурналаРегистрации')
    value = getattr(mgr, period)
    conn.УстановитьПериодРазделенияХраненияДанныхЖурналаРегистрации(value)
    after = conn.ПолучитьПериодРазделенияХраненияДанныхЖурналаРегистрации()
    return {'before': period, 'after': _split_period_name(conn, after)}


def cmd_dump(base, out_file, start_date=None, end_date=None):
    """Выгрузить журнал регистрации в XML (v8e:EventLog)."""
    import datetime
    conn = _connect(base)
    out_file = os.path.abspath(out_file)
    if start_date or end_date:
        flt = conn.NewObject('Структура')
        for key, val in (('ДатаНачала', start_date), ('ДатаОкончания', end_date)):
            if val:
                d = datetime.datetime.strptime(val, '%Y-%m-%d')
                flt.Вставить(key, d)
        conn.ВыгрузитьЖурналРегистрации(out_file, flt)
    else:
        conn.ВыгрузитьЖурналРегистрации(out_file)
    size = os.path.getsize(out_file)
    return {'out_file': out_file, 'bytes': size}


def main(argv=None):
    # русская Windows: консоль по умолчанию cp866 — форсируем UTF-8,
    # иначе JSON с кириллицей и сообщения идут в перекодировке
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description='Настройки журнала регистрации ИБ через внешнее соединение')
    ap.add_argument('--base', help='путь файловой ИБ')
    ap.add_argument('--conn', help='готовая строка соединения (вместо --base)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('get', help='прочитать настройки')
    p_set = sub.add_parser('set', help='включить/выключить события')
    p_set.add_argument('events', nargs='+',
                       help='имена событий или "all"')
    p_set.add_argument('--on', action='store_true', help='включить (по умолчанию)')
    p_set.add_argument('--off', action='store_true', help='выключить')
    p_split = sub.add_parser('split', help='период разделения хранения')
    p_split.add_argument('period', help='Год|Месяц|День|Неделя|Час|Квартал|НеИспользовать')
    p_dump = sub.add_parser('dump', help='выгрузить журнал в XML')
    p_dump.add_argument('out_file')
    p_dump.add_argument('--start', help='дата начала YYYY-MM-DD')
    p_dump.add_argument('--end', help='дата конца YYYY-MM-DD')
    args = ap.parse_args(argv)

    try:
        if args.cmd == 'get':
            res = cmd_get(args.base, args.conn)
        elif args.cmd == 'set':
            use = not args.off
            res = cmd_set(args.base, args.events, use)
        elif args.cmd == 'split':
            res = cmd_split(args.base, args.period)
        else:
            res = cmd_dump(args.base, args.out_file, args.start, args.end)
    except EventLogError as e:
        print('eventlog: %s' % e, file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
