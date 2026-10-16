"""MCP-сервер files — чтение, поиск и черновик правок файлов проекта (День 34, spec FR-02, plan D-02).

Ассистент работает с файлами копии проекта (свой клон CRM на закреплённом коммите):
перечисляет, читает, ищет по многим файлам, смотрит оглавление и историю — и готовит
правки. Правки **не пишутся на диск**: `files_create` и `files_edit` кладут новое
содержимое в черновик задачи (JSON-файл `FILES_DRAFT`), `files_draft_diff` показывает
единый diff черновика против базы. В файлы проекта черновик записывает код задачи после
подтверждения человеком (agent/files/workspace.py) — у сервера записи на диск, удаления,
запуска команд и push нет вовсе (ответ на «опасные операции» лекции недели 7 построением).

Что видит модель. Содержимое берётся из объектов git базового коммита (`FILES_BASE`,
по умолчанию HEAD), а поверх него — черновик: модель читает файлы «как после своих правок»,
а рабочий каталог копии не участвует вовсе. Поиск (`files_search`) идёт по базе.

Границы. Путь — относительный, внутри проекта, без `..`, без `.git/` и закрытых файлов
(`.env*`, ключи, сертификаты, `secrets/`); бинарные файлы не читаются; ответы ограничены
по размеру. git — из белого списка подкоманд, без необязательных блокировок и без
исполнения программ из локального конфига (как git_server.py Дня 31).

    python files_server.py --корень memory/files/crm --база 9474ed1 --черновик /tmp/d.json
"""

from __future__ import annotations

import argparse
import ast
import difflib
import fcntl
import fnmatch
import json
import os
import re
import subprocess
import sys
from typing import Annotated, Any

from pydantic import Field

КОРЕНЬ_СКРИПТА = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, КОРЕНЬ_СКРИПТА)

from mcp.server import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp_types import ToolAnnotations  # noqa: E402

ИМЯ = "files"
ВЕРСИЯ = "34.0"
ПРЕДЕЛ_ФАЙЛОВ = 300
ПРЕДЕЛ_ЧТЕНИЯ_СТРОК = 400
ПРЕДЕЛ_ЧТЕНИЯ_СИМВОЛОВ = 60_000
ПРЕДЕЛ_ПОИСКА = 200
ПРЕДЕЛ_ЗАПИСИ = 200_000            # символов в одном файле черновика
ПРЕДЕЛ_ЧЕРНОВИКА = 30              # файлов в черновике
ПРЕДЕЛ_DIFF = 60_000
ПРЕДЕЛ_ЖУРНАЛА = 30
ТАЙМАУТ_С = 20.0
РАЗРЕШЁННЫЕ = ("ls-tree", "cat-file", "grep", "log", "show", "rev-parse")
_НАСТРОЙКИ = ("-c", "core.fsmonitor=", "-c", "core.untrackedCache=false", "-c", "color.ui=never")
# Закрытые пути: секреты и служебное. Шаблоны fnmatch по пути и по имени файла.
ЗАКРЫТО = (".git", ".git/*", "*/.git/*", ".env", ".env.*", "*/.env", "*/.env.*", "*.pem", "*.key",
           "*.p12", "*.pfx", "id_rsa*", "*/id_rsa*", "secrets/*", "*/secrets/*", "*.sqlite3", "*.db")

ИНСТРУКЦИИ = """\
Файлы проекта: files_list — файлы по шаблону (glob), files_search — поиск строки или \
регулярного выражения по многим файлам (файл, строка, текст), files_read — файл по \
строкам с номерами, files_outline — оглавление (функции и классы Python, заголовки \
markdown, ключи JSON), files_log и files_show — история git. Правки — files_create \
(новый файл) и files_edit (замена фрагмента: old должен встречаться ровно один раз) — \
идут в черновик задачи, а не на диск; files_draft_diff — diff черновика. Записывает \
черновик в проект человек после подтверждения. Удалять файлы и запускать команды нельзя."""

