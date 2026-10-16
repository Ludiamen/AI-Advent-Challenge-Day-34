"""Рабочая копия проекта для ассистента файлов (День 34, spec FR-01, FR-05, NFR-02; plan D-01, D-05).

Ассистент никогда не пишет в рабочую копию владельца: у него свой клон (`git clone --local
--no-hardlinks`), самостоятельный репозиторий — в `.git` оригинала ничего не попадает
(`git worktree` записал бы туда служебные файлы). Клон стоит на закреплённом базовом коммите.

Черновик правок проверяется и применяется **через временный индекс**: `read-tree` базы во
временный файл индекса → `git apply --cached` → `write-tree` → `commit-tree` с родителем-базой
→ ветка `assistant/<id>`. Рабочий каталог клона для проверки не нужен, поэтому проверка не
зависит от того, на какой ветке клон стоит. После коммита клон переключается на ветку задачи:
файлы на диске — те, что подтвердил человек.

Дата и автор коммитов фиксированы для ветки случаев (тот же вход — тот же хэш, plan D-10) и
задаются явно для коммитов ассистента.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from typing import Any

АВТОР = ("Ассистент файлов", "files-assistant@example.invalid")
ДАТА_СЛУЧАЕВ = "2026-10-16T12:00:00+03:00"
ТАЙМАУТ_С = 120.0


class ОшибкаКопии(RuntimeError):
    pass


def _окружение(**доп: str) -> dict[str, str]:
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C.UTF-8", GIT_PAGER="cat", **доп)


def _git(каталог: str, *аргументы: str, вход: str | None = None, окружение: dict[str, str] | None = None) -> str:
    try:
        итог = subprocess.run(["git", "-c", "core.fsmonitor=", "-c", "color.ui=never", "-C", каталог, *аргументы],
                              input=вход, capture_output=True, text=True, timeout=ТАЙМАУТ_С,
                              env=окружение or _окружение())
    except subprocess.TimeoutExpired as e:
        raise ОшибкаКопии(f"git {аргументы[0]}: нет ответа {ТАЙМАУТ_С:.0f} с") from e
    if итог.returncode != 0:
        raise ОшибкаКопии(f"git {аргументы[0]}: {(итог.stderr or итог.stdout).strip()[:400]}")
    return итог.stdout


def снимок_оригинала(путь: str) -> dict[str, str]:
    """HEAD и отпечаток статуса рабочей копии владельца — только чтение, без блокировок индекса."""
    env = _окружение(GIT_OPTIONAL_LOCKS="0")
    голова = _git(путь, "rev-parse", "HEAD", окружение=env).strip()
    статус = _git(путь, "status", "--porcelain=v1", "-uall", окружение=env)
    return {"HEAD": голова, "статус": hashlib.sha256(статус.encode("utf-8")).hexdigest()[:16],
            "изменений": str(len([с for с in статус.splitlines() if с.strip()]))}


class Копия:
    def __init__(self, проект: str, оригинал: str, база: str, каталог: str) -> None:
        self.проект = проект
        self.оригинал = os.path.abspath(os.path.expanduser(оригинал))
        self.база = база
        self.каталог = os.path.abspath(os.path.expanduser(каталог))

    def git(self, *аргументы: str, **kw: Any) -> str:
        return _git(self.каталог, *аргументы, **kw)

    # --- клон ------------------------------------------------------------------------------

    def подготовить(self) -> str:
        """Клон есть и стоит на базе (отделённый HEAD, чистый рабочий каталог). Вернуть полный хэш базы."""
        if not os.path.isdir(os.path.join(self.оригинал, ".git")):
            raise ОшибкаКопии(f"оригинал проекта — не репозиторий git: {self.оригинал}")
        if not os.path.isdir(os.path.join(self.каталог, ".git")):
            os.makedirs(os.path.dirname(self.каталог), exist_ok=True)
            _git(os.path.dirname(self.каталог), "clone", "-q", "--local", "--no-hardlinks", "--no-checkout",
                 self.оригинал, self.каталог)
        полный = self.хэш(self.база)
        if not полный:
            # Базы ещё нет в клоне — оригинал ушёл вперёд: забрать коммиты (пишется только клон).
            self.git("fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
            полный = self.хэш(self.база)
        if not полный:
            raise ОшибкаКопии(f"коммита «{self.база}» нет ни в клоне, ни в оригинале")
        self.git("checkout", "-q", "--detach", "--force", полный)
        self.git("clean", "-q", "-fd")
        return полный

    def хэш(self, ссылка: str) -> str:
        try:
            return self.git("rev-parse", "--verify", "--quiet", f"{ссылка}^{{commit}}").strip()
        except ОшибкаКопии:
            return ""

    # --- черновик ----------------------------------------------------------------------------

    def _индекс(self, база: str) -> tuple[str, dict[str, str]]:
        fd, путь = tempfile.mkstemp(prefix="files-index-")
        os.close(fd)
        os.unlink(путь)
        env = _окружение(GIT_INDEX_FILE=путь)
        self.git("read-tree", база, окружение=env)
        return путь, env

    def проверить(self, diff: str, база: str = "") -> str:
        """Пусто — diff применяется к базе; иначе — причина (текст git)."""
        if not diff.strip():
            return "diff пуст"
        путь, env = self._индекс(база or self.база)
        try:
            self.git("apply", "--cached", "--check", "--whitespace=nowarn", "-", вход=diff, окружение=env)
            return ""
        except ОшибкаКопии as e:
            return str(e)
        finally:
            if os.path.exists(путь):
                os.unlink(путь)

    def применить(self, diff: str, ветка: str, сообщение: str, база: str = "") -> str:
        """Коммит diff на новой ветке от базы; клон переключается на неё. Вернуть хэш коммита."""
        база = self.хэш(база or self.база)
        if not база:
            raise ОшибкаКопии("базы нет в клоне — подготовить() копию")
        if self.хэш(f"refs/heads/{ветка}"):
            raise ОшибкаКопии(f"ветка {ветка} уже есть — задача применена раньше")
        путь, env = self._индекс(база)
        try:
            self.git("apply", "--cached", "--whitespace=nowarn", "-", вход=diff, окружение=env)
            дерево = self.git("write-tree", окружение=env).strip()
        finally:
            if os.path.exists(путь):
                os.unlink(путь)
        автор = dict(GIT_AUTHOR_NAME=АВТОР[0], GIT_AUTHOR_EMAIL=АВТОР[1],
                     GIT_COMMITTER_NAME=АВТОР[0], GIT_COMMITTER_EMAIL=АВТОР[1])
        коммит = self.git("commit-tree", дерево, "-p", база, "-F", "-", вход=сообщение,
                          окружение=_окружение(**автор)).strip()
        self.git("update-ref", f"refs/heads/{ветка}", коммит, "")
        self.git("checkout", "-q", "--force", ветка)
        return коммит

    # --- ветка случаев --------------------------------------------------------------------------

    def построить_случаи(self, случаи: dict[str, Any], база: str = "") -> str:
        """Ветка посеянных случаев (files-cases.json) от базы: по коммиту на группу «коммиты» (сообщения —
        обычные, без подсказок модели), дата и автор фиксированы — тот же вход даёт тот же хэш. Вернуть
        хэш последнего коммита."""
        родитель = self.хэш(база or self.база)
        коммиты = случаи.get("коммиты") or [{"id": "", "сообщение": "Правки"}]
        автор = dict(GIT_AUTHOR_NAME=АВТОР[0], GIT_AUTHOR_EMAIL=АВТОР[1], GIT_AUTHOR_DATE=ДАТА_СЛУЧАЕВ,
                     GIT_COMMITTER_NAME=АВТОР[0], GIT_COMMITTER_EMAIL=АВТОР[1], GIT_COMMITTER_DATE=ДАТА_СЛУЧАЕВ)
        for к in коммиты:
            свои = [с for с in случаи["случаи"] if (с.get("коммит") or "") == к["id"]]
            if not свои:
                continue
            путь, env = self._индекс(родитель)
            try:
                for с in свои:
                    объект = self.git("hash-object", "-w", "--stdin", вход=с["создать"]).strip()
                    self.git("update-index", "--add", "--cacheinfo", f"100644,{объект},{с['файл']}", окружение=env)
                дерево = self.git("write-tree", окружение=env).strip()
            finally:
                if os.path.exists(путь):
                    os.unlink(путь)
            родитель = self.git("commit-tree", дерево, "-p", родитель, "-F", "-", вход=к["сообщение"],
                                окружение=_окружение(**автор)).strip()
        self.git("update-ref", f"refs/heads/{случаи['ветка']}", родитель)
        return родитель


def из_проекта(имя: str = "crm", путь_конфига: str = "") -> Копия:
    """Копия по projects.json: «путь» — оригинал, «файлы.база» — коммит, «файлы.копия» — каталог
    клона внутри памяти (MEMORY_DIR, по умолчанию memory/): у проверок — своя копия (plan D-01)."""
    from agent.devassist import project
    файл = project.путь_конфига(путь_конфига)
    with open(файл, encoding="utf-8") as f:
        описание = json.load(f)["проекты"].get(имя)
    if not описание or "файлы" not in описание:
        raise ОшибкаКопии(f"у проекта «{имя}» в {файл} нет раздела «файлы» (база, копия)")
    п = project.загрузить(имя, путь_конфига)
    память = os.path.abspath(os.getenv("MEMORY_DIR") or os.path.join(os.path.dirname(файл), "memory"))
    каталог = os.path.join(память, описание["файлы"].get("копия") or f"files/{имя}")
    return Копия(имя, п.путь, описание["файлы"]["база"], каталог)
