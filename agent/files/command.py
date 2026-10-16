"""Команда `/files` в чатах агента — веб `/chat` и консоль (День 34, spec FR-08, plan D-07).

`/files цель` — ассистент файлов по цели на копии проекта (база из projects.json);
`/files --база ССЫЛКА цель` — на другой ветке или коммите копии (ветка посеянных случаев).
Итог — карточка в ленте сессии: находки с проверкой, diff черновика; «Применить» / «Отклонить»
— ручки веба, тот же путь, что `--файлы-применить` / `--файлы-отклонить` консоли (паритет).
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

_FILES = re.compile(r"^/files(?:@[\w_]+)?(?:\s+--база\s+(?P<база>\S+))?(?:\s+(?P<цель>.+))?\s*$", re.I | re.S)
_ССЫЛКА = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,99}$")
СПРАВКА = ("  /files цель — ассистент файлов: сам ищет и читает файлы копии проекта, находки с цитатами, "
           "черновик правок diff-ом; в файлы — только после «Применить» (--база ССЫЛКА — другая ветка копии)")


class ОшибкаКоманды(ValueError):
    pass


def разобрать(текст: str) -> tuple[str, str] | None:
    """(цель, база) или None — это не /files. Пустая цель или кривая ссылка — ОшибкаКоманды."""
    м = _FILES.match((текст or "").strip())
    if not м:
        return None
    цель, база = (м.group("цель") or "").strip(), (м.group("база") or "").strip()
    if not цель:
        raise ОшибкаКоманды("нужна цель: /files найди, где используется … / обнови README по коду / "
                            "составь ADR … / проверь код на правила проекта")
    if база and (not _ССЫЛКА.match(база) or ".." in база):
        raise ОшибкаКоманды(f"«{база}» — не ссылка git (ветка, тег, хэш)")
    return цель, база


def ассистент(модель: str = "", канал: str = "веб", ход: Callable[[dict[str, Any]], None] | None = None,
              проект: str = "crm") -> Any:
    from agent import catalog
    from agent.devassist import project
    from agent.files import task, workspace
    from agent.files.assistant import Ассистент
    модель = модель if модель in catalog.MODELS and not catalog.get(модель).local else "ds-flash"
    return Ассистент(workspace.из_проекта(проект), task.Хранилище(), модель=модель, канал=канал,
                     описание=project.загрузить(проект).описание, ход=ход)


def карточка(з: Any) -> dict[str, Any]:
    """Данные карточки веба: итог, находки с проверкой, diff, вызовы (без содержимого файлов)."""
    return {"вид": "файлы", "id": з.id, "цель": з.цель, "статус": з.статус, "итог": з.итог, "вид_цели": з.вид,
            "находки": з.находки, "diff": з.diff, "изменены": з.изменены, "прочитаны": з.прочитаны,
            "вызовы": з.вызовы[-60:], "вызовов": len(з.вызовы), "проверка": з.проверка, "пометки": з.пометки,
            "правки": з.правки, "база": з.база[:10], "модель": з.модель, "круги": з.круги,
            "токены": з.токены_вход + з.токены_выход, "стоимость": round(з.стоимость, 4), "секунд": з.секунд,
            "ветка": з.ветка, "коммит": з.коммит[:10], "решение": з.решение, "сравнение": з.сравнение}


def в_историю(чат: Any, з: Any, база: str = "") -> None:
    """Ход `/files` — в историю чата (реплика и короткий итог) и в ленту сессии для веба."""
    from agent.devassist import command
    кратко = (f"Ассистент файлов, задача {з.id} ({з.статус}): {з.итог[:600]}"
              + (f" Черновик: {', '.join(з.изменены)}." if з.изменены else ""))
    чат.короткая.append(чат.сессия_диалога, "user", f"/files {з.цель}")
    чат.короткая.append(чат.сессия_диалога, "assistant", кратко, tokens=з.токены_вход + з.токены_выход)
    if база:
        command.сохранить(база, чат.сессия, чат.память.реплик, карточка(з))


def обновить_в_ленте(база: str, з: Any) -> int:
    """После «Применить» / «Отклонить» — карточка задачи в лентах всех сессий получает новый статус."""
    from agent.devassist import command
    каталог = os.path.join(база, "devassist", "chat")
    if not os.path.isdir(каталог):
        return 0
    обновлено = 0
    for имя in os.listdir(каталог):
        if not имя.endswith(".json"):
            continue
        путь = os.path.join(каталог, имя)
        try:
            with open(путь, encoding="utf-8") as f:
                записи = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        тронуто = False
        for i, з_ in enumerate(записи if isinstance(записи, list) else []):
            if з_.get("вид") == "файлы" and з_.get("id") == з.id:
                записи[i] = {**з_, **карточка(з), "after_turn": з_.get("after_turn", 0), "when": з_.get("when")}
                тронуто = True
        if тронуто:
            временный = путь + ".tmp"
            with open(временный, "w", encoding="utf-8") as f:
                json.dump(записи[-command.ХРАНИТЬ:], f, ensure_ascii=False)
            os.replace(временный, путь)
            обновлено += 1
    return обновлено


def решение_в_метрики(з: Any, решение: str, канал: str) -> None:
    from agent.files import metrics, task
    metrics.записать({"событие": "решение", "задача": з.id, "канал": канал, "решение": решение,
                      "время": time.strftime("%Y-%m-%dT%H:%M:%S")}, task.Хранилище().метрики)
