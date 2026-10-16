"""Проверка результата ассистента файлов кодом (День 34, spec FR-04, plan D-04).

Модель отвечает итогом и находками, но фактом становится только то, что подтвердил код:
- находка — существующий незакрытый файл, строка и дословная цитата: цитата ищется в строке
  ±3 (номер строки поправляется по цитате), затем во всём файле (если цитата не короче 8
  символов); не найдена — «не подтверждена», а не выдаётся за факт;
- черновик — применяется к базе (`git apply --cached --check` во временном индексе);
  изменённые существующие файлы должны были читаться (`files_read`) — иначе пометка;
- новый ADR — по шаблону проекта (`templates/adr.md` CRM): ключи frontmatter, четыре
  раздела, ссылки на файлы в обратных кавычках ведут на существующие файлы;
- документы SDD — `tools/sdd.py проверить` проекта на копии с применённым черновиком: число
  ошибок не растёт (только когда черновик трогает docs/ или specs/ и в проекте есть sdd.py).

Содержимое берётся через files_server (база + черновик, границы путей — его же).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Any, Callable

import files_server
from agent.rag import cite

ОКНО = 3
КЛЮЧИ_ADR = ("id", "вид", "статус", "версия", "зависит_от")
РАЗДЕЛЫ_ADR = ("Контекст", "Решение", "Альтернативы", "Последствия")
_ПУТЬ_В_ТЕКСТЕ = re.compile(r"`([\w./-]+/[\w.-]+\.[A-Za-z0-9]{1,6})`")
_ADR = re.compile(r"^docs/decisions/(\d{4})-[^/]+\.md$")
_кэш_sdd: dict[tuple[str, str], int] = {}


def _норм(текст: str) -> str:
    return cite.нормализовать(текст or "")


def находки(сырые: list[dict[str, Any]]) -> list[dict[str, Any]]:
    итог: list[dict[str, Any]] = []
    видел: set[tuple[str, int, str]] = set()
    for с in сырые or []:
        if not isinstance(с, dict):
            continue
        н = {к: в for к, в in с.items() if к in ("файл", "строка", "цитата", "суть", "роль", "правило")}
        н["файл"] = str(н.get("файл") or "").strip()
        try:
            н["строка"] = int(н.get("строка") or 0)
        except (TypeError, ValueError):
            н["строка"] = 0
        н["цитата"] = str(н.get("цитата") or "").strip()
        н["подтверждена"], н["пометка"] = _подтвердить(н)
        if н["подтверждена"] and н["строка"]:
            н["строка_файла"] = _строка_файла(н["файл"], н["строка"], из_базы=н["пометка"].startswith("в базе"))
        ключ = (н["файл"], н["строка"], _норм(н["цитата"]))
        if ключ in видел:
            continue
        видел.add(ключ)
        итог.append(н)
    return итог


def _подтвердить(н: dict[str, Any]) -> tuple[bool, str]:
    try:
        путь = files_server.проверить_путь(н["файл"])
        текст, из_черновика = files_server.текущий(путь)
    except files_server.ОшибкаЗапроса as e:
        return False, str(e)[:120]
    if текст is None:
        return False, "файла нет"
    цитата = _норм(н["цитата"])
    if not цитата:
        return False, "нет цитаты"
    # Находка «файл есть» (например, его нет в README): цитата — сам путь существующего файла.
    if цитата in (_норм(путь), _норм(путь.rsplit("/", 1)[-1])):
        н["строка"] = 0
        return True, "файл существует"
    найдено = _в_тексте(н, текст, цитата)
    if найдено is not None:
        return True, найдено
    # Цитата из строки, которую черновик уже изменил: её место — в базе.
    if из_черновика:
        база = files_server.в_базе(путь)
        if база is not None:
            найдено = _в_тексте(н, база, цитата)
            if найдено is not None:
                return True, "в базе (строка изменена черновиком)" + (f"; {найдено}" if найдено else "")
    return False, "цитаты в файле нет"


def _в_тексте(н: dict[str, Any], текст: str, цитата: str) -> str | None:
    """Пометка (пусто — строка верна), если цитата найдена в строке ±ОКНО или во всём файле; None — нет."""
    строки = текст.splitlines()
    заявлена = н["строка"]
    окно = range(max(1, заявлена - ОКНО), min(len(строки), заявлена + ОКНО) + 1) if заявлена else range(0)
    for номер in sorted(окно, key=lambda н_: abs(н_ - заявлена)):
        if цитата in _норм(строки[номер - 1]):
            if номер != заявлена:
                н["строка"] = номер
                return f"строка поправлена (была {заявлена})"
            return ""
    # Цитата на несколько строк или далеко от заявленной строки (не короче 5 символов: «T-3203»).
    if len(цитата) >= 5:
        for номер, строка in enumerate(строки, start=1):
            if цитата in _норм(строка):
                н["строка"] = номер
                return f"строка поправлена (была {заявлена})"
        if цитата in _норм(текст):
            return "цитата на нескольких строках"
    return None


def _строка_файла(путь: str, номер: int, из_базы: bool = False) -> str:
    """Настоящая строка файла — для карточки и судьи: он видит код, а не пересказ. Находка о строке,
    которую черновик уже исправил, — строка базы (иначе судья видит исправленный текст)."""
    try:
        путь = files_server.проверить_путь(путь)
        текст = files_server.в_базе(путь) if из_базы else files_server.текущий(путь)[0]
    except files_server.ОшибкаЗапроса:
        return ""
    строки = (текст or "").splitlines()
    return строки[номер - 1].strip()[:200] if 0 < номер <= len(строки) else ""


def adr(путь: str, текст: str, существует: Callable[[str], bool]) -> list[str]:
    """Замечания к новому ADR по шаблону проекта; пусто — по правилам."""
    замечания = []
    м = re.match(r"^---\n(.*?)\n---\n", текст, re.S)
    if not м:
        замечания.append("нет frontmatter (--- ключи --- в начале файла, как в templates/adr.md)")
        шапка: dict[str, str] = {}
    else:
        шапка = {}
        for строка in м.group(1).splitlines():
            ключ, _, значение = строка.partition(":")
            шапка[ключ.strip()] = значение.strip()
        for ключ in КЛЮЧИ_ADR:
            if not шапка.get(ключ):
                замечания.append(f"frontmatter: не заполнен ключ «{ключ}»")
        if шапка.get("вид") and шапка["вид"] != "adr":
            замечания.append(f"frontmatter: вид «{шапка['вид']}», нужен «adr»")
        номер = _ADR.match(путь)
        if номер and шапка.get("id") and шапка["id"] != f"ADR-{номер.group(1)}":
            замечания.append(f"id «{шапка['id']}» не совпадает с именем файла (ADR-{номер.group(1)})")
    for раздел in РАЗДЕЛЫ_ADR:
        if not re.search(rf"^##\s+{раздел}\b", текст, re.M):
            замечания.append(f"нет раздела «## {раздел}»")
    for ссылка in sorted(set(_ПУТЬ_В_ТЕКСТЕ.findall(текст))):
        if not существует(ссылка):
            замечания.append(f"ссылка на несуществующий файл: {ссылка}")
    return замечания


def _существует(путь: str) -> bool:
    """Путь из текста документа: от корня проекта или, как принято в документах Django-проектов
    («apps/core/tasks.py»), от каталога с кодом (backend/, src/)."""
    for вариант in (путь, f"backend/{путь}", f"src/{путь}"):
        try:
            текст, _ = files_server.текущий(files_server.проверить_путь(вариант))
        except files_server.ОшибкаЗапроса:
            continue
        if текст is not None:
            return True
    return False


def черновик(д: dict[str, Any], копия: Any, база: str, прочитаны: list[str]) -> dict[str, Any]:
    """Отчёт о черновике: применяется ли, что менялось без чтения, замечания к ADR."""
    файлы = [ф["путь"] for ф in д.get("файлы", [])]
    отчёт: dict[str, Any] = {"файлов": len(файлы), "применяется": False, "причина": "", "без_чтения": [],
                             "adr": {}}
    if not д.get("diff", "").strip():
        отчёт["причина"] = "черновик пуст"
        return отчёт
    if д.get("обрезано"):
        отчёт["причина"] = "diff черновика обрезан — слишком большой"
        return отчёт
    причина = копия.проверить(д["diff"], база)
    отчёт["применяется"], отчёт["причина"] = not причина, причина[:300]
    новые = {ф["путь"] for ф in д.get("файлы", []) if ф.get("новый")}
    отчёт["без_чтения"] = [п for п in файлы if п not in новые and п not in set(прочитаны)]
    for п in файлы:
        if _ADR.match(п) or "/adr" in п.lower():
            текст, _ = files_server.текущий(п)
            отчёт["adr"][п] = adr(п, текст or "", _существует)
    return отчёт


def _ошибки_sdd(каталог: str) -> tuple[int, list[str]]:
    р = subprocess.run(["python3", "tools/sdd.py", "проверить"], cwd=каталог, capture_output=True, text=True,
                       timeout=300, stdin=subprocess.DEVNULL)
    строки = [с for с in (р.stdout + р.stderr).splitlines() if с.startswith("ОШИБКА")]
    return len(строки), строки


def sdd(копия: Any, база: str, diff: str) -> dict[str, Any]:
    """tools/sdd.py проекта на базе и на базе + черновике (выгрузка во временный каталог)."""
    if not копия.git("ls-tree", "--name-only", база, "tools/sdd.py").strip():
        return {"есть": False}
    итог: dict[str, Any] = {"есть": True}
    with tempfile.TemporaryDirectory(prefix="files-sdd-") as tmp:
        архив = subprocess.run(["git", "-C", копия.каталог, "archive", "--format=tar", база],
                               capture_output=True, timeout=120, check=True).stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=архив, check=True, timeout=120)
        ключ = (копия.каталог, база)
        if ключ not in _кэш_sdd:
            _кэш_sdd[ключ] = _ошибки_sdd(tmp)[0]
        итог["ошибок_до"] = _кэш_sdd[ключ]
        р = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=tmp, input=diff, text=True,
                           capture_output=True, timeout=60)
        if р.returncode != 0:
            итог.update(ошибок_после=None, новые=[f"diff не применился: {р.stderr.strip()[:200]}"])
            return итог
        после, строки = _ошибки_sdd(tmp)
    итог["ошибок_после"] = после
    итог["новые"] = строки[:20] if после > итог["ошибок_до"] else []
    return итог


def нужен_sdd(файлы: list[str]) -> bool:
    return any(п.endswith(".md") and п.startswith(("docs/", "specs/")) for п in файлы)


def путь_проекта_sdd(копия: Any) -> str:
    return os.path.join(копия.каталог, "tools", "sdd.py")
