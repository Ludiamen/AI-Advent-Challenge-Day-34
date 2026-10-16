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
        случаи = {"ветка": "cases/t",
                  "коммиты": [{"id": "а", "сообщение": "Новые модули"}, {"id": "б", "сообщение": "tools: проверка"}],
                  "случаи": [{"id": "V1", "файл": "app/bad.py", "создать": "x = 1\n", "коммит": "а"},
                             {"id": "D1", "файл": "tools/check.py", "создать": "print(1)\n", "коммит": "б"}]}
        первый = к.построить_случаи(случаи)
        второй = к.построить_случаи(случаи)
        self.assertEqual(первый, второй)                   # фиксированная дата и автор — тот же хэш
        self.assertEqual(git(self.копия_путь, "show", "cases/t:app/bad.py"), "x = 1\n")
        self.assertEqual(git(self.копия_путь, "log", "--format=%s", f"{self.база}..cases/t").splitlines(),
                         ["tools: проверка", "Новые модули"])
        self.assertEqual(git(self.копия_путь, "show", "--name-only", "--format=", "cases/t").split(), ["tools/check.py"])


class _Сервер(_Проект):
    """Копия + окружение сервера files (корень, база, черновик) на время теста."""

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


class СерверФайлов(_Сервер):
    """spec FR-02, NFR-01; plan D-02: чтение из базы + черновик, границы, черновик не пишет на диск."""

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


class _СКопией(_Сервер):
    """Копия + сервер + хранилище задач во временной памяти."""

    def setUp(self) -> None:
        super().setUp()
        from agent.files import task
        self.хр = task.Хранилище(os.path.join(self.tmp.name, "memory", "files"))


class ЗадачиФайлов(_СКопией):
    """spec FR-05, FR-09; plan D-05, D-08: статусы, применение, отклонение, сравнение повторов."""

    def test_номера_сохранение_список(self) -> None:
        з1 = self.хр.новая("цель один", "t", self.база, "ds-flash")
        з2 = self.хр.новая("цель два", "t", self.база, "ds-flash")
        self.assertEqual((з1.id, з2.id), (1, 2))
        з1.итог = "готово"
        self.хр.сохранить(з1)
        self.assertEqual(self.хр.загрузить(1).итог, "готово")
        self.assertEqual([з.id for з in self.хр.список()], [2, 1])
        with self.assertRaises(KeyError):
            self.хр.загрузить(99)

    def _с_черновиком(self):
        з = self.хр.новая("добавь app/ в README", "t", self.база, "ds-flash")
        os.environ["FILES_DRAFT"] = self.хр.черновик(з.id)
        self.f.заменить("README.md", "| docs/ | документы |", "| docs/ | документы |\n| app/ | код |")
        з.diff = self.f.diff_черновика()["diff"]
        з.статус = "ждёт подтверждения"
        self.хр.сохранить(з)
        return з

    def test_применить(self) -> None:
        з = self._с_черновиком()
        з = self.хр.применить(з.id, self.к, кто="владелец")
        self.assertEqual(з.статус, "применена")
        self.assertEqual(з.ветка, f"assistant/{з.id}")
        self.assertIn("| app/ | код |", git(self.копия_путь, "show", f"{з.коммит}:README.md"))
        self.assertIn("добавь app/ в README", git(self.копия_путь, "log", "-1", "--format=%B", з.коммит))
        with self.assertRaises(ValueError):
            self.хр.применить(з.id, self.к)
        with self.assertRaises(ValueError):
            self.хр.отклонить(з.id, "поздно")

    def test_отклонить_ничего_не_меняет(self) -> None:
        з = self._с_черновиком()
        до = git(self.копия_путь, "rev-parse", "HEAD")
        з = self.хр.отклонить(з.id, "не то", кто="владелец")
        self.assertEqual((з.статус, з.решение["причина"]), ("отклонена", "не то"))
        self.assertEqual(git(self.копия_путь, "rev-parse", "HEAD"), до)
        self.assertEqual(git(self.копия_путь, "branch", "--list", "assistant/*"), "")
        with self.assertRaises(ValueError):
            self.хр.применить(з.id, self.к)

    def test_без_черновика_применять_нечего(self) -> None:
        з = self.хр.новая("найди", "t", self.база, "ds-flash")
        з.статус = "готово"
        self.хр.сохранить(з)
        with self.assertRaises(ValueError):
            self.хр.применить(з.id, self.к)

    def test_сравнение_повторов(self) -> None:
        from agent.files import task
        а = task.Задача(1, "ц", "t", self.база, "m", изменены=["README.md", "docs/a.md"],
                        находки=[{"файл": "a.py", "строка": 1, "подтверждена": True}])
        б = task.Задача(2, "ц", "t", self.база, "m", изменены=["README.md"],
                        находки=[{"файл": "a.py", "строка": 1, "подтверждена": True},
                                 {"файл": "b.py", "строка": 2, "подтверждена": True}])
        с = task.сравнить(а, б)
        self.assertEqual(с["файлы_совпадение"], 0.5)
        self.assertEqual(с["находки_совпадение"], 0.5)


