"""Тесты Дня 34: ассистент для работы с файлами проекта (specs/034-file-assistant).

Рабочая копия и сервер files проверяются на временных репозиториях git — CRM не нужна;
ассистент — на заглушке модели. Живые прогоны на ds-flash — в check_files.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

КОРЕНЬ = os.path.dirname(os.path.abspath(__file__))


def git(каталог: str, *аргументы: str) -> str:
    return subprocess.run(["git", "-C", каталог, *аргументы], capture_output=True, text=True, check=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="т", GIT_AUTHOR_EMAIL="t@example.invalid",
                                   GIT_COMMITTER_NAME="т", GIT_COMMITTER_EMAIL="t@example.invalid")).stdout


def оригинал(каталог: str) -> str:
    """Маленький «проект»: README, код, документ; два коммита. Вернуть хэш второго."""
    os.makedirs(каталог)
    git(каталог, "init", "-q", "-b", "main")
    файлы = {
        "README.md": "# Проект\n\n## Где что лежит\n\n| Путь | Что |\n|---|---|\n| docs/ | документы |\n",
        "app/service.py": "def scan_file(path):\n    return True\n\n\ndef upload(path):\n    return scan_file(path)\n",
        "app/views.py": "from app.service import scan_file\n\n\ndef view(p):\n    return scan_file(p)\n",
        "docs/api.md": "# API\n\nФункция `scan_file` проверяет файл.\n",
        ".env": "SECRET_KEY=очень-секретно\n",
    }
    for путь, текст in файлы.items():
        os.makedirs(os.path.dirname(os.path.join(каталог, путь)) or каталог, exist_ok=True)
        with open(os.path.join(каталог, путь), "w", encoding="utf-8") as f:
            f.write(текст)
    git(каталог, "add", "-A")
    git(каталог, "commit", "-q", "-m", "первый")
    with open(os.path.join(каталог, "tools.py"), "w", encoding="utf-8") as f:
        f.write("print('инструмент')\n")
    git(каталог, "add", "-A")
    git(каталог, "commit", "-q", "-m", "второй")
    return git(каталог, "rev-parse", "HEAD").strip()


class _Проект(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ориг = os.path.join(self.tmp.name, "orig")
        self.база = оригинал(self.ориг)
        # Незакоммиченная работа «другой сессии» в оригинале — её трогать нельзя.
        with open(os.path.join(self.ориг, "app", "service.py"), "a", encoding="utf-8") as f:
            f.write("# правка другой сессии\n")
        self.копия_путь = os.path.join(self.tmp.name, "copy")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def копия(self):
        from agent.files import workspace
        к = workspace.Копия("t", self.ориг, self.база, self.копия_путь)
        к.подготовить()
        return к


class РабочаяКопия(_Проект):
    """spec FR-01, FR-05, NFR-02; plan D-01, D-05."""

    def test_клон_на_базе_и_оригинал_не_тронут(self) -> None:
        from agent.files import workspace
        до = workspace.снимок_оригинала(self.ориг)
        к = self.копия()
        self.assertEqual(git(self.копия_путь, "rev-parse", "HEAD").strip(), self.база)
        self.assertEqual(git(self.копия_путь, "status", "--porcelain"), "")
        with open(os.path.join(self.копия_путь, "app", "service.py"), encoding="utf-8") as f:
            self.assertNotIn("правка другой сессии", f.read())
        к.подготовить()                                   # повторно — без ошибок, тот же HEAD
        self.assertEqual(workspace.снимок_оригинала(self.ориг), до)

    def test_неизвестная_база(self) -> None:
        from agent.files import workspace
        with self.assertRaises(workspace.ОшибкаКопии):
            workspace.Копия("t", self.ориг, "0" * 40, self.копия_путь).подготовить()

    def _diff(self) -> str:
        return ("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
                "@@ -6,3 +6,4 @@\n | Путь | Что |\n |---|---|\n | docs/ | документы |\n+| app/ | код |\n")

    def test_проверка_и_применение(self) -> None:
        from agent.files import workspace
        до = workspace.снимок_оригинала(self.ориг)
        к = self.копия()
        self.assertEqual(к.проверить(self._diff()), "")
        self.assertNotEqual(к.проверить(self._diff().replace("| docs/ | документы |", "| нет | такой |")), "")
        коммит = к.применить(self._diff(), "assistant/1", "Ассистент: README")
        self.assertEqual(git(self.копия_путь, "rev-parse", "assistant/1").strip(), коммит)
        self.assertEqual(git(self.копия_путь, "rev-parse", "assistant/1^").strip(), self.база)
        self.assertIn("| app/ | код |", git(self.копия_путь, "show", "assistant/1:README.md"))
        with open(os.path.join(self.копия_путь, "README.md"), encoding="utf-8") as f:
            self.assertIn("| app/ | код |", f.read())
        self.assertIn("Ассистент: README", git(self.копия_путь, "log", "-1", "--format=%s", "assistant/1"))
        self.assertEqual(workspace.снимок_оригинала(self.ориг), до)

    def test_неприменимый_diff_не_коммитится(self) -> None:
        from agent.files import workspace
        к = self.копия()
        плохой = self._diff().replace("| docs/ | документы |", "| нет | такой |")
        with self.assertRaises(workspace.ОшибкаКопии):
            к.применить(плохой, "assistant/2", "x")
        self.assertEqual(git(self.копия_путь, "branch", "--list", "assistant/2"), "")

    def test_ветка_случаев_воспроизводима(self) -> None:
        к = self.копия()
        случаи = {"ветка": "cases/t", "случаи": [{"id": "V1", "файл": "app/bad.py", "создать": "x = 1\n"}]}
        первый = к.построить_случаи(случаи)
        второй = к.построить_случаи(случаи)
        self.assertEqual(первый, второй)                   # фиксированная дата и автор — тот же хэш
        self.assertEqual(git(self.копия_путь, "show", "cases/t:app/bad.py"), "x = 1\n")


class СерверФайлов(_Проект):
    """spec FR-02, NFR-01; plan D-02: чтение из базы + черновик, границы, черновик не пишет на диск."""

    def setUp(self) -> None:
        super().setUp()
        self.к = self.копия()
        self.черновик = os.path.join(self.tmp.name, "draft.json")
        self.env = {"FILES_ROOT": self.копия_путь, "FILES_BASE": self.база, "FILES_DRAFT": self.черновик}
        self._было = {к: os.environ.get(к) for к in self.env}
        os.environ.update(self.env)
        import files_server
        self.f = files_server

    def tearDown(self) -> None:
        for к, в in self._было.items():
            if в is None:
                os.environ.pop(к, None)
            else:
                os.environ[к] = в
        super().tearDown()

    def test_список_и_закрытые(self) -> None:
        файлы = self.f.список("**")["файлы"]
        self.assertIn("app/service.py", файлы)
        self.assertNotIn(".env", файлы)                       # закрытый не показывается
        self.assertEqual(self.f.список("app/*.py")["файлы"], ["app/service.py", "app/views.py"])
        self.assertEqual(self.f.список("**/*.md")["всего"], 2)

    def test_границы_путей(self) -> None:
        for плохой in ("../orig/README.md", "/etc/passwd", ".env", ".git/config", "app/../.env", "~/x", "a\0b",
                       "app/*.py"):
            with self.subTest(плохой=плохой), self.assertRaises(self.f.ОшибкаЗапроса):
                self.f.прочитать(плохой)
        with self.assertRaises(self.f.ОшибкаЗапроса):
            self.f.создать(".env.local", "SECRET=1\n")

    def test_чтение_из_базы_а_не_с_диска(self) -> None:
        with open(os.path.join(self.копия_путь, "README.md"), "w", encoding="utf-8") as f:
            f.write("подменено на диске\n")
        р = self.f.прочитать("README.md", 1, 1)
        self.assertEqual(р["текст"], "1\t# Проект")
        self.assertEqual(р["всего_строк"], 7)

    def test_поиск(self) -> None:
        р = self.f.искать("scan_file", слово=True)
        self.assertEqual(р["файлов"], 3)
        self.assertEqual(р["мест"], 5)
        self.assertIn({"файл": "app/views.py", "строка": 1, "текст": "from app.service import scan_file"}, р["места"])
        self.assertEqual(self.f.искать("scan_file", шаблон="docs/**")["файлов"], 1)
        р = self.f.искать(r"(?:def|class)\s+scan_\w+", регулярка=True)  # PCRE, как правила конституции
        self.assertEqual(р["мест"], 1)
        self.assertEqual(self.f.искать("SECRET_KEY")["мест"], 0)        # .env не ищется
        with self.assertRaises(self.f.ОшибкаЗапроса):
            self.f.искать("(", регулярка=True)

    def test_оглавление(self) -> None:
        self.assertEqual([п["имя"] for п in self.f.оглавление("app/service.py")["пункты"]], ["scan_file", "upload"])
        self.assertEqual([п["имя"] for п in self.f.оглавление("README.md")["пункты"]], ["Проект", "Где что лежит"])

    def test_черновик_не_пишет_на_диск_и_diff_применяется(self) -> None:
        self.f.заменить("README.md", "| docs/ | документы |", "| docs/ | документы |\n| app/ | код |")
        self.f.создать("docs/adr-0001.md", "# ADR-0001\n\nКонтекст.")
        with open(os.path.join(self.копия_путь, "README.md"), encoding="utf-8") as f:
            self.assertNotIn("app/", f.read())                     # на диске — без правки
        self.assertFalse(os.path.exists(os.path.join(self.копия_путь, "docs/adr-0001.md")))
        self.assertIn("| app/ | код |", self.f.прочитать("README.md")["текст"])   # модель видит правку
        self.assertTrue(self.f.прочитать("README.md")["из_черновика"])
        self.assertIn("docs/adr-0001.md", self.f.список("docs/*")["файлы"])
        д = self.f.diff_черновика()
        self.assertEqual([ф["путь"] for ф in д["файлы"]], ["README.md", "docs/adr-0001.md"])
        self.assertEqual(self.к.проверить(д["diff"]), "")
        коммит = self.к.применить(д["diff"], "assistant/7", "проба")
        self.assertIn("Контекст.", git(self.копия_путь, "show", f"{коммит}:docs/adr-0001.md"))

    def test_правка_ровно_одно_вхождение(self) -> None:
        with self.assertRaisesRegex(self.f.ОшибкаЗапроса, "0 раз"):
            self.f.заменить("app/service.py", "нет такого", "x")
        with self.assertRaisesRegex(self.f.ОшибкаЗапроса, "2 раз"):
            self.f.заменить("app/service.py", "scan_file(path)", "x")
        with self.assertRaisesRegex(self.f.ОшибкаЗапроса, "уже есть"):
            self.f.создать("README.md", "x")
        with self.assertRaises(self.f.ОшибкаЗапроса):
            self.f.заменить("нет/файла.py", "a", "b")

    def test_правки_накапливаются_в_одном_файле(self) -> None:
        self.f.заменить("app/service.py", "def upload(path):", "def upload(path, user):")
        self.f.заменить("app/service.py", "return scan_file(path)", "return scan_file(path) and user")
        д = self.f.diff_черновика()
        self.assertEqual(д["файлов"], 1)
        self.assertEqual(self.к.проверить(д["diff"]), "")

    def test_журнал_и_коммит(self) -> None:
        ж = self.f.журнал("", 5)["журнал"]
        self.assertEqual([к["тема"] for к in ж], ["второй", "первый"])
        self.assertIn("tools.py", self.f.коммит(ж[0]["коммит"])["сводка"])
        with self.assertRaises(self.f.ОшибкаЗапроса):
            self.f.коммит("--all")

    def test_через_mcp(self) -> None:
        import anyio
        from mcp import Client

        async def работа():
            async with Client(self.f.сервер) as к:
                имена = sorted(и.name for и in (await к.list_tools()).tools)
                р = await к.call_tool("files_read", {"path": ".env"})
                return имена, р.is_error
        имена, ошибка = anyio.run(работа)
        self.assertEqual(имена, ["files_create", "files_draft_diff", "files_edit", "files_list", "files_log",
                                 "files_outline", "files_read", "files_search", "files_show"])
        self.assertTrue(ошибка)
