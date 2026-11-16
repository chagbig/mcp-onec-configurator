# -*- coding: utf-8 -*-
"""Живой e2e контура форм (этап 8): чтение, кэш М2, запись.

Прогон: python server/tests/live_e2e_forms.py <pid>
<pid> — живая сессия конфигуратора на файловой тестовой базе (как в live_e2e.py).
База должна входить в own_bases конфига сервера (нужен update_db_cfg).

Запись идёт штатной командой «Загрузить конфигурацию из файла» через мост —
конфигуратор остаётся открытым, сессия не меняется. Правка идемпотентна:
кнопка МсрE2EКнопка заменяется при каждом прогоне, база в сохранённом состоянии.

Выход: код 0 и построчный вердикт ok/FAIL (как в mcp_to_line).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from mcp_snegopat.config import load_config  # noqa: E402
from mcp_snegopat.server import Toolkit  # noqa: E402

OBJ = "ОбщаяФорма.МсрФормаСервер"
BUTTON = "МсрE2EКнопка"


def _make_edit():
    """Идемпотентная правка: удалить прежнюю e2e-кнопку, добавить заново со
    свободным id (простая повторяемость прогона на одной базе)."""
    from lxml import etree
    NS = "http://v8.1c.ru/8.3/xcf/logform"

    def edit(xml_text: str) -> str:
        root = etree.fromstring(xml_text.encode("utf-8"))
        for btn in root.findall(".//{%s}Button" % NS):
            if btn.get("name") == BUTTON:
                btn.getparent().remove(btn)
        ids = [int(e.get("id")) for e in root.iter()
               if e.get("id") and e.get("id").lstrip("-").isdigit()]
        nid = max(ids) + 1 if ids else 1
        acb = root.find("{%s}AutoCommandBar" % NS)
        items = acb.find("{%s}ChildItems" % NS)
        if items is None:
            items = etree.SubElement(acb, "{%s}ChildItems" % NS)
        btn = etree.SubElement(items, "{%s}Button" % NS)
        btn.set("name", BUTTON)
        btn.set("id", str(nid))
        etree.SubElement(btn, "{%s}Type" % NS).text = "CommandBarButton"
        etree.SubElement(btn, "{%s}CommandName" % NS).text = \
            "Form.StandardCommand.CustomizeForm"
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8").decode("utf-8")

    return edit


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pid = int(sys.argv[1])
    tk = Toolkit(load_config())
    # предусловие: основная конфигурация открыта (свежеподнятая сессия — не открыта)
    tk.bridge.call(pid, "config_open", {}, timeout=300)
    results = []

    def step(name: str, ok: bool, info: str) -> bool:
        results.append(ok)
        print(("ok   " if ok else "FAIL ") + name + ": " + info, flush=True)
        return ok

    # 1. чтение (кэш как повезёт — поле cache фиксируем, xml обязателен)
    r1 = tk.form_get_xml(session=pid, obj_path=OBJ)
    ok1 = bool(r1["xml"].lstrip().startswith("<") and "logform" in r1["xml"]
               and r1.get("form_uuid"))
    step("form_get_xml", ok1,
         "cache=%s, %d байт, %d мс" % (r1["cache"], len(r1["xml"]), r1["ms"]))

    # 2. повторное чтение — тот же xml (кэш-слои М2 не меняют содержимое)
    r2 = tk.form_get_xml(session=pid, obj_path=OBJ)
    step("кэш М2 повтор", r2["xml"] == r1["xml"],
         "cache=%s, %d мс" % (r2["cache"], r2["ms"]))

    # 3. запись: идемпотентная правка, К2-снимок из чтения, постусловия внутри
    res = tk.form_set_xml(session=pid, obj_path=OBJ, xml=_make_edit()(r2["xml"]),
                          edit_marker=BUTTON, expected_snapshot=r1["snapshot"])
    step("form_set_xml", bool(res.get("new_session")),
         "new_session=%s, %d мс" % (res.get("new_session"), res["ms"]))

    # 4. верификация: живой form_save содержит кнопку (сессия та же)
    fresh = Toolkit(load_config())
    new_pid = res["new_session"]
    r = fresh.bridge.call(new_pid, "form_save",
                          {"path": OBJ, "prop": "Форма", "file": "e2e-forms-verify.txt"},
                          timeout=120)
    text = Path(r["file"]).read_text(encoding="utf-8-sig")
    step("живой form_save", BUTTON in text, "%d байт" % r["size"])

    print("итого: %d/%d" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