class ПроверкаКодом(_СКопией):
    """spec FR-04; plan D-04: находки — файл, строка, дословная цитата; черновик применим; ADR по правилам."""

    def test_находки(self) -> None:
        from agent.files import check
        находки = check.находки([
            {"файл": "app/views.py", "строка": 1, "цитата": "from app.service import scan_file"},
            {"файл": "app/views.py", "строка": 3, "цитата": "return scan_file(p)"},   # строка 5, рядом
            {"файл": "app/views.py", "строка": 1, "цитата": "from app.service import scan_file"},  # дубль
            {"файл": "app/views.py", "строка": 2, "цитата": "import os"},             # выдумка
            {"файл": "нет/файла.py", "строка": 1, "цитата": "x"},
            {"файл": ".env", "строка": 1, "цитата": "SECRET_KEY"},
        ])
        self.assertEqual(len(находки), 5)
        self.assertEqual([н["подтверждена"] for н in находки], [True, True, False, False, False])
        self.assertEqual(находки[1]["строка"], 5)                    # поправлена по цитате
        self.assertIn("строка поправлена", находки[1]["пометка"])

    def test_черновик(self) -> None:
        from agent.files import check
        self.f.заменить("docs/api.md", "проверяет файл.", "проверяет файл антивирусом.")
        д = self.f.diff_черновика()
        отчёт = check.черновик(д, self.к, self.база, прочитаны=["README.md"])
        self.assertTrue(отчёт["применяется"])
        self.assertEqual(отчёт["без_чтения"], ["docs/api.md"])
        self.assertEqual(check.черновик({"diff": "", "файлы": []}, self.к, self.база, [])["применяется"], False)

    def test_adr(self) -> None:
        from agent.files import check
        хороший = ("---\nid: ADR-0012\nвид: adr\nстатус: черновик\nверсия: 1\nзависит_от: ADR-0001\n"
                   "подпись: ожидает утверждения владельца\n---\n\n# ADR-0012: Проверка файлов\n\n"
                   "## Контекст\nСм. `app/service.py`.\n\n## Решение\nДа.\n\n## Альтернативы\nНет.\n\n"
                   "## Последствия\nСм. `app/нет.py`.\n")
        замечания = check.adr("docs/decisions/0012-x.md", хороший, существует=lambda п: п == "app/service.py")
        self.assertEqual(замечания, ["ссылка на несуществующий файл: app/нет.py"])
        плохой = "# ADR без шапки\n\n## Решение\nДа.\n"
        замечания = check.adr("docs/decisions/0013-y.md", плохой, существует=lambda п: True)
        self.assertTrue(any("frontmatter" in з for з in замечания))
        self.assertTrue(any("Контекст" in з for з in замечания))


class ЗаглушкаМодели:
    """Отвечает по сценарию: список Reply; запоминает сообщения каждого вызова."""

    def __init__(self, ответы):
        self.ответы = list(ответы)
        self.вызовы = []

    def call(self, модель, сообщения, **kw):
        from agent.llm import LLMError
        self.вызовы.append({"сообщения": [dict(м) for м in сообщения], "инструменты": kw.get("tools")})
        if not self.ответы:
            raise LLMError("сценарий заглушки кончился")
        о = self.ответы.pop(0)
        if isinstance(о, Exception):
            raise о
        return о


def ответ(текст="", вызовы=()):
    from agent.llm import Reply, ToolCall
    тк = [ToolCall(f"c{i}", имя, арг) for i, (имя, арг) in enumerate(вызовы)]
    сообщение = {"role": "assistant", "content": текст,
                 "tool_calls": [{"id": в.ид, "type": "function",
                                 "function": {"name": в.имя, "arguments": json.dumps(в.аргументы, ensure_ascii=False)}}
                                for в in тк]} if тк else {"role": "assistant", "content": текст}
    return Reply(текст, "ds-flash", prompt_tokens=1000, completion_tokens=100, cost=0.001, tool_calls=тк,
                 message=сообщение)


