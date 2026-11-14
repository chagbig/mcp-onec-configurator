# -*- coding: utf-8 -*-
r"""Дашборд журнала изменений метаданных (IDEAS №7).

Читает journal/<pid>.jsonl (пишет мост 1.1+, UTF-16 с BOM, append-JSONL —
тот же формат, что Toolkit.md_journal в mcp_snegopat/server.py) и строит
самодостаточный HTML-дашборд: сводка (human/agent, период, разбивка по
kindName), timeline в обратном хронологическом порядке и клиентские
фильтры (текстовый поиск, чекбоксы human/agent, select kindName).

Требования и ловушки:
- файл журнала UTF-16 с BOM; кодировка определяется по BOM (le/be),
  fallback — «utf-16» (порядок платформы);
- оборванные хвостовые строки (мост дописывает в середине записи)
  молча пропускаются — журнал диагностический;
- HTML без внешних зависимостей: инлайн CSS, vanilla JS, данные зашиты
  в <script> как JSON (с экранированием < > & против </script>);
- файл пишется в UTF-8 с BOM (utf-8-sig) — кириллица в заголовках.
"""
from __future__ import annotations

import argparse
import codecs
import html
import json
import os
import sys
from pathlib import Path

TITLE_DEFAULT = "Журнал изменений метаданных"


def load_entries(path: str) -> list[dict]:
    """Прочитать журнал bridge (UTF-16 JSONL) в список записей.

    Кодировка — по BOM (utf-16-le / utf-16-be), без BOM — fallback «utf-16».
    Пустые и битые (не-JSON) строки пропускаются. Результат отсортирован
    по ts по возрастанию."""
    raw = Path(path).read_bytes()
    if raw.startswith(codecs.BOM_UTF16_LE):
        enc = "utf-16-le"
    elif raw.startswith(codecs.BOM_UTF16_BE):
        enc = "utf-16-be"
    else:
        enc = "utf-16"
    text = raw.decode(enc, errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]  # остаток BOM при явном le/be-декодировании
    entries: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue  # оборванная хвостовая строка — журнал диагностический
        if isinstance(e, dict):
            entries.append(e)
    entries.sort(key=lambda e: str(e.get("ts") or ""))
    return entries


