"""Ассистент файлов: цель → цикл модели с инструментами files → проверка кодом (День 34, FR-03, FR-11; D-03).

Ход:
1. копия проекта на базе (свой клон, workspace.py); новая задача в хранилище;
2. окружение сервера files закрепляется за задачей: корень копии, база, черновик задачи
   (под замком: веб может вести две задачи сразу, а окружение у процесса одно);
3. круги: модель (function calling) просит инструменты, вызов — клиентом MCP в процессе
   (`mcp.Client(files_server.сервер)`), ответ — модели; после круга — «осталось кругов»;
   старые выводы сжимаются; предел кругов или входа — последний вызов без инструментов;
4. итог JSON → проверка кодом (check.py): находки, черновик, ADR, sdd.py проекта;
5. статус: черновик применяется — «ждёт подтверждения», иначе «готово»; сбой облака —
   «не выполнено»; метрики без текстов.

Повтор (`повторить`) — та же цель на той же базе, новая задача и сравнение с прежней.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable

import files_server
from agent.files import check, metrics, prompts, task
from agent.llm import LLMError
from agent.local import progress
from agent.service import pd

КРУГОВ = 12
ПРЕДЕЛ_ВХОДА = 250_000          # входных токенов за цель (ворота 0: 127–148 тыс. на ds-flash)
ПРЕДЕЛ_ВЫВОДА = 15_000          # символов ответа инструмента модели
ПОЛНЫХ_ВЫВОДОВ = 6              # последние ответы инструментов — целиком, старые — сжато
СЖАТО = 1500
ОТВЕТ_ТОКЕНОВ = 6000
_замок = threading.Lock()


def _кратко(имя: str, данные: dict[str, Any]) -> str:
    if имя == "files_search":
        return f"{данные.get('мест', 0)} мест в {данные.get('файлов', 0)} файлах"
    if имя == "files_list":
        return f"{данные.get('всего', 0)} файлов"
    if имя == "files_read":
        return f"строки {данные.get('с')}–{данные.get('по')} из {данные.get('всего_строк')}"
    if имя == "files_outline":
        return f"{данные.get('пунктов', 0)} пунктов"
    if имя in ("files_edit", "files_create"):
        return "в черновик"
    if имя == "files_draft_diff":
        return f"{данные.get('файлов', 0)} файлов в черновике"
    if имя == "files_log":
        return f"{данные.get('коммитов', 0)} коммитов"
    return ""


def разобрать_итог(текст: str) -> dict[str, Any] | None:
    """JSON итога из ответа модели (с обёрткой ```json или текстом вокруг)."""
    текст = текст or ""
    for начало in [м.start() for м in re.finditer(r"\{", текст)]:
        глубина = 0
        for i in range(начало, len(текст)):
            if текст[i] == "{":
                глубина += 1
            elif текст[i] == "}":
                глубина -= 1
                if глубина == 0:
                    try:
                        д = json.loads(текст[начало:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(д, dict) and "итог" in д:
                        return д
                    break
    return None


class Ассистент:
    def __init__(self, копия: Any, хранилище: task.Хранилище, клиент: Any = None, модель: str = "ds-flash",
                 кругов: int = КРУГОВ, канал: str = "консоль", описание: str = "", проверять_sdd: bool = True,
                 ход: Callable[[dict[str, Any]], None] | None = None, паузы: tuple[float, ...] = (2.0, 5.0)) -> None:
        if клиент is None:
            from agent.llm import Client
            клиент = Client()
        self.копия = копия
        self.хр = хранилище
        self.клиент = клиент
        self.модель = модель
        self.кругов = кругов
        self.канал = канал
        self.описание = описание
        self.проверять_sdd = проверять_sdd
        self.ход = ход
        self.паузы = паузы

    # --- публичное ---------------------------------------------------------------------------

    def выполнить(self, цель: str, база: str = "", повтор_от: int = 0) -> task.Задача:
        if not (цель or "").strip():
            raise ValueError("нужна цель")
        старт = time.monotonic()
        self._событие("копия", подпись="копия проекта")
        полная_база = self.копия.подготовить()
        if база:
            полная_база = self.копия.хэш(база)
            if not полная_база:
                raise ValueError(f"ссылки «{база}» в копии проекта нет")
        з = self.хр.новая(цель, self.копия.проект, полная_база, self.модель, повтор_от=повтор_от)
        try:
            with _замок, _окружение(self.копия.каталог, полная_база, self.хр.черновик(з.id),
                                     self.копия.проект):
                self._круги(з)
                self._проверить(з)
        except LLMError as e:
            з.статус, з.итог = "не выполнено", f"модель не ответила: {str(e)[:300]}"
            з.diff, з.изменены = "", []
        з.секунд = round(time.monotonic() - старт, 1)
        self.хр.сохранить(з)
        try:
            metrics.записать(metrics.из_задачи(з, self.канал), self.хр.метрики)
        except OSError:
            pass
        self._событие("итог", подпись=з.статус, задача=з.id)
        return з

    def повторить(self, id: int) -> task.Задача:
        прежняя = self.хр.загрузить(id)
        з = self.выполнить(прежняя.цель, прежняя.база, повтор_от=прежняя.id)
        з.сравнение = task.сравнить(прежняя, з)
        self.хр.сохранить(з)
        return з

    # --- круги -------------------------------------------------------------------------------

    def _событие(self, этап: str, **данные: Any) -> None:
        progress.этап(f"файлы:{этап}", **данные)
        if self.ход:
            try:
                self.ход({"этап": этап, **данные})
            except Exception:  # noqa: BLE001 — наблюдатель не ломает задачу
                pass

    def _каталоги(self) -> str:
        верх = sorted({п.split("/", 1)[0] + ("/" if "/" in п else "") for п in files_server._файлы_базы()})
        return ", ".join(верх[:40])

    def _звать(self, з: task.Задача, сообщения: list[dict[str, Any]], инструменты: list[dict[str, Any]] | None) -> Any:
        последняя: Exception | None = None
        for попытка in range(len(self.паузы) + 1):
            try:
                r = self.клиент.call(self.модель, сообщения, max_tokens=ОТВЕТ_ТОКЕНОВ, temperature=0,
                                     tools=инструменты)
                з.токены_вход += r.prompt_tokens
                з.токены_выход += r.completion_tokens
                з.стоимость += r.cost
                return r
            except LLMError as e:
                последняя = e
                if попытка < len(self.паузы):
                    з.пометки.append(f"модель не ответила ({str(e)[:60]}) — повтор")
                    time.sleep(self.паузы[попытка])
        raise последняя  # type: ignore[misc]

    def _круги(self, з: task.Задача) -> None:
        инструменты = _инструменты()
        сообщения: list[dict[str, Any]] = [
            {"role": "system", "content": prompts.РОЛЬ.format(описание=self.описание or self.копия.проект,
                                                              каталоги=self._каталоги())},
            {"role": "user", "content": з.цель},
        ]
        r = None
        for круг in range(1, self.кругов + 1):
            з.круги = круг
            self._событие("модель", круг=круг, подпись=f"круг {круг}")
            r = self._звать(з, сообщения, инструменты)
            if not r.tool_calls:
                break
            сообщения.append(r.message or {"role": "assistant", "content": r.text or ""})
            for в in r.tool_calls:
                сообщения.append({"role": "tool", "tool_call_id": в.ид, "content": self._вызвать(з, в)})
            _сжать(сообщения)
            осталось = self.кругов - круг
            if r.prompt_tokens >= ПРЕДЕЛ_ВХОДА:
                з.пометки.append(f"предел входа {ПРЕДЕЛ_ВХОДА} токенов: итог по собранному")
                r = None
                break
            if осталось == 0:
                з.пометки.append(f"предел кругов ({self.кругов}): итог по собранному")
                r = None
                break
            сообщения.append({"role": "user", "content": prompts.ОСТАЛОСЬ.format(n=осталось)
                              + (prompts.ПОРА if осталось <= 3 else "")})
        if r is None:
            сообщения.append({"role": "user", "content": prompts.КРУГИ_КОНЧИЛИСЬ})
            self._событие("модель", подпись="итог без инструментов")
            r = self._звать(з, сообщения, None)
        итог = разобрать_итог(r.text)
        if итог is None:
            сообщения += [{"role": "assistant", "content": r.text or ""},
                          {"role": "user", "content": prompts.ПОВТОР_ФОРМАТА}]
            r = self._звать(з, сообщения, None)
            итог = разобрать_итог(r.text)
        if итог is None:
            з.пометки.append("итог модели не разобран — показан текстом")
            итог = {"итог": (r.text or "").strip()[:2000]}
        текст, _ = pd.скрыть(str(итог.get("итог") or ""), профиль="точный")
        з.итог = текст
        з.вид = str(итог.get("вид") or "")[:40]
        з.правки = str(итог.get("правки") or "")[:1000]
        з.находки = list(итог.get("находки") or [])[:200]

    def _вызвать(self, з: task.Задача, в: Any) -> str:
        старт = time.monotonic()
        self._событие("инструмент", подпись=в.имя, аргументы=_аргументы_кратко(в.аргументы))
        if в.ошибка:
            ок, текст, данные = False, f"аргументы не разобраны: {в.ошибка}", {}
        elif в.имя not in ИМЕНА:
            ок, текст, данные = False, f"инструмента «{в.имя}» нет; есть: {', '.join(ИМЕНА)}", {}
        else:
            ок, текст, данные = _вызов_mcp(в.имя, в.аргументы or {})
        if ок and в.имя == "files_read":
            путь = str(данные.get("путь") or (в.аргументы or {}).get("path") or "")
            if путь and путь not in з.прочитаны:
                з.прочитаны.append(путь)
        текст_модели = текст if len(текст) <= ПРЕДЕЛ_ВЫВОДА else текст[:ПРЕДЕЛ_ВЫВОДА] + "\n… вывод обрезан"
        текст_модели, _ = pd.скрыть(текст_модели, профиль="точный")
        з.вызовы.append({"имя": в.имя, "аргументы": _аргументы_кратко(в.аргументы), "ок": ок,
                         "мс": round((time.monotonic() - старт) * 1000), "размер": len(текст),
                         "кратко": _кратко(в.имя, данные) if ок else текст[:160]})
        return текст_модели

    # --- проверка -------------------------------------------------------------------------------

    def _проверить(self, з: task.Задача) -> None:
        self._событие("проверка", подпись="проверка кодом")
        з.находки = check.находки(з.находки)
        д = files_server.diff_черновика()
        з.diff = д["diff"] if not д["пусто"] else ""
        з.изменены = [ф["путь"] for ф in д["файлы"]]
        з.проверка = check.черновик(д, self.копия, з.база, з.прочитаны) if з.diff else {"применяется": False}
        if з.diff and self.проверять_sdd and check.нужен_sdd(з.изменены) and з.проверка.get("применяется"):
            self._событие("проверка", подпись="правила SDD проекта (tools/sdd.py)")
            try:
                з.проверка["sdd"] = check.sdd(self.копия, з.база, з.diff)
            except Exception as e:  # noqa: BLE001 — проверка проекта не ломает задачу
                з.проверка["sdd"] = {"есть": True, "ошибка": str(e)[:200]}
        if з.diff and з.проверка.get("применяется"):
            з.статус = "ждёт подтверждения"
        else:
            з.статус = "готово"
            if з.diff:
                з.пометки.append(f"черновик не применяется к базе — не предлагается: {з.проверка.get('причина', '')[:160]}")
        if з.проверка.get("без_чтения"):
            з.пометки.append("правка без чтения: " + ", ".join(з.проверка["без_чтения"]))
        не = [н for н in з.находки if not н.get("подтверждена")]
        if не:
            з.пометки.append(f"не подтверждено находок: {len(не)} из {len(з.находки)}")


# --- MCP в процессе -----------------------------------------------------------------------------

ИМЕНА = ("files_list", "files_search", "files_read", "files_outline", "files_log", "files_show",
         "files_create", "files_edit", "files_draft_diff")
_список_инструментов: list[dict[str, Any]] = []


def _с_клиентом(действие: Callable[[Any], Any]) -> Any:
    import anyio
    from mcp import Client

    async def работа():
        async with Client(files_server.сервер) as клиент:
            return await действие(клиент)
    return anyio.run(работа)


def _инструменты() -> list[dict[str, Any]]:
    if not _список_инструментов:
        async def список(к):
            return (await к.list_tools()).tools
        _список_инструментов.extend(
            {"type": "function", "function": {"name": и.name, "description": и.description or "",
                                              "parameters": и.input_schema}}
            for и in _с_клиентом(список) if и.name in ИМЕНА)
    return _список_инструментов


def _вызов_mcp(имя: str, аргументы: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    async def вызов(к):
        return await к.call_tool(имя, аргументы)
    try:
        р = _с_клиентом(вызов)
    except Exception as e:  # noqa: BLE001 — сбой сервера тоже ответ модели
        return False, str(e), {}
    текст = "\n".join(getattr(б, "text", "") for б in (р.content or []))
    данные = getattr(р, "structured_content", None) or {}
    if not данные:
        try:
            данные = json.loads(текст)
        except (json.JSONDecodeError, TypeError):
            данные = {}
    if isinstance(данные, dict) and set(данные) == {"result"} and isinstance(данные["result"], dict):
        данные = данные["result"]
    return not р.is_error, текст, данные if isinstance(данные, dict) else {}


def _аргументы_кратко(аргументы: dict[str, Any] | None) -> dict[str, Any]:
    """Аргументы для журнала: длинные строки (содержимое правок) — только длина."""
    return {к: (в if not isinstance(в, str) or len(в) <= 120 else f"<{len(в)} симв.>")
            for к, в in (аргументы or {}).items()}


def _сжать(сообщения: list[dict[str, Any]]) -> None:
    ответы = [i for i, м in enumerate(сообщения) if м.get("role") == "tool"]
    for i in ответы[:-ПОЛНЫХ_ВЫВОДОВ]:
        текст = сообщения[i].get("content") or ""
        if len(текст) > СЖАТО and not текст.endswith(prompts.СОКРАЩЕНО):
            сообщения[i] = {**сообщения[i], "content": текст[:СЖАТО] + prompts.СОКРАЩЕНО}


class _окружение:
    """Окружение сервера files на время задачи; прежние значения возвращаются."""

    КЛЮЧИ = ("FILES_ROOT", "FILES_BASE", "FILES_DRAFT", "FILES_PROJECT")

    def __init__(self, корень: str, база: str, черновик: str, проект: str) -> None:
        self.новое = dict(zip(self.КЛЮЧИ, (корень, база, черновик, проект)))

    def __enter__(self):
        self.было = {к: os.environ.get(к) for к in self.КЛЮЧИ}
        os.environ.update(self.новое)
        return self

    def __exit__(self, *_):
        for к, в in self.было.items():
            if в is None:
                os.environ.pop(к, None)
            else:
                os.environ[к] = в