class АссистентФайлов(_СКопией):
    """spec FR-03, FR-10, FR-11; plan D-03: цикл с инструментами на заглушке модели."""

    def ассистент(self, ответы, **kw):
        from agent.files.assistant import Ассистент
        self.модель = ЗаглушкаМодели(ответы)
        return Ассистент(self.к, self.хр, клиент=self.модель, модель="ds-flash", **kw)

    ИТОГ = json.dumps({"итог": "scan_file вызывается в двух местах; документ дополнен",
                       "находки": [{"файл": "app/views.py", "строка": 5, "цитата": "return scan_file(p)", "суть": "вызов"},
                                   {"файл": "app/service.py", "строка": 6, "цитата": "return scan_file(path)", "суть": "вызов"}],
                       "правки": "docs/api.md: уточнено описание"}, ensure_ascii=False)

    def test_цель_до_черновика(self) -> None:
        а = self.ассистент([
            ответ(вызовы=[("files_search", {"query": "scan_file", "word": True})]),
            ответ(вызовы=[("files_read", {"path": "app/views.py"}), ("files_read", {"path": "docs/api.md"})]),
            ответ(вызовы=[("files_edit", {"path": "docs/api.md", "old": "проверяет файл.",
                                          "new": "проверяет файл (вызывается из app/views.py)."})]),
            ответ(self.ИТОГ),
        ])
        з = а.выполнить("Найди, где проверяются файлы, и поправь документацию")
        self.assertEqual(з.статус, "ждёт подтверждения")
        self.assertEqual(з.круги, 4)
        self.assertEqual(sorted(з.прочитаны), ["app/views.py", "docs/api.md"])
        self.assertEqual(з.изменены, ["docs/api.md"])
        self.assertTrue(з.проверка["применяется"])
        self.assertEqual([н["подтверждена"] for н in з.находки], [True, True])
        self.assertEqual(з.вызовы[0]["имя"], "files_search")
        self.assertGreater(з.вызовы[1]["размер"], 0)                    # журнал: размер, без содержимого
        self.assertNotIn("from app.service", json.dumps(з.вызовы, ensure_ascii=False))
        self.assertTrue(os.path.exists(self.хр.черновик(з.id)))
        система = self.модель.вызовы[0]["сообщения"][0]["content"]
        self.assertNotIn("app/views.py", система)                       # файлы модель находит сама
        self.assertIn("[осталось кругов:", self.модель.вызовы[1]["сообщения"][-1]["content"])
        from agent.files import metrics
        строка = metrics.прочитать(self.хр.метрики)[-1]
        self.assertEqual((строка["итог"], строка["изменено"], строка["прочитано"]), ("правки", 1, 2))
        self.assertNotIn("цель", строка)

    def test_только_отчёт(self) -> None:
        а = self.ассистент([ответ(вызовы=[("files_search", {"query": "scan_file"})]), ответ(self.ИТОГ)])
        з = а.выполнить("Найди все использования scan_file")
        self.assertEqual((з.статус, з.diff), ("готово", ""))

    def test_предел_кругов(self) -> None:
        а = self.ассистент([ответ(вызовы=[("files_list", {"pattern": "**"})])] * 3 + [ответ(self.ИТОГ)], кругов=3)
        з = а.выполнить("цель")
        self.assertEqual(з.круги, 3)
        self.assertIn("предел кругов", " ".join(з.пометки))
        self.assertIsNone(self.модель.вызовы[-1]["инструменты"])          # последний — без инструментов

    def test_ошибка_инструмента_видна_модели(self) -> None:
        а = self.ассистент([ответ(вызовы=[("files_read", {"path": ".env"})]), ответ(self.ИТОГ)])
        з = а.выполнить("цель")
        tool = [м for м in self.модель.вызовы[1]["сообщения"] if м.get("role") == "tool"][0]
        self.assertIn("закрыт", tool["content"])
        self.assertFalse(з.вызовы[0]["ок"])

    def test_облако_недоступно(self) -> None:
        from agent.llm import LLMError
        а = self.ассистент([LLMError("503 unavailable")] * 3)
        з = а.выполнить("цель")
        self.assertEqual(з.статус, "не выполнено")
        self.assertIn("503", з.итог)
        self.assertEqual(з.diff, "")

    def test_повторить(self) -> None:
        а = self.ассистент([ответ(вызовы=[("files_search", {"query": "scan_file"})]), ответ(self.ИТОГ)] * 2)
        з1 = а.выполнить("Найди все использования scan_file")
        з2 = а.повторить(з1.id)
        self.assertEqual((з2.цель, з2.база, з2.повтор_от), (з1.цель, з1.база, з1.id))
        self.assertEqual(з2.сравнение["находки_совпадение"], 1.0)