ЧТЕНИЕ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                         open_world_hint=False)
ЧЕРНОВИК = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                           open_world_hint=False)

Путь = Annotated[str, Field(description="Путь файла внутри проекта, например backend/apps/core/urls.py.")]


class ОшибкаЗапроса(ToolError):
    """Неверный аргумент или git не ответил: текст доходит до модели, она может исправиться."""


# --- окружение -------------------------------------------------------------------------------

def корень() -> str:
    путь = os.path.abspath(os.path.expanduser(os.getenv("FILES_ROOT") or ""))
    if not os.getenv("FILES_ROOT") or not os.path.isdir(os.path.join(путь, ".git")):
        raise ОшибкаЗапроса(f"корень проекта не задан или не репозиторий git: {путь or '—'} (--корень или FILES_ROOT)")
    return путь


def база() -> str:
    return os.getenv("FILES_BASE") or "HEAD"


def _окружение() -> dict[str, str]:
    return dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C.UTF-8",
                GIT_PAGER="cat", PAGER="cat")


def _git(*аргументы: str, пусто_при: tuple[int, ...] = (), байты: bool = False) -> Any:
    if not аргументы or аргументы[0] not in РАЗРЕШЁННЫЕ:
        raise ОшибкаЗапроса(f"git {аргументы[:1]}: подкоманда не из белого списка")
    try:
        итог = subprocess.run(["git", *_НАСТРОЙКИ, "-C", корень(), *аргументы], capture_output=True,
                              env=_окружение(), timeout=ТАЙМАУТ_С, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise ОшибкаЗапроса(f"git {аргументы[0]}: нет ответа {ТАЙМАУТ_С:.0f} с") from e
    if итог.returncode in пусто_при and not итог.stderr.strip():
        return b"" if байты else ""
    if итог.returncode != 0:
        raise ОшибкаЗапроса(f"git {аргументы[0]}: {итог.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return итог.stdout if байты else итог.stdout.decode("utf-8", "replace")


def закрыт(путь: str) -> bool:
    имя = путь.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(путь, ш) or fnmatch.fnmatch(имя, ш) for ш in ЗАКРЫТО) or путь.startswith(".git/")


def проверить_путь(значение: str, шаблон: bool = False) -> str:
    """Относительный путь внутри проекта: без «..», абсолютных путей, NUL; закрытые — отказ."""
    значение = (значение or "").strip().replace("\\", "/")
    while значение.startswith("./"):
        значение = значение[2:]
    части = значение.split("/")
    if (not значение or значение.startswith(("/", "-", ":", "~")) or ".." in части or "\0" in значение
            or len(значение) > 300):
        raise ОшибкаЗапроса(f"путь «{значение}» недопустим: нужен путь внутри проекта, без «..» и абсолютных путей")
    if not шаблон and any(с in значение for с in "*?["):
        raise ОшибкаЗапроса(f"«{значение}» — шаблон; нужен путь одного файла (список — files_list)")
    if not шаблон and закрыт(значение):
        raise ОшибкаЗапроса(f"«{значение}» закрыт: секреты и служебные файлы не читаются и не пишутся")
    return значение


# --- черновик --------------------------------------------------------------------------------

def _путь_черновика() -> str:
    return os.getenv("FILES_DRAFT") or ""


def прочитать_черновик() -> dict[str, Any]:
    путь = _путь_черновика()
    if not путь or not os.path.exists(путь):
        return {"база": база(), "файлы": {}}
    with open(путь, encoding="utf-8") as f:
        return json.load(f)


def _записать_черновик(данные: dict[str, Any]) -> None:
    путь = _путь_черновика()
    if not путь:
        raise ОшибкаЗапроса("черновик задачи не задан (FILES_DRAFT) — правки некуда положить")
    os.makedirs(os.path.dirname(os.path.abspath(путь)), exist_ok=True)
    временный = f"{путь}.{os.getpid()}.tmp"
    with open(временный, "w", encoding="utf-8") as f:
        json.dump(данные, f, ensure_ascii=False, indent=1)
    os.replace(временный, путь)


class _Блок:
    def __enter__(self):
        путь = _путь_черновика()
        self.ф = open(путь + ".lock", "a") if путь else None
        if self.ф:
            fcntl.flock(self.ф, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        if self.ф:
            fcntl.flock(self.ф, fcntl.LOCK_UN)
            self.ф.close()


# --- содержимое ------------------------------------------------------------------------------

def в_базе(путь: str) -> str | None:
    """Текст файла в базовом коммите или None, если файла нет. Бинарный — ошибка."""
    объект = f"{база()}:{путь}"
    try:
        вид = _git("cat-file", "-t", объект).strip()
    except ОшибкаЗапроса:
        return None
    if вид != "blob":
        raise ОшибкаЗапроса(f"«{путь}» — не файл, а {вид}")
    данные = _git("cat-file", "blob", объект, байты=True)
    if b"\0" in данные[:8000]:
        raise ОшибкаЗапроса(f"«{путь}» — бинарный файл, читать его нечем")
    return данные.decode("utf-8", "replace")


def текущий(путь: str) -> tuple[str | None, bool]:
    """Содержимое «как сейчас»: черновик поверх базы. Второе — из черновика ли."""
    ч = прочитать_черновик()["файлы"].get(путь)
    if ч is not None:
        return ч["текст"], True
    return в_базе(путь), False


def _файлы_базы() -> list[str]:
    """Файлы базового коммита (ls-tree), а не рабочего каталога копии."""
    return [п for п in _git("ls-tree", "-r", "-z", "--name-only", база()).split("\0") if п]


def список(шаблон: str = "**", предел: int = 100) -> dict[str, Any]:
    шаблон = проверить_путь(шаблон or "**", шаблон=True)
    новые = [п for п, з in прочитать_черновик()["файлы"].items() if з.get("новый") and _glob(п, шаблон)]
    видимые = sorted({п for п in _файлы_базы() if _glob(п, шаблон)} | set(новые))
    видимые = [п for п in видимые if not закрыт(п)]
    предел = max(1, min(int(предел or 100), ПРЕДЕЛ_ФАЙЛОВ))
    return {"шаблон": шаблон, "база": база(), "всего": len(видимые), "файлы": видимые[:предел],
            "обрезано": len(видимые) > предел, "в_черновике": новые}


def _glob(путь: str, шаблон: str) -> bool:
    """glob как в git: «*» — внутри одного каталога, «**» — на любую глубину."""
    регулярка = ""
    i = 0
    while i < len(шаблон):
        с = шаблон[i]
        if шаблон.startswith("**/", i):
            регулярка += "(?:.*/)?"
            i += 3
            continue
        if шаблон.startswith("**", i):
            регулярка += ".*"
            i += 2
            continue
        регулярка += "[^/]*" if с == "*" else "[^/]" if с == "?" else re.escape(с)
        i += 1
    return re.fullmatch(регулярка, путь) is not None


def прочитать(путь: str, с: int = 1, по: int = 0) -> dict[str, Any]:
    путь = проверить_путь(путь)
    текст, из_черновика = текущий(путь)
    if текст is None:
        raise ОшибкаЗапроса(f"файла «{путь}» нет (список — files_list, поиск — files_search)")
    строки = текст.splitlines()
    всего = len(строки)
    с = max(1, int(с or 1))
    по = min(всего, int(по or 0) or всего, с + ПРЕДЕЛ_ЧТЕНИЯ_СТРОК - 1)
    кусок = [f"{н}\t{строки[н - 1]}" for н in range(с, по + 1)]
    вывод = "\n".join(кусок)
    обрезано = по < всего
    if len(вывод) > ПРЕДЕЛ_ЧТЕНИЯ_СИМВОЛОВ:
        вывод = вывод[:ПРЕДЕЛ_ЧТЕНИЯ_СИМВОЛОВ]
        по = с + вывод.count("\n")
        обрезано = True
    return {"путь": путь, "всего_строк": всего, "с": с, "по": по, "из_черновика": из_черновика,
            "текст": вывод, "обрезано": обрезано,
            "дальше": f"files_read(path, start={по + 1})" if обрезано else ""}


def искать(запрос: str, регулярка: bool = False, шаблон: str = "", слово: bool = False,
           регистр: bool = True, предел: int = ПРЕДЕЛ_ПОИСКА) -> dict[str, Any]:
    if not запрос or len(запрос) > 300 or "\0" in запрос:
        raise ОшибкаЗапроса("нужен запрос — строка до 300 символов")
    if регулярка:
        try:
            re.compile(запрос)
        except re.error as e:
            raise ОшибкаЗапроса(f"регулярное выражение не разобрано: {e}") from e
    # Регулярка — PCRE (-P): синтаксис как у Python и правил конституции ((?:…), \s, \d).
    аргументы = ["grep", "-n", "-I", "--no-color", "-P" if регулярка else "-F"]
    if слово:
        аргументы.append("-w")
    if not регистр:
        аргументы.append("-i")
    аргументы += ["-e", запрос, база(), "--"]
    if шаблон:
        аргументы.append(f":(glob){проверить_путь(шаблон, шаблон=True)}")
    вывод = _git(*аргументы, пусто_при=(1,))
    предел = max(1, min(int(предел or ПРЕДЕЛ_ПОИСКА), ПРЕДЕЛ_ПОИСКА))
    места, файлы = [], {}
    префикс = база() + ":"
    for строка in вывод.splitlines():
        if строка.startswith(префикс):
            строка = строка[len(префикс):]
        путь, _, остаток = строка.partition(":")
        номер, _, текст = остаток.partition(":")
        if not номер.isdigit() or закрыт(путь):
            continue
        файлы[путь] = файлы.get(путь, 0) + 1
        if len(места) < предел:
            места.append({"файл": путь, "строка": int(номер), "текст": текст.strip()[:240]})
    всего = sum(файлы.values())
    return {"запрос": запрос, "база": база(), "мест": всего, "файлов": len(файлы),
            "по_файлам": dict(sorted(файлы.items(), key=lambda п: -п[1])[:50]), "места": места,
            "обрезано": всего > len(места)}


def оглавление(путь: str) -> dict[str, Any]:
    путь = проверить_путь(путь)
    текст, _ = текущий(путь)
    if текст is None:
        raise ОшибкаЗапроса(f"файла «{путь}» нет")
    пункты: list[dict[str, Any]] = []
    if путь.endswith(".py"):
        try:
            дерево = ast.parse(текст)
        except SyntaxError as e:
            raise ОшибкаЗапроса(f"Python не разобран: {e}") from e
        for узел in ast.walk(дерево):
            if isinstance(узел, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                вид = "класс" if isinstance(узел, ast.ClassDef) else "функция"
                пункты.append({"вид": вид, "имя": узел.name, "строка": узел.lineno,
                               "до": getattr(узел, "end_lineno", узел.lineno)})
        пункты.sort(key=lambda п: п["строка"])
    elif путь.endswith((".md", ".markdown")):
        for н, строка in enumerate(текст.splitlines(), start=1):
            м = re.match(r"^(#{1,6})\s+(.+)", строка)
            if м:
                пункты.append({"вид": f"h{len(м.group(1))}", "имя": м.group(2).strip()[:160], "строка": н})
    elif путь.endswith(".json"):
        try:
            данные = json.loads(текст)
        except json.JSONDecodeError as e:
            raise ОшибкаЗапроса(f"JSON не разобран: {e}") from e
        if isinstance(данные, dict):
            пункты = [{"вид": "ключ", "имя": к, "тип": type(в).__name__} for к, в in данные.items()]
    else:
        raise ОшибкаЗапроса("оглавление есть у .py, .md и .json; остальное — files_read")
    return {"путь": путь, "строк": len(текст.splitlines()), "пунктов": len(пункты), "пункты": пункты[:300]}


def журнал(путь: str = "", предел: int = 20) -> dict[str, Any]:
    предел = max(1, min(int(предел or 20), ПРЕДЕЛ_ЖУРНАЛА))
    аргументы = ["log", f"-{предел}", "--format=%h%x1f%ad%x1f%s", "--date=short", база(), "--"]
    if путь:
        аргументы.append(f":(glob){проверить_путь(путь, шаблон=True)}")
    коммиты = []
    for строка in _git(*аргументы).splitlines():
        хэш, дата, тема = (строка.split("\x1f") + ["", ""])[:3]
        коммиты.append({"коммит": хэш, "дата": дата, "тема": тема[:200]})
    return {"путь": путь or "*", "коммитов": len(коммиты), "журнал": коммиты}


def коммит(ссылка: str) -> dict[str, Any]:
    ссылка = (ссылка or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{6,40}|HEAD(~\d{1,3})?", ссылка):
        raise ОшибкаЗапроса("нужен хэш коммита (из files_log) или HEAD~N")
    if ссылка.startswith("HEAD"):
        ссылка = база() + ссылка[4:]
    сводка = _git("show", "--no-color", "--no-ext-diff", "--no-textconv", "--stat=120",
                  "--format=%h %ad%n%s%n%n%b", "--date=short", ссылка, "--")
    diff = _git("show", "--no-color", "--no-ext-diff", "--no-textconv", "--format=", ссылка, "--")
    return {"коммит": ссылка, "сводка": сводка.strip()[:6000], "diff": diff[:20_000], "обрезано": len(diff) > 20_000}


# --- правки в черновик ----------------------------------------------------------------------

def создать(путь: str, содержимое: str) -> dict[str, Any]:
    путь = проверить_путь(путь)
    if len(содержимое or "") > ПРЕДЕЛ_ЗАПИСИ:
        raise ОшибкаЗапроса(f"файл длиннее {ПРЕДЕЛ_ЗАПИСИ} символов")
    with _Блок():
        ч = прочитать_черновик()
        if в_базе(путь) is not None:
            raise ОшибкаЗапроса(f"«{путь}» уже есть — меняйте его files_edit")
        if путь not in ч["файлы"] and len(ч["файлы"]) >= ПРЕДЕЛ_ЧЕРНОВИКА:
            raise ОшибкаЗапроса(f"в черновике уже {ПРЕДЕЛ_ЧЕРНОВИКА} файлов — больше нельзя")
        текст = содержимое if содержимое.endswith("\n") or not содержимое else содержимое + "\n"
        ч["файлы"][путь] = {"новый": True, "текст": текст}
        ч["база"] = база()
        _записать_черновик(ч)
    return {"путь": путь, "создан_в_черновике": True, "строк": текст.count("\n"),
            "дальше": "files_draft_diff — посмотреть diff"}


def заменить(путь: str, было: str, стало: str) -> dict[str, Any]:
    путь = проверить_путь(путь)
    if not было:
        raise ОшибкаЗапроса("old пуст: укажите дословный фрагмент файла, который заменяется")
    with _Блок():
        ч = прочитать_черновик()
        текст, _ = текущий(путь)
        if текст is None:
            raise ОшибкаЗапроса(f"файла «{путь}» нет; новый файл — files_create")
        вхождений = текст.count(было)
        if вхождений != 1:
            raise ОшибкаЗапроса(
                f"фрагмент old встречается в «{путь}» {вхождений} раз(а), нужно ровно 1: "
                + ("скопируйте его дословно из files_read (без номеров строк)" if вхождений == 0
                   else "возьмите фрагмент длиннее, чтобы он был единственным"))
        if путь not in ч["файлы"] and len(ч["файлы"]) >= ПРЕДЕЛ_ЧЕРНОВИКА:
            raise ОшибкаЗапроса(f"в черновике уже {ПРЕДЕЛ_ЧЕРНОВИКА} файлов — больше нельзя")
        новый = текст.replace(было, стало, 1)
        if len(новый) > ПРЕДЕЛ_ЗАПИСИ:
            raise ОшибкаЗапроса(f"файл длиннее {ПРЕДЕЛ_ЗАПИСИ} символов")
        прежний = ч["файлы"].get(путь, {})
        ч["файлы"][путь] = {"новый": bool(прежний.get("новый")), "текст": новый}
        ч["база"] = база()
        _записать_черновик(ч)
    строка = текст[:текст.index(было)].count("\n") + 1
    return {"путь": путь, "изменено_в_черновике": True, "строка": строка,
            "было_строк": было.count("\n") + 1, "стало_строк": стало.count("\n") + 1}


def _diff_файла(путь: str, было: str | None, стало: str) -> str:
    а = (было or "").splitlines(keepends=True)
    б = стало.splitlines(keepends=True)
    куски = list(difflib.unified_diff(а, б, "/dev/null" if было is None else f"a/{путь}", f"b/{путь}", n=3))
    if not куски:
        return ""
    вывод = [f"diff --git a/{путь} b/{путь}\n"] + (["new file mode 100644\n"] if было is None else [])
    for к in куски:
        вывод.append(к if к.endswith("\n") else к + "\n\\ No newline at end of file\n")
    return "".join(вывод)


def diff_черновика(предел: int = ПРЕДЕЛ_DIFF) -> dict[str, Any]:
    ч = прочитать_черновик()
    части, файлы = [], []
    for путь, з in sorted(ч["файлы"].items()):
        было = None if з.get("новый") else в_базе(путь)
        кусок = _diff_файла(путь, было, з["текст"])
        if кусок:
            части.append(кусок)
            файлы.append({"путь": путь, "новый": bool(з.get("новый")),
                          "добавлено": sum(1 for с in кусок.splitlines() if с.startswith("+") and not с.startswith("+++")),
                          "удалено": sum(1 for с in кусок.splitlines() if с.startswith("-") and not с.startswith("---"))})
    текст = "".join(части)
    return {"база": ч.get("база") or база(), "файлов": len(файлы), "файлы": файлы,
            "diff": текст[:предел], "обрезано": len(текст) > предел, "пусто": not текст}


# --- инструменты MCP ------------------------------------------------------------------------

сервер = MCPServer(ИМЯ, title="Файлы проекта (чтение и черновик правок)", version=ВЕРСИЯ,
                   instructions=ИНСТРУКЦИИ, log_level="WARNING")


@сервер.tool(title="Файлы по шаблону", annotations=ЧТЕНИЕ)
def files_list(
    pattern: Annotated[str, Field(description="Шаблон пути (glob): * — внутри каталога, ** — на любую глубину, "
                                              "например backend/**/urls.py или docs/*.md. ** — все файлы.")] = "**",
    limit: Annotated[int, Field(description="Сколько путей вернуть, 1–300.", ge=1, le=ПРЕДЕЛ_ФАЙЛОВ)] = 100,
) -> dict[str, Any]:
    """Файлы проекта (базовый коммит + новые файлы черновика) по шаблону пути."""
    return список(pattern, limit)


@сервер.tool(title="Поиск по файлам", annotations=ЧТЕНИЕ)
def files_search(
    query: Annotated[str, Field(description="Что искать: строка (по умолчанию) или регулярное выражение (regex=true).")],
    regex: Annotated[bool, Field(description="query — регулярное выражение (синтаксис Python / PCRE).")] = False,
    glob: Annotated[str, Field(description="Ограничить файлами по шаблону, например backend/** или *.md. "
                                           "Пусто — весь проект.")] = "",
    word: Annotated[bool, Field(description="Только целые слова (имя функции, а не часть другого имени).")] = False,
    case_sensitive: Annotated[bool, Field(description="Учитывать регистр.")] = True,
    limit: Annotated[int, Field(description="Сколько мест вернуть, 1–200.", ge=1, le=ПРЕДЕЛ_ПОИСКА)] = 100,
) -> dict[str, Any]:
    """Поиск по всем файлам проекта: каждое место — файл, номер строки и текст строки; сводка по файлам."""
    return искать(query, regex, glob, word, case_sensitive, limit)


@сервер.tool(title="Прочитать файл", annotations=ЧТЕНИЕ)
def files_read(
    path: Путь,
    start: Annotated[int, Field(description="С какой строки (с 1).", ge=1)] = 1,
    end: Annotated[int, Field(description="По какую строку включительно; 0 — до конца (не больше 400 строк за раз).",
                              ge=0)] = 0,
) -> dict[str, Any]:
    """Текст файла с номерами строк («номер<TAB>строка»), как он есть с учётом черновика правок."""
    return прочитать(path, start, end)


@сервер.tool(title="Оглавление файла", annotations=ЧТЕНИЕ)
def files_outline(path: Путь) -> dict[str, Any]:
    """Оглавление: функции и классы Python со строками, заголовки markdown, ключи JSON."""
    return оглавление(path)


@сервер.tool(title="История git", annotations=ЧТЕНИЕ)
def files_log(
    path: Annotated[str, Field(description="Файл или шаблон; пусто — весь проект.")] = "",
    limit: Annotated[int, Field(description="Сколько коммитов, 1–30.", ge=1, le=ПРЕДЕЛ_ЖУРНАЛА)] = 20,
) -> dict[str, Any]:
    """Последние коммиты базы: хэш, дата, тема (авторы не выводятся)."""
    return журнал(path, limit)


@сервер.tool(title="Коммит", annotations=ЧТЕНИЕ)
def files_show(commit: Annotated[str, Field(description="Хэш коммита из files_log или HEAD~N.")]) -> dict[str, Any]:
    """Коммит: тема, сводка изменённых файлов и diff (до 20 000 символов)."""
    return коммит(commit)


@сервер.tool(title="Создать файл (черновик)", annotations=ЧЕРНОВИК)
def files_create(
    path: Путь,
    content: Annotated[str, Field(description="Полное содержимое нового файла.")],
) -> dict[str, Any]:
    """Новый файл — в черновик задачи (на диск запишет человек после подтверждения). Существующий — files_edit."""
    return создать(path, content)


@сервер.tool(title="Заменить фрагмент (черновик)", annotations=ЧЕРНОВИК)
def files_edit(
    path: Путь,
    old: Annotated[str, Field(description="Дословный фрагмент файла (без номеров строк), ровно одно вхождение.")],
    new: Annotated[str, Field(description="Чем заменить фрагмент.")],
) -> dict[str, Any]:
    """Правка существующего файла заменой фрагмента — в черновик задачи. Прочитайте файл перед правкой."""
    return заменить(path, old, new)


@сервер.tool(title="Diff черновика", annotations=ЧТЕНИЕ)
def files_draft_diff() -> dict[str, Any]:
    """Единый diff всех правок черновика против базового коммита."""
    return diff_черновика()


def main() -> int:
    разбор = argparse.ArgumentParser(description="MCP-сервер files: чтение, поиск и черновик правок проекта.")
    разбор.add_argument("--корень", default="", metavar="ПУТЬ", help="копия проекта (иначе FILES_ROOT)")
    разбор.add_argument("--база", default="", metavar="КОММИТ", help="базовый коммит (иначе FILES_BASE, HEAD)")
    разбор.add_argument("--черновик", default="", metavar="ФАЙЛ", help="черновик задачи (иначе FILES_DRAFT)")
    разбор.add_argument("--http", type=int, default=0, metavar="ПОРТ", help="Streamable HTTP на 127.0.0.1:ПОРТ/mcp")
    а = разбор.parse_args()
    if а.корень:
        os.environ["FILES_ROOT"] = os.path.abspath(os.path.expanduser(а.корень))
    if а.база:
        os.environ["FILES_BASE"] = а.база
    if а.черновик:
        os.environ["FILES_DRAFT"] = os.path.abspath(а.черновик)
    корень()
    if а.http:
        сервер.run("streamable-http", host="127.0.0.1", port=а.http)
    else:
        сервер.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
