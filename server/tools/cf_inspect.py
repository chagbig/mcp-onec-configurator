# -*- coding: utf-8 -*-
"""Чтение контейнера cf/epf (формат v8, версия 2) без платформы 1С.

Формат снят эмпирически 27.10.2026 на mcp-20261027-231440.cf (проба П1 этапа 8),
сверен с описанием в скилле epf-dump:

  Внешний контейнер:
    заголовок 16 байт: ff ff ff 7f | 00 02 | 00 00 | 05 00 00 00 | 00 00 00 00
    блок: "\r\n<8hex data_size> <8hex page_size> <8hex next> \r\n" (31 байт) + данные
      на диске блок занимает page_size байт (хвост — нули), логическая длина = data_size;
      данные — либо raw deflate (zlib, wbits=-15), либо дословные
  Вложенный контейнер (хранилище объектов) идёт сразу после последней страницы внешнего:
    заголовок 19 байт: ff×8 | 00 02 | 00 00 | 0b 00 00 00 | 00 00 00, далее блоки
    с 16-значными hex-полями (64-бит) и маркером конца ffffffffffffffff
  Запись: заголовок 96/38/32/40 байт (raw): 16 байт (uuid+метка) + нули + имя UTF-16LE
    (null-терминированное; "root", "version", "versions", "<guid>", "<guid>.0", "deleted");
    затем отдельным блоком данные записи (deflate, UTF-8 с BOM).

Оглавление (TOC) не используется — записи перечисляются последовательным обходом
блоков: raw-блок с именем на смещении >=16 открывает запись, следующий блок — данные.

Ограничение чтения: имя берётся как UTF-16LE до двойного нуля; имена длиннее 1023
символов не встречаются. Большие cf читаются смещениями (файл не грузится в память).
"""
import argparse
import json
import re
import sys
import zlib

DES8 = re.compile(rb"\r\n([0-9a-fA-F]{8}) ([0-9a-fA-F]{8}) ([0-9a-fA-F]{8}) \r\n")
DES16 = re.compile(rb"\r\n([0-9a-fA-F]{16}) ([0-9a-fA-F]{16}) ([0-9a-fA-F]{16}) \r\n")
OUTER_MAGIC = b"\xff\xff\xff\x7f\x00\x02"
NESTED_MAGIC = b"\xff" * 8 + b"\x00\x02"
_NAME_RE = re.compile(r"^[0-9A-Za-z_.\-]{1,255}$")


def _find_nested_start(f, outer_end):
    """Ищет заголовок вложенного контейнера начиная с outer_end (первая позиция после
    последней страницы внешнего контейнера). Возвращает смещение данных после заголовка
    либо None."""
    f.seek(outer_end)
    head = f.read(4096)
    i = head.find(NESTED_MAGIC)
    if i < 0:
        return None
    nl = head.find(b"\r\n", i)
    if nl < 0:
        return None
    return outer_end + nl  # позиция "\r\n" перед строкой дескриптора