def _count_by(entries: list[dict], key: str) -> dict[str, int]:
    """Счётчик значений поля (пустое значение — «?»)."""
    out: dict[str, int] = {}
    for e in entries:
        k = str(e.get(key) or "?")
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def build_dashboard(entries: list[dict], title: str = TITLE_DEFAULT) -> str:
    """Собрать самодостаточный HTML-дашборд (инлайн CSS + vanilla JS).

    Строка предназначена для записи в utf-8-sig; <meta charset="utf-8">
    задаёт кодировку при открытии в браузере."""
    total = len(entries)
    human = sum(1 for e in entries if e.get("src") == "human")
    agent = sum(1 for e in entries if e.get("src") == "agent")
    other = total - human - agent
    kinds = _count_by(entries, "kindName")
    tss = [str(e.get("ts") or "") for e in entries if e.get("ts")]
    period = " — " if not tss else "%s … %s" % (min(tss), max(tss))

    # данные для клиентского рендера: только нужные поля, без циклов
    view = [{"ts": str(e.get("ts") or ""),
             "src": str(e.get("src") or "?"),
             "kindName": str(e.get("kindName") or "?"),
             "name": str((e.get("obj") or {}).get("name") or ""),
             "cls": str((e.get("obj") or {}).get("cls") or ""),
             "container": str(e.get("container") or ""),
             "request": str(e.get("request") or "")}
            for e in entries]
    data_json = (json.dumps(view, ensure_ascii=False)
                 .replace("&", "\\u0026").replace("<", "\\u003c")
                 .replace(">", "\\u003e"))

    kind_chips = " ".join(
        '<span class="chip">%s: <b>%d</b></span>' % (html.escape(k), v)
        for k, v in kinds.items()) or '<span class="chip">нет записей</span>'

    css = """
body{font-family:Consolas,'Segoe UI',monospace;background:#f4f5f7;color:#1f2328;margin:0;padding:24px}
h1{font-size:20px;margin:0 0 4px 0}
.sub{color:#57606a;font-size:12px;margin-bottom:16px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.card{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:10px 16px;min-width:110px}
.card .n{font-size:22px;font-weight:700}
.card .l{font-size:11px;color:#57606a;text-transform:uppercase}
.card.human .n{color:#0969da}.card.agent .n{color:#b45309}
.chips{margin-bottom:16px}.chip{display:inline-block;background:#fff;border:1px solid #d0d7de;border-radius:12px;padding:2px 10px;font-size:12px;margin:2px 4px 2px 0}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:10px 12px;margin-bottom:8px}
.filters input[type=text]{flex:1;min-width:200px;padding:5px 8px;border:1px solid #d0d7de;border-radius:6px;font:inherit}
.filters select{padding:5px 8px;border:1px solid #d0d7de;border-radius:6px;font:inherit}
.filters label{font-size:13px;cursor:pointer;user-select:none}
.badge{display:inline-block;border-radius:10px;padding:1px 9px;font-size:11px;font-weight:700;color:#fff}
.badge.human{background:#0969da}.badge.agent{background:#b45309}.badge.other{background:#6e7781}
.count{font-size:12px;color:#57606a;margin-bottom:8px}
.row{background:#fff;border:1px solid #e1e4e8;border-left:3px solid #d0d7de;border-radius:6px;padding:7px 12px;margin-bottom:6px;font-size:13px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.row .ts{color:#57606a;font-size:12px;white-space:nowrap}
.row .kind{font-weight:700;white-space:nowrap}
.row .obj{flex:1;min-width:200px}
.row .obj .cls{color:#57606a;font-size:11px}
.row .cont{color:#8b949e;font-size:11px;white-space:nowrap}
.row.agent{border-left-color:#b45309}.row.human{border-left-color:#0969da}
.empty{color:#57606a;font-style:italic;padding:12px}
"""

    js = """
var DATA = __DATA__;
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function render(){
  var q=document.getElementById('q').value.toLowerCase();
  var showH=document.getElementById('f-human').checked;
  var showA=document.getElementById('f-agent').checked;
  var kind=document.getElementById('f-kind').value;
  var list=document.getElementById('list');
  var parts=[],shown=0;
  for(var i=0;i<DATA.length;i++){
    var e=DATA[i];
    if(e.src==='human'&&!showH)continue;
    if(e.src==='agent'&&!showA)continue;
    if(kind&&e.kindName!==kind)continue;
    if(q&&(e.name+' '+e.cls+' '+e.kindName+' '+e.container).toLowerCase().indexOf(q)===-1)continue;
    shown++;
    var obj=e.name?esc(e.name)+(e.cls?' <span class="cls">('+esc(e.cls)+')</span>':''):'&mdash;';
    var req=e.request?' title="'+esc(e.request)+'"':'';
    parts.push('<div class="row '+esc(e.src)+'"'+req+'>'
      +'<span class="ts">'+esc(e.ts||'?')+'</span>'
      +'<span class="badge '+esc(e.src)+'">'+esc(e.src)+'</span>'
      +'<span class="kind">'+esc(e.kindName)+'</span>'
      +'<span class="obj">'+obj+'</span>'
      +'<span class="cont">'+esc(e.container||'')+'</span></div>');
  }
  list.innerHTML=parts.length?parts.join(''):'<div class="empty">ничего не найдено по фильтрам</div>';
  document.getElementById('count').textContent='показано '+shown+' из '+DATA.length;
}
window.addEventListener('DOMContentLoaded',function(){
  ['q','f-human','f-agent','f-kind'].forEach(function(id){
    document.getElementById(id).addEventListener(id==='q'?'input':'change',render);
  });
  render();
});
""".replace("__DATA__", data_json)

    return (
        '<!DOCTYPE html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        '<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n'
        '<h1>%s</h1>\n'
        '<div class="sub">period: %s &middot; источник: journal/&lt;pid&gt;.jsonl (мост 1.1+)</div>\n'
        '<div class="cards">'
        '<div class="card"><div class="n">%d</div><div class="l">всего записей</div></div>'
        '<div class="card human"><div class="n">%d</div><div class="l">human</div></div>'
        '<div class="card agent"><div class="n">%d</div><div class="l">agent</div></div>'
        '%s'
        '</div>\n'
        '<div class="chips">%s</div>\n'
        '<div class="filters">'
        '<input type="text" id="q" placeholder="поиск: объект, kindName, контейнер">'
        '<label><input type="checkbox" id="f-human" checked> human</label>'
        '<label><input type="checkbox" id="f-agent" checked> agent</label>'
        '<select id="f-kind"><option value="">— все виды —</option>%s</select>'
        '</div>\n'
        '<div class="count" id="count"></div>\n'
        '<div id="list"></div>\n'
        '<script>%s</script>\n'
        '</body>\n</html>\n'
    ) % (
        html.escape(title), css, html.escape(title), html.escape(period),
        total, human, agent,
        ('<div class="card"><div class="n">%d</div><div class="l">прочие src</div></div>' % other)
        if other else '',
        kind_chips,
        "".join('<option value="%s">%s</option>' % (html.escape(k), html.escape(k))
                for k in kinds),
        js,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: --journal <путь> (обязателен), --out <html>, --title."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description='HTML-дашборд журнала изменений метаданных (journal/<pid>.jsonl)')
    ap.add_argument('--journal', required=True, help='путь к journal/<pid>.jsonl')
    ap.add_argument('--out', default='', help='имя html (по умолчанию рядом, суффикс -dashboard.html)')
    ap.add_argument('--title', default=TITLE_DEFAULT, help='заголовок дашборда')
    a = ap.parse_args(argv)
    if not os.path.isfile(a.journal):
        print('journal_dashboard: файл журнала не найден: %s' % a.journal, file=sys.stderr)
        return 2
    out = a.out or str(Path(a.journal).with_name(Path(a.journal).stem + '-dashboard.html'))
    entries = load_entries(a.journal)
    page = build_dashboard(entries, a.title)
    with open(out, 'w', encoding='utf-8-sig', newline='\n') as fh:
        fh.write(page)
    print(json.dumps({'out': out, 'entries': len(entries)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
