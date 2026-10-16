"""Задача ассистента файлов: журнал, статусы, решение человека, повтор (День 34, FR-05, FR-09; D-05, D-08).

Задача — то, что ассистент сделал по одной цели: базовый коммит, модель, вызовы инструментов
(имя, аргументы, размер ответа — без содержимого файлов), прочитанные и изменённые файлы,
находки с проверкой кодом, черновик правок (diff), решение человека и коммит. Хранится в
`<память>/files/tasks/<id>.json`, черновик сервера files — рядом, `<id>.draft.json`.

Статусы: «в работе» → «готово» (только отчёт) | «ждёт подтверждения» (есть черновик) |
«не выполнено» (сбой); «ждёт подтверждения» → «применена» (коммит на ветке assistant/<id>)
| «отклонена». Применённую и отклонённую задачу второй раз не решить.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

СТАТУСЫ = ("в работе", "готово", "ждёт подтверждения", "применена", "отклонена", "не выполнено")


@dataclass
class Задача:
    id: int
    цель: str
    проект: str
    база: str
    модель: str
    создана: str = ""
    статус: str = "в работе"
    вид: str = ""
    итог: str = ""
    находки: list[dict[str, Any]] = field(default_factory=list)
    правки: str = ""
    diff: str = ""
    изменены: list[str] = field(default_factory=list)
    прочитаны: list[str] = field(default_factory=list)
    вызовы: list[dict[str, Any]] = field(default_factory=list)
    проверка: dict[str, Any] = field(default_factory=dict)
    пометки: list[str] = field(default_factory=list)
    круги: int = 0
    токены_вход: int = 0
    токены_выход: int = 0
    стоимость: float = 0.0
    секунд: float = 0.0
    решение: dict[str, Any] = field(default_factory=dict)
    ветка: str = ""
    коммит: str = ""
    повтор_от: int = 0
    сравнение: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def из_словаря(cls, д: dict[str, Any]) -> "Задача":
        поля = cls.__dataclass_fields__
        return cls(**{к: в for к, в in д.items() if к in поля})

    def сообщение_коммита(self) -> str:
        хвост = f"\n\n{self.правки.strip()}" if self.правки.strip() else ""
        return (f"Ассистент файлов, задача {self.id}: {self.цель.strip()[:200]}{хвост}\n\n"
                f"Модель {self.модель}; база {self.база[:12]}; файлов изменено {len(self.изменены)}.")


class Хранилище:
    def __init__(self, каталог: str = "") -> None:
        память = os.path.abspath(os.getenv("MEMORY_DIR") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "memory"))
        self.каталог = os.path.abspath(каталог or os.path.join(память, "files"))
        self.задачи = os.path.join(self.каталог, "tasks")
        self.метрики = os.path.join(self.каталог, "metrics.jsonl")

    def _путь(self, id: int) -> str:
        return os.path.join(self.задачи, f"{int(id)}.json")

    def черновик(self, id: int) -> str:
        return os.path.join(self.задачи, f"{int(id)}.draft.json")

    def новая(self, цель: str, проект: str, база: str, модель: str, повтор_от: int = 0) -> Задача:
        os.makedirs(self.задачи, exist_ok=True)
        with open(os.path.join(self.задачи, ".lock"), "a") as замок:
            fcntl.flock(замок, fcntl.LOCK_EX)
            номера = [int(ф[:-5]) for ф in os.listdir(self.задачи) if ф.endswith(".json") and ф[:-5].isdigit()]
            з = Задача(max(номера, default=0) + 1, цель.strip(), проект, база, модель,
                       создана=time.strftime("%Y-%m-%dT%H:%M:%S"), повтор_от=повтор_от)
            self.сохранить(з)
            fcntl.flock(замок, fcntl.LOCK_UN)
        return з

    def сохранить(self, з: Задача) -> None:
        os.makedirs(self.задачи, exist_ok=True)
        временный = self._путь(з.id) + f".{os.getpid()}.tmp"
        with open(временный, "w", encoding="utf-8") as f:
            json.dump(з.to_dict(), f, ensure_ascii=False, indent=1)
        os.replace(временный, self._путь(з.id))

    def загрузить(self, id: int) -> Задача:
        try:
            with open(self._путь(id), encoding="utf-8") as f:
                return Задача.из_словаря(json.load(f))
        except FileNotFoundError:
            raise KeyError(f"задачи {id} нет") from None

    def список(self, предел: int = 50) -> list[Задача]:
        if not os.path.isdir(self.задачи):
            return []
        номера = sorted((int(ф[:-5]) for ф in os.listdir(self.задачи) if ф.endswith(".json") and ф[:-5].isdigit()),
                        reverse=True)
        return [self.загрузить(н) for н in номера[:предел]]

    # --- решение человека -----------------------------------------------------------------

    def применить(self, id: int, копия: Any, кто: str = "человек") -> Задача:
        з = self.загрузить(id)
        if з.статус != "ждёт подтверждения" or not з.diff.strip():
            raise ValueError(f"задача {id} — «{з.статус}»: применять нечего" if not з.diff.strip()
                             else f"задача {id} уже «{з.статус}» — решение принято раньше")
        причина = копия.проверить(з.diff, з.база)
        if причина:
            raise ValueError(f"черновик задачи {id} не применяется к базе: {причина[:300]}")
        з.ветка = f"assistant/{з.id}"
        з.коммит = копия.применить(з.diff, з.ветка, з.сообщение_коммита(), з.база)
        з.статус = "применена"
        з.решение = {"кто": кто, "когда": time.strftime("%Y-%m-%dT%H:%M:%S"), "решение": "применить"}
        self.сохранить(з)
        self._решение_в_метрики(з, "применить", кто)
        return з

    def отклонить(self, id: int, причина: str = "", кто: str = "человек") -> Задача:
        з = self.загрузить(id)
        if з.статус != "ждёт подтверждения":
            raise ValueError(f"задача {id} — «{з.статус}»: отклонять нечего")
        з.статус = "отклонена"
        з.решение = {"кто": кто, "когда": time.strftime("%Y-%m-%dT%H:%M:%S"), "решение": "отклонить",
                     "причина": (причина or "без причины").strip()[:300]}
        self.сохранить(з)
        self._решение_в_метрики(з, "отклонить", кто)
        return з

    def _решение_в_метрики(self, з: Задача, решение: str, кто: str) -> None:
        from agent.files import metrics
        try:
            metrics.записать({"событие": "решение", "задача": з.id, "канал": кто, "решение": решение}, self.метрики)
        except OSError:
            pass


def _жаккар(а: set, б: set) -> float:
    if not а and not б:
        return 1.0
    return round(len(а & б) / len(а | б), 2)


def сравнить(а: Задача, б: Задача) -> dict[str, Any]:
    """Повтор той же цели на той же базе: совпадение изменённых файлов и подтверждённых находок."""
    находки_а = {(н["файл"], н.get("строка")) for н in а.находки if н.get("подтверждена")}
    находки_б = {(н["файл"], н.get("строка")) for н in б.находки if н.get("подтверждена")}
    return {"файлы_совпадение": _жаккар(set(а.изменены), set(б.изменены)),
            "находки_совпадение": _жаккар(находки_а, находки_б),
            "файлы": {"было": sorted(а.изменены), "стало": sorted(б.изменены)},
            "находок": {"было": len(находки_а), "стало": len(находки_б)}}