class Container:
    """Обход одного контейнера (внешнего или вложенного) по смещениям файла."""

    def __init__(self, f, start):
        self.f = f
        self.pos = start  # смещение строки дескриптора текущего блока
        self.width = None  # 8 или 16 (hex-разрядность полей), определяется по первому блоку

    def _read(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def _next_block(self, peek=656):
        """Читает дескриптор на self.pos (+ peek байт данных за одним заходом);
        None = конец контейнера."""
        buf = self._read(self.pos, 4 + 16 * 3 + 3 + peek)
        if buf[:2] != b"\r\n":
            return None
        if self.width == 16:
            m = DES16.match(buf)
        elif self.width == 8:
            m = DES8.match(buf)
        else:
            m = DES8.match(buf)
            if not m:
                m = DES16.match(buf)
        if not m:
            return None
        self.width = 16 if m.re is DES16 else 8
        size, page = int(m.group(1), 16), int(m.group(2), 16)
        data_off = self.pos + m.end() - m.start()  # длина совпадает со срезом
        blk = {"desc_off": self.pos, "data_off": data_off, "size": size, "page": page,
               "peek": buf[m.end() - m.start():m.end() - m.start() + peek]}
        self.pos = data_off + page
        return blk

    def walk(self, limit=None):
        """Последовательные блоки до конца контейнера."""
        n = 0
        while True:
            blk = self._next_block()
            if blk is None:
                return
            yield blk
            n += 1
            if limit is not None and n >= limit:
                return

    def payload(self, blk, try_deflate=True):
        """Данные блока: на диске page байт (чтение по смещению), логическая длина size."""
        raw = self._read(blk["data_off"], blk["page"])
        data = raw[: blk["size"]]
        if try_deflate:
            try:
                return zlib.decompress(raw, -15), True
            except zlib.error:
                pass
        return data, False


def _header_name(payload):
    """Имя из raw-заголовка записи: 16 байт служебных, затем нули и имя UTF-16LE
    (null-терминированное). None, если это не заголовок записи.
    Имена записей cf/epf — ASCII (guid, "root", "version.0" и т.п.), что и требуем."""
    if len(payload) < 22:
        return None
    i = 16
    while i < len(payload) and payload[i] == 0:
        i += 1
    if i >= len(payload) - 1:
        return None
    chars = []
    j = i
    while j + 1 < len(payload):
        ch = payload[j] | (payload[j + 1] << 8)
        if ch == 0:
            break
        chars.append(chr(ch))
        j += 2
    name = "".join(chars)
    if not name or not _NAME_RE.match(name):
        return None
    return name


class CfFile:
    """cf-контейнер: внешние записи + вложенное хранилище объектов."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        magic = self.f.read(16)
        if magic[:6] != OUTER_MAGIC:
            raise ValueError("не cf-контейнер v2: %r" % magic[:16])
        # первый блок внешнего контейнера — сразу после 16-байтного заголовка
        self.outer = Container(self.f, 16)

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _scan(self, cont, level):
        """Обход контейнера: raw-блоки с валидным ASCII-именем — заголовки записей,
        следующие за ними блоки — данные; прочие raw-блоки (TOC, служебные) пропускаются.
        Данные без заголовка считаются корневой записью "/", если начинаются с "{2,".
        Полные данные в памяти не держатся — только первые peek байт каждого блока."""
        out = []
        pending_name = None
        pending_hdr = None
        root_emitted = False

        def flush_empty():
            nonlocal pending_name, pending_hdr
            if pending_name is not None:
                out.append(self._rec(pending_name, pending_hdr, None, level))
                pending_name = pending_hdr = None

        for blk in cont.walk():
            peek = blk["peek"]
            name = _header_name(peek)
            if name is not None:
                flush_empty()
                pending_name, pending_hdr = name, blk
                continue
            if pending_name is not None:
                out.append(self._rec(pending_name, pending_hdr, blk, level))
                pending_name = pending_hdr = None
                continue
            if not root_emitted and peek.lstrip(b"\xef\xbb\xbf\r\n ").startswith(b"{2,"):
                root_emitted = True
                out.append(self._rec("/", None, blk, level))
        flush_empty()
        return out

    def records(self, nested=True):
        """Все записи: [{name, data_off, size, page, level}].
        level 0 = внешний контейнер, 1 = вложенное хранилище."""
        out = self._scan(self.outer, 0)
        if not nested:
            return out
        start = _find_nested_start(self.f, self.outer.pos)
        if start is None:
            return out
        out.extend(self._scan(Container(self.f, start), 1))
        return out

    @staticmethod
    def _rec(name, hdr, data, level):
        return {
            "name": name,
            "level": level,
            "data_off": data["data_off"] if data else None,
            "size": data["size"] if data else 0,
            "page": data["page"] if data else 0,
        }

    def read(self, rec):
        """Данные записи (распакованные)."""
        if rec["data_off"] is None:
            return b""
        raw = self._seek_read(rec["data_off"], rec["page"])
        try:
            return zlib.decompress(raw, -15)
        except zlib.error:
            return raw[: rec["size"]]

    def _seek_read(self, off, n):
        self.f.seek(off)
        return self.f.read(n)


def _partial_text(cf, rec, nbytes=8192):
    """Первые байты распакованных данных записи (потоковое разжатие без полного
    чтения блока). Для больших записей возвращает только начало."""
    if rec["data_off"] is None:
        return b""
    raw = cf._seek_read(rec["data_off"], min(rec["page"], nbytes * 4))
    try:
        do = zlib.decompressobj(-15)
        return do.decompress(raw, nbytes)
    except zlib.error:
        return raw[:nbytes]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Чтение записей cf-контейнера без платформы")
    ap.add_argument("cf")
    ap.add_argument("--record", help="выдать данные записи с этим именем в stdout")
    ap.add_argument("--find", help="фильтр имён (подстрока, без учёта регистра)")
    ap.add_argument("--names", action="store_true", help="только список имён")
    ap.add_argument("--scan", help="частичный декомпресс данных записей и фильтр по подстроке")
    ap.add_argument("--limit", type=int, default=None, help="ограничение числа блоков (отладка)")
    args = ap.parse_args(argv)
    with CfFile(args.cf) as cf:
        recs = cf.records()
        if args.find:
            fl = args.find.lower()
            recs = [r for r in recs if fl in r["name"].lower()]
        if args.names:
            for r in recs:
                print("%d\t%s\t%s" % (r["level"], r["name"], r["size"]))
            return 0
        if args.scan:
            for r in recs:
                if r["data_off"] is None:
                    continue
                txt = _partial_text(cf, r)
                try:
                    s = txt.decode("utf-8-sig", "replace")
                except Exception:
                    continue
                if args.scan in s:
                    i = s.find(args.scan)
                    print("%s\t%d\t%s" % (r["name"], r["size"],
                                          s[max(0, i - 60):i + 120].replace("\r", " ").replace("\n", " ")))
            return 0
        if args.record:
            for r in recs:
                if r["name"] == args.record:
                    sys.stdout.buffer.write(cf.read(r))
                    return 0
            print("запись не найдена: %s" % args.record, file=sys.stderr)
            return 1
        print(json.dumps(
            [{k: v for k, v in r.items() if k != "payload"} for r in recs],
            ensure_ascii=False, indent=1))
        return 0


if __name__ == "__main__":
    sys.exit(main())
