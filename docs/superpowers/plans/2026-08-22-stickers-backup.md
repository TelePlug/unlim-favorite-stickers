# Бекап избранных стикеров — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** экспорт избранного двумя кнопками в настройках плагина и импорт тапом по файлу `.stickers` в чате.

**Architecture:** данные принадлежат `StickersDB` — она получает четыре метода сериализации, все на чистом Python. Файловая часть (имя, разбор, определение формата) — модульные функции рядом с `serialize_sticker`. UI-часть (`create_settings`, обработчики, диалог) живёт в `MyPlugin`. Перехват тапа — отдельный `ImportBackupHook` на всех перегрузках `AndroidUtilities.openForView`.

**Tech Stack:** Python 3.11 внутри Chaquopy, API плагинов exteraGram (`ui.settings`, `ui.alert`, `ui.bulletin`, `client_utils`, `file_utils`, `android_utils`). Тесты — `tests/test_plugin.py`, свой раннер без pytest.

**Спека:** `docs/superpowers/specs/2026-08-21-stickers-backup-design.md`

---

## Предусловия

- [ ] **PR #14 смержен в main, ветка перебазирована.** План опирается на два его изменения: приведение id аккаунта к строке внутри `StickersDB` и кеш разобранных документов `self.__cache`, который импорт обязан сбрасывать. Проверить: `grep -n "__cache" unlim_favorite_stickers.py` должен найти шесть вхождений.

## Структура файлов

Плагин обязан оставаться одним `.py`: `.plugin` — это один файл, и релизный workflow копирует именно его. Разделение логическое.

| Файл | Что меняется |
|---|---|
| `unlim_favorite_stickers.py` | константы бекапа и `BackupError`; функции `backup_filename` / `parse_backup` / `detect_scope`; шесть методов `StickersDB`; `ImportBackupHook`; `create_settings` и обработчики в `MyPlugin`; регистрация хука |
| `tests/test_plugin.py` | стабы `ui.settings`, `ui.alert`, `client_utils`, `file_utils`; тесты всех перечисленных частей |
| `README.md` | раздел про бекап |

---

### Task 1: Стабы окружения в стенде

Новые импорты в плагине сломают загрузку стенда, поэтому стабы идут первыми.

**Files:**
- Modify: `tests/test_plugin.py`

- [ ] **Step 1: Добавить стабы после класса `JInt`**

```python
class FakeSettingItem:
    """Дataclass-подобные элементы ui.settings: важны поля, не поведение."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeHeader(FakeSettingItem):
    def __init__(self, text):
        super().__init__(text=text)


class FakeDivider(FakeSettingItem):
    def __init__(self, text=""):
        super().__init__(text=text)


class FakeText(FakeSettingItem):
    def __init__(self, text, on_click=None, **kwargs):
        super().__init__(text=text, on_click=on_click, **kwargs)


DIALOGS = []


class FakeAlertDialogBuilder:
    """Запоминает, что показали и какие кнопки повесили."""

    def __init__(self, context):
        self.context = context
        self.title = None
        self.message = None
        self.buttons = {}
        self.shown = False
        DIALOGS.append(self)

    def set_title(self, title):
        self.title = title
        return self

    def set_message(self, message):
        self.message = message
        return self

    def _button(self, kind, text, listener):
        self.buttons[kind] = (text, listener)
        return self

    def set_positive_button(self, text, listener=None):
        return self._button("positive", text, listener)

    def set_neutral_button(self, text, listener=None):
        return self._button("neutral", text, listener)

    def set_negative_button(self, text, listener=None):
        return self._button("negative", text, listener)

    def show(self):
        self.shown = True
        return self

    def dismiss(self):
        return None

    def press(self, kind):
        """Нажать кнопку так, как это сделал бы пользователь."""
        self.buttons[kind][1](self, 0)


SENT_DOCUMENTS = []
CACHE_DIR = tempfile.mkdtemp()


class FakeFragment:
    def getParentActivity(self):
        return object()
```

- [ ] **Step 2: Зарегистрировать модули в `install_stubs`**

Добавить перед `sys.modules.update(...)`:

```python
    ui_settings = types.ModuleType("ui.settings")
    ui_settings.Header = FakeHeader
    ui_settings.Divider = FakeDivider
    ui_settings.Text = FakeText

    ui_alert = types.ModuleType("ui.alert")
    ui_alert.AlertDialogBuilder = FakeAlertDialogBuilder

    client_utils = types.ModuleType("client_utils")
    client_utils.send_document = lambda peer, file_path, caption="": SENT_DOCUMENTS.append(
        (peer, file_path, caption)
    )
    client_utils.get_last_fragment = FakeFragment

    file_utils = types.ModuleType("file_utils")
    file_utils.get_cache_dir = lambda: CACHE_DIR
    file_utils.write_file = lambda path, content: open(path, "w", encoding="utf-8").write(content)
    file_utils.read_file = lambda path: open(path, encoding="utf-8").read()
```

и в сам `sys.modules.update({...})`:

```python
            "ui.settings": ui_settings,
            "ui.alert": ui_alert,
            "client_utils": client_utils,
            "file_utils": file_utils,
```

- [ ] **Step 3: Добавить `run_on_ui_thread` в стаб `android_utils`**

В `install_stubs`, рядом с `android_utils.log`:

```python
    android_utils.run_on_ui_thread = lambda func, delay=0: func()
```

- [ ] **Step 4: Прогнать тесты**

Run: `python tests/test_plugin.py`
Expected: `39/39 прошло` — стабы ничего не ломают, новых тестов ещё нет.

- [ ] **Step 5: Commit**

```bash
git add tests/test_plugin.py
git commit -m "test: стабы настроек, диалогов и файлового API для стенда"
```

---

### Task 2: Константы и импорты бекапа

**Files:**
- Modify: `unlim_favorite_stickers.py` (шапка файла)

- [ ] **Step 1: Дописать импорты**

Заменить блок импортов на:

```python
import datetime as dt
import json
import os

from android_utils import log, run_on_ui_thread
from base_plugin import BasePlugin, MethodHook
from client_utils import get_last_fragment, send_document
from file_utils import get_cache_dir, read_file, write_file
from hook_utils import find_class
from java import jclass, jint
from ui.alert import AlertDialogBuilder
from ui.bulletin import BulletinHelper
from ui.settings import Divider, Header, Text
```

- [ ] **Step 2: Добавить константы после `ArrayList = jclass(...)`**

```python
BACKUP_VERSION = 1
BACKUP_SUFFIX = ".stickers"
BACKUP_CAPTION = "Бекап избранных стикеров"
REQUIRED_STICKER_FIELDS = ("id", "access_hash", "dc_id", "mime_type")


class BackupError(Exception):
    """Причина отказа импорта, пригодная для показа пользователю"""
```

- [ ] **Step 3: Прогнать тесты**

Run: `python tests/test_plugin.py`
Expected: `39/39 прошло` — модуль грузится с новыми импортами.

- [ ] **Step 4: Commit**

```bash
git add unlim_favorite_stickers.py
git commit -m "feat: константы формата бекапа"
```

---

### Task 3: Экспорт из базы

**Files:**
- Modify: `unlim_favorite_stickers.py` (класс `StickersDB`)
- Test: `tests/test_plugin.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в раздел StickersDB:

```python
def test_export_account_keeps_panel_order():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    db.add_sticker(FakeSticker(200), "42")
    data = db.export_account("42")
    assert data["version"] == 1
    assert [s["id"] for s in data["stickers"]] == [200, 100], data["stickers"]


def test_export_account_hides_account_ids():
    """Файл одного аккаунта можно отправить другому человеку."""
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    assert "accounts" not in db.export_account("42")
    assert "42" not in json.dumps(db.export_account("42"))


def test_export_all_is_a_database_snapshot():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    db.add_sticker(FakeSticker(200), "43")
    data = db.export_all()
    assert data["version"] == 1
    assert sorted(data["accounts"]) == ["42", "43"]
    assert sorted(data["stickers"]) == ["100", "200"]


def test_counts_for_settings_screen():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    db.add_sticker(FakeSticker(200), "43")
    assert db.count_stickers("42") == 1
    assert db.count_all() == (2, 2)
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python tests/test_plugin.py`
Expected: четыре `FAIL ... AttributeError: 'StickersDB' object has no attribute 'export_account'`

- [ ] **Step 3: Реализовать**

Добавить в `StickersDB` после `is_sticker_favorite`:

```python
    def count_stickers(self, account) -> int:
        """Сколько стикеров у аккаунта - без разбора, только длина списка"""
        return len(self.__stickers["accounts"].get(str(account), []))

    def count_all(self) -> tuple:
        """(всего стикеров, всего аккаунтов)"""
        return len(self.__stickers["stickers"]), len(self.__stickers["accounts"])

    def export_account(self, account) -> dict:
        """Плоский список в порядке панели, без упоминания аккаунтов"""
        account = str(account)
        return {
            "version": BACKUP_VERSION,
            "stickers": [
                self.__stickers["stickers"][sticker_id]
                for sticker_id in reversed(self.__stickers["accounts"].get(account, []))
                if sticker_id in self.__stickers["stickers"]
            ],
        }

    def export_all(self) -> dict:
        """Слепок базы: восстанавливает все аккаунты по их id"""
        return {
            "version": BACKUP_VERSION,
            "accounts": {
                account: list(ids)
                for account, ids in self.__stickers["accounts"].items()
            },
            "stickers": dict(self.__stickers["stickers"]),
        }
```

- [ ] **Step 4: Прогнать тесты**

Run: `python tests/test_plugin.py`
Expected: `43/43 прошло`

- [ ] **Step 5: Commit**

```bash
git add unlim_favorite_stickers.py tests/test_plugin.py
git commit -m "feat: экспорт избранного из базы в два формата"
```

---

### Task 4: Разбор файла бекапа

**Files:**
- Modify: `unlim_favorite_stickers.py` (модульные функции после `deserialize_sticker`)
- Test: `tests/test_plugin.py`

- [ ] **Step 1: Написать падающие тесты**

```python
def test_parse_backup_accepts_both_formats():
    account_file = json.dumps({"version": 1, "stickers": []})
    all_file = json.dumps({"version": 1, "accounts": {}, "stickers": {}})
    assert plugin.detect_scope(plugin.parse_backup(account_file)) == "account"
    assert plugin.detect_scope(plugin.parse_backup(all_file)) == "all"


def test_parse_backup_rejects_future_version():
    text = json.dumps({"version": 99, "stickers": []})
    try:
        plugin.parse_backup(text)
    except plugin.BackupError as e:
        assert "новой версией" in str(e), str(e)
    else:
        raise AssertionError("будущая версия должна быть отклонена")


def test_parse_backup_rejects_garbage():
    for text in ("не json", json.dumps([1, 2]), json.dumps({"version": 1})):
        try:
            plugin.parse_backup(text)
        except plugin.BackupError:
            continue
        raise AssertionError(f"мусор принят за бекап: {text}")


def test_backup_filename_marks_scope():
    assert plugin.backup_filename("account").endswith(".stickers")
    assert "-all-" in plugin.backup_filename("all")
    assert "-all-" not in plugin.backup_filename("account")
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python tests/test_plugin.py`
Expected: `FAIL ... module 'plugin' has no attribute 'parse_backup'`

- [ ] **Step 3: Реализовать**

Добавить после `deserialize_sticker`:

```python
def backup_filename(scope: str) -> str:
    """Имя файла бекапа: favorites-2026-08-22.stickers"""
    suffix = "-all" if scope == "all" else ""
    return f"favorites{suffix}-{dt.date.today().isoformat()}{BACKUP_SUFFIX}"


def detect_scope(data: dict) -> str:
    """Слепок базы отличается от списка одного аккаунта ключом accounts"""
    return "all" if isinstance(data.get("accounts"), dict) else "account"


def parse_backup(text: str) -> dict:
    """Разбор файла бекапа. На любой брак - BackupError с причиной"""
    try:
        data = json.loads(text)
    except ValueError:
        raise BackupError("Файл не похож на бекап стикеров")
    if not isinstance(data, dict):
        raise BackupError("Файл не похож на бекап стикеров")
    if data.get("version") != BACKUP_VERSION:
        raise BackupError("Файл сделан более новой версией плагина")
    if isinstance(data.get("accounts"), dict) and isinstance(
        data.get("stickers"), dict
    ):
        return data
    if isinstance(data.get("stickers"), list):
        return data
    raise BackupError("Файл не похож на бекап стикеров")
```

- [ ] **Step 4: Прогнать тесты**

Run: `python tests/test_plugin.py`
Expected: `47/47 прошло`

- [ ] **Step 5: Commit**

```bash
git add unlim_favorite_stickers.py tests/test_plugin.py
git commit -m "feat: разбор и опознание файла бекапа"
```

---

### Task 5: Импорт в аккаунт

**Files:**
- Modify: `unlim_favorite_stickers.py` (класс `StickersDB`)
- Test: `tests/test_plugin.py`

- [ ] **Step 1: Написать падающие тесты**

```python
def test_import_roundtrip_keeps_order():
    source, _ = make_db()
    source.add_sticker(FakeSticker(100), "42")
    source.add_sticker(FakeSticker(200), "42")
    target, _ = make_db()
    applied, skipped = target.import_account(source.export_account("42"), "42")
    assert (applied, skipped) == (2, 0)
    assert [doc.id for doc in target.get_all_stickers("42")] == [200, 100]


def test_import_into_another_account():
    source, _ = make_db()
    source.add_sticker(FakeSticker(100), "42")
    target, _ = make_db()
    target.import_account(source.export_account("42"), "999")
    assert len(target.get_all_stickers("999")) == 1
    assert target.get_all_stickers("42") == []


def test_import_merge_is_idempotent():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    backup = db.export_account("42")
    db.add_sticker(FakeSticker(200), "42")
    db.import_account(backup, "42")
    assert [doc.id for doc in db.get_all_stickers("42")] == [200, 100]


def test_import_replace_wipes_previous():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    backup = db.export_account("42")
    db.add_sticker(FakeSticker(200), "42")
    db.import_account(backup, "42", replace=True)
    assert [doc.id for doc in db.get_all_stickers("42")] == [100]


def test_import_replace_drops_orphans():
    """Запись, на которую больше никто не ссылается, не должна копиться."""
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    backup = db.export_account("42")
    db.add_sticker(FakeSticker(200), "42")
    db.import_account(backup, "42", replace=True)
    assert db.count_all() == (1, 1)


def test_import_skips_broken_records():
    db, _ = make_db()
    applied, skipped = db.import_account(
        {"stickers": [
            {"id": 1, "access_hash": 2, "dc_id": 2, "mime_type": "image/webp"},
            {"id": 2, "dc_id": 2},
            "вообще не запись",
        ]},
        "42",
    )
    assert (applied, skipped) == (1, 2)
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python tests/test_plugin.py`
Expected: `FAIL ... has no attribute 'import_account'`

- [ ] **Step 3: Реализовать**

Добавить в `StickersDB` после `export_all`:

```python
    @staticmethod
    def __is_valid_record(record) -> bool:
        return isinstance(record, dict) and all(
            key in record for key in REQUIRED_STICKER_FIELDS
        )

    def __drop_orphans(self):
        """Записи, на которые не ссылается ни один аккаунт, не нужны"""
        used = {
            sticker_id
            for ids in self.__stickers["accounts"].values()
            for sticker_id in ids
        }
        for sticker_id in list(self.__stickers["stickers"]):
            if sticker_id not in used:
                del self.__stickers["stickers"][sticker_id]

    def import_account(self, data: dict, account, replace: bool = False) -> tuple:
        """Импорт плоского списка в аккаунт

        ImportResult из спеки - это кортеж (применено, пропущено):
        заводить датакласс ради двух чисел незачем.
        """
        account = str(account)
        applied = skipped = 0
        if replace:
            self.__stickers["accounts"][account] = []
        ids = self.__stickers["accounts"].setdefault(account, [])
        # В файле новые сверху, база хранит наоборот
        for record in reversed(data.get("stickers", [])):
            if not self.__is_valid_record(record):
                skipped += 1
                log(f"[favstickers] Пропущена запись бекапа: {str(record)[:80]}")
                continue
            sticker_id = str(record["id"])
            self.__stickers["stickers"][sticker_id] = record
            if sticker_id not in ids:
                ids.append(sticker_id)
            applied += 1
        self.__drop_orphans()
        self.__cache.clear()
        self.__save_db()
        return applied, skipped
```

- [ ] **Step 4: Прогнать тесты**

Run: `python tests/test_plugin.py`
Expected: `53/53 прошло`

- [ ] **Step 5: Commit**

```bash
git add unlim_favorite_stickers.py tests/test_plugin.py
git commit -m "feat: импорт списка стикеров в аккаунт"
```

---

### Task 6: Импорт слепка базы

**Files:**
- Modify: `unlim_favorite_stickers.py` (класс `StickersDB`)
- Test: `tests/test_plugin.py`

- [ ] **Step 1: Написать падающие тесты**

```python
def test_import_all_restores_every_account():
    source, _ = make_db()
    source.add_sticker(FakeSticker(100), "42")
    source.add_sticker(FakeSticker(200), "42")
    source.add_sticker(FakeSticker(300), "43")
    target, _ = make_db()
    applied, skipped = target.import_all(source.export_all())
    assert (applied, skipped) == (3, 0)
    assert [doc.id for doc in target.get_all_stickers("42")] == [200, 100]
    assert [doc.id for doc in target.get_all_stickers("43")] == [300]


def test_import_all_replace_leaves_absent_accounts_alone():
    """Замена трогает только аккаунты, которые есть в файле."""
    source, _ = make_db()
    source.add_sticker(FakeSticker(100), "42")
    backup = source.export_all()
    target, _ = make_db()
    target.add_sticker(FakeSticker(999), "43")
    target.import_all(backup, replace=True)
    assert [doc.id for doc in target.get_all_stickers("43")] == [999]
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python tests/test_plugin.py`
Expected: `FAIL ... has no attribute 'import_all'`

- [ ] **Step 3: Реализовать**

Добавить в `StickersDB` после `import_account`:

```python
    def import_all(self, data: dict, replace: bool = False) -> tuple:
        """Импорт слепка: каждый аккаунт восстанавливается по своему id"""
        applied = skipped = 0
        records = data.get("stickers", {})
        for account, ids in data.get("accounts", {}).items():
            # import_account ждет порядок панели, в слепке порядок базы
            payload = {
                "stickers": [records[i] for i in reversed(ids) if i in records]
            }
            account_applied, account_skipped = self.import_account(
                payload, account, replace
            )
            applied += account_applied
            skipped += account_skipped
        return applied, skipped
```

- [ ] **Step 4: Прогнать тесты**

Run: `python tests/test_plugin.py`
Expected: `55/55 прошло`

- [ ] **Step 5: Commit**

```bash
git add unlim_favorite_stickers.py tests/test_plugin.py
git commit -m "feat: импорт слепка базы по аккаунтам"
```

---

### Task 7: Экран настроек и экспорт

Экран и обработчики идут одной задачей: экран без обработчиков не проходит тесты.

**Files:**
- Modify: `unlim_favorite_stickers.py` (класс `MyPlugin`)
- Test: `tests/test_plugin.py`

- [ ] **Step 1: Написать падающие тесты**

```python
def make_plugin_with_db(db):
    instance = plugin.MyPlugin()
    instance._MyPlugin__DB = db
    JAVA_CLASSES["org.telegram.messenger.UserConfig"].getInstance.return_value.clientUserId = 42
    return instance


def test_settings_screen_has_two_export_buttons():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    items = make_plugin_with_db(db).create_settings()
    clickable = [i for i in items if getattr(i, "on_click", None)]
    assert len(clickable) == 2, [i.text for i in items]
    assert "1" in clickable[0].text, clickable[0].text
    assert any(".stickers" in getattr(i, "text", "") for i in items), "нет подсказки про импорт"


def test_export_sends_document_to_saved_messages():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    instance = make_plugin_with_db(db)
    before = len(SENT_DOCUMENTS)
    instance.create_settings()[1].on_click(None)
    assert len(SENT_DOCUMENTS) == before + 1
    peer, path, _ = SENT_DOCUMENTS[-1]
    assert peer == 42, peer
    assert path.endswith(".stickers"), path
    assert json.load(open(path))["stickers"][0]["id"] == 100


def test_export_all_writes_snapshot_format():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    instance = make_plugin_with_db(db)
    instance.create_settings()[2].on_click(None)
    data = json.load(open(SENT_DOCUMENTS[-1][1]))
    assert "accounts" in data and "42" in data["accounts"]


def test_export_of_empty_db_sends_nothing():
    instance = make_plugin_with_db(make_db()[0])
    before = len(SENT_DOCUMENTS)
    instance.create_settings()[1].on_click(None)
    assert len(SENT_DOCUMENTS) == before
    assert BULLETINS[-1][0] == "info", BULLETINS[-1]
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python tests/test_plugin.py`
Expected: `FAIL ... 'MyPlugin' object has no attribute 'create_settings'`

- [ ] **Step 3: Реализовать экран**

Добавить в `MyPlugin` перед `on_plugin_load`:

```python
    def create_settings(self) -> list:
        """Экран настроек плагина

        Счетчики в тексте кнопок, а не в subtext: у Text в установленной
        версии ui.settings параметра subtext нет, и лишний аргумент уронил
        бы построение экрана целиком.
        """
        own = self.db.count_stickers(self.__get_current_account_id())
        total, accounts = self.db.count_all()
        return [
            Header(text="Бекап избранного"),
            Text(
                text=f"Экспорт текущего аккаунта ({own})",
                on_click=lambda view: self.__export_current(),
            ),
            Text(
                text=f"Экспорт всех аккаунтов ({total} в {accounts})",
                on_click=lambda view: self.__export_all(),
            ),
            Divider(
                text="Чтобы восстановить, откройте файл .stickers "
                "в любом чате и подтвердите импорт"
            ),
        ]
```

- [ ] **Step 4: Реализовать обработчики**

Добавить следом:

```python
    def __export_current(self):
        self.__export(
            self.db.export_account(self.__get_current_account_id()),
            backup_filename("account"),
            "В избранном пусто, нечего экспортировать",
        )

    def __export_all(self):
        self.__export(
            self.db.export_all(),
            backup_filename("all"),
            "База пуста, нечего экспортировать",
        )

    def __export(self, data: dict, filename: str, empty_message: str):
        """Записать бекап в кеш и отправить себе в Избранное

        Колбэк настроек выполняется в Java-UI, где исключение проглатывается
        так же молча, как в хуках, поэтому каждая ветка заканчивается плашкой.
        """
        fragment = get_last_fragment()
        try:
            if not data.get("stickers"):
                BulletinHelper.show_info(empty_message, fragment)
                return
            path = os.path.join(get_cache_dir(), filename)
            write_file(path, json.dumps(data, ensure_ascii=False))
            # send_document ждет числовой peer, база ключуется строкой
            send_document(
                int(self.__get_current_account_id()), path, BACKUP_CAPTION
            )
            BulletinHelper.show_success("Бекап отправлен в Избранное", fragment)
        except Exception as e:
            log(f"[favstickers] Экспорт не удался: {e!r}")
            BulletinHelper.show_error("Не удалось выполнить экспорт", fragment)
```

- [ ] **Step 5: Прогнать тесты**

Run: `python tests/test_plugin.py`
Expected: `59/59 прошло`

- [ ] **Step 6: Commit**

```bash
git add unlim_favorite_stickers.py tests/test_plugin.py
git commit -m "feat: экспорт избранного кнопками в настройках плагина"
```

---

### Task 8: Опознание файла бекапа при тапе

Самая опасная часть: хук висит на всех открываемых файлах, и ошибка опознания ломает открытие любого вложения.

**Files:**
- Modify: `unlim_favorite_stickers.py` (новый класс перед `MyPlugin`)
- Test: `tests/test_plugin.py`

- [ ] **Step 1: Написать падающие тесты**

```python
class FakeFile:
    def __init__(self, path):
        self.__path = path

    def getAbsolutePath(self):
        return self.__path


def test_backup_tap_is_intercepted():
    taps = []
    hook = plugin.ImportBackupHook(taps.append)
    param = FakeParam([FakeFile("/cache/favorites-2026-08-22.stickers")])
    hook.before_hooked_method(param)
    assert param.set_result_value is False
    assert taps == ["/cache/favorites-2026-08-22.stickers"]


def test_other_files_are_left_alone():
    """Ошибка здесь ломает открытие любого вложения в мессенджере."""
    taps = []
    hook = plugin.ImportBackupHook(taps.append)
    for arg in (FakeFile("/cache/doc.pdf"), FakeFile("/cache/photo.jpg"), None, object()):
        param = FakeParam([arg])
        hook.before_hooked_method(param)
        assert not param.result_was_set, arg
    assert taps == []


def test_hook_survives_empty_args():
    hook = plugin.ImportBackupHook(lambda path: None)
    param = FakeParam([])
    hook.before_hooked_method(param)
    assert not param.result_was_set
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python tests/test_plugin.py`
Expected: `FAIL ... module 'plugin' has no attribute 'ImportBackupHook'`

- [ ] **Step 3: Реализовать**

Добавить перед `class MyPlugin`:

```python
class ImportBackupHook(MethodHook):
    """Перехват тапа по файлу .stickers в чате

    Висит на всех перегрузках AndroidUtilities.openForView, то есть на любом
    открываемом файле. Штатное открытие гасится строго после того, как имя
    опознано - иначе плагин сломает открытие чужих вложений.
    """

    def __init__(self, on_backup_tap):
        self.__on_backup_tap = on_backup_tap

    def before_hooked_method(self, param):
        try:
            if not param.args:
                return
            path = self.__backup_path(param.args[0])
            if path is None:
                return
            param.setResult(False)
            self.__on_backup_tap(path)
        except Exception as e:
            log(f"[favstickers] Ошибка обработки тапа по файлу: {e!r}")

    @staticmethod
    def __backup_path(arg):
        """Путь к нашему файлу, иначе None"""
        if arg is None:
            return None
        if hasattr(arg, "getAbsolutePath"):
            path = str(arg.getAbsolutePath())
            return path if path.endswith(BACKUP_SUFFIX) else None
        name = str(arg.getDocumentName()) if hasattr(arg, "getDocumentName") else ""
        if not name.endswith(BACKUP_SUFFIX):
            return None
        # Скачанный файл лежит по attachPath
        attach_path = getattr(getattr(arg, "messageOwner", None), "attachPath", None)
        return str(attach_path) if attach_path else None
```

- [ ] **Step 4: Прогнать тесты**

Run: `python tests/test_plugin.py`
Expected: `62/62 прошло`

- [ ] **Step 5: Commit**

```bash
git add unlim_favorite_stickers.py tests/test_plugin.py
git commit -m "feat: опознание файла бекапа при тапе"
```

---

### Task 9: Диалог и применение импорта

**Files:**
- Modify: `unlim_favorite_stickers.py` (класс `MyPlugin`)
- Test: `tests/test_plugin.py`

- [ ] **Step 1: Написать падающие тесты**

```python
def write_backup_file(data) -> str:
    path = os.path.join(CACHE_DIR, "test-backup.stickers")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def test_import_asks_before_applying():
    source, _ = make_db()
    source.add_sticker(FakeSticker(100), "42")
    target, _ = make_db()
    instance = make_plugin_with_db(target)
    instance._MyPlugin__on_backup_tap(write_backup_file(source.export_account("42")))
    assert DIALOGS[-1].shown
    assert target.get_all_stickers("42") == [], "до подтверждения база не меняется"
    DIALOGS[-1].press("positive")
    assert len(target.get_all_stickers("42")) == 1


def test_import_replace_saves_safety_snapshot():
    source, _ = make_db()
    source.add_sticker(FakeSticker(100), "42")
    target, _ = make_db()
    target.add_sticker(FakeSticker(999), "42")
    instance = make_plugin_with_db(target)
    instance._MyPlugin__on_backup_tap(write_backup_file(source.export_account("42")))
    DIALOGS[-1].press("neutral")
    assert [doc.id for doc in target.get_all_stickers("42")] == [100]
    snapshots = [p for p in os.listdir(CACHE_DIR) if p.startswith("favorites-before-import")]
    assert snapshots, os.listdir(CACHE_DIR)


def test_import_of_broken_file_explains_why():
    path = os.path.join(CACHE_DIR, "broken.stickers")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{это не json")
    instance = make_plugin_with_db(make_db()[0])
    instance._MyPlugin__on_backup_tap(path)
    assert BULLETINS[-1][0] == "error"
    assert "бекап" in BULLETINS[-1][1].lower(), BULLETINS[-1]


def test_import_of_missing_file_says_to_wait():
    instance = make_plugin_with_db(make_db()[0])
    instance._MyPlugin__on_backup_tap(os.path.join(CACHE_DIR, "нет-такого.stickers"))
    assert BULLETINS[-1][0] == "info"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python tests/test_plugin.py`
Expected: `FAIL ... has no attribute '_MyPlugin__on_backup_tap'`

- [ ] **Step 3: Реализовать**

Добавить в `MyPlugin` после `__export`:

```python
    def __on_backup_tap(self, file_path: str):
        """Тап по файлу бекапа: прочитать, разобрать, спросить"""
        fragment = get_last_fragment()
        if not os.path.exists(file_path):
            BulletinHelper.show_info(
                "Файл ещё скачивается, дождитесь загрузки", fragment
            )
            return
        try:
            data = parse_backup(read_file(file_path))
        except BackupError as e:
            BulletinHelper.show_error(str(e), fragment)
            return
        except Exception as e:
            log(f"[favstickers] Не удалось прочитать бекап: {e!r}")
            BulletinHelper.show_error("Не удалось прочитать файл бекапа", fragment)
            return
        run_on_ui_thread(lambda: self.__confirm_import(data))

    def __confirm_import(self, data: dict):
        """Диалог выбора между слиянием и заменой"""
        fragment = get_last_fragment()
        activity = fragment.getParentActivity() if fragment else None
        if activity is None:
            # Без Activity диалог не показать, а молча заменять нельзя
            self.__apply_import(data, replace=False)
            return
        count = len(data["stickers"])
        builder = AlertDialogBuilder(activity)
        builder.set_title("Импорт избранного")
        builder.set_message(
            f"В файле {count} стикеров. Добавить их к текущим или заменить?"
        )
        builder.set_positive_button(
            "Добавить", lambda b, w: (b.dismiss(), self.__apply_import(data, False))
        )
        builder.set_neutral_button(
            "Заменить", lambda b, w: (b.dismiss(), self.__apply_import(data, True))
        )
        builder.set_negative_button("Отмена", lambda b, w: b.dismiss())
        builder.show()

    def __save_safety_snapshot(self):
        """Слепок перед заменой: единственная разрушительная операция плагина"""
        path = os.path.join(
            get_cache_dir(),
            f"favorites-before-import-{dt.date.today().isoformat()}{BACKUP_SUFFIX}",
        )
        write_file(path, json.dumps(self.db.export_all(), ensure_ascii=False))
        log(f"[favstickers] Страховочный слепок базы: {path}")

    def __apply_import(self, data: dict, replace: bool):
        fragment = get_last_fragment()
        try:
            if replace:
                self.__save_safety_snapshot()
            if detect_scope(data) == "all":
                applied, skipped = self.db.import_all(data, replace)
            else:
                applied, skipped = self.db.import_account(
                    data, self.__get_current_account_id(), replace
                )
            self.__notify_favorites_changed()
            message = f"Импортировано {applied}"
            if skipped:
                message += f", пропущено {skipped}"
            BulletinHelper.show_success(message, fragment)
        except Exception as e:
            log(f"[favstickers] Импорт не удался: {e!r}")
            BulletinHelper.show_error("Не удалось импортировать бекап", fragment)
```

Примечание: `len(data["stickers"])` работает для обоих форматов — у слепка это словарь, у списка список.

- [ ] **Step 4: Прогнать тесты**

Run: `python tests/test_plugin.py`
Expected: `66/66 прошло`

- [ ] **Step 5: Commit**

```bash
git add unlim_favorite_stickers.py tests/test_plugin.py
git commit -m "feat: диалог подтверждения и применение импорта"
```

---

### Task 10: Регистрация хука

**Files:**
- Modify: `unlim_favorite_stickers.py` (метод `__install_hooks`)
- Test: `tests/test_plugin.py`

- [ ] **Step 1: Написать падающий тест**

```python
def test_openforview_is_hooked_but_not_required():
    """Импорт - удобство: без openForView плагин обязан загрузиться."""
    instance = make_plugin_with_db(make_db()[0])
    instance.on_plugin_load()
    assert "openForView" in [name for name, _ in instance.hooked_by_name]

    instance = make_plugin_with_db(make_db()[0])
    instance.fail_on = "openForView"
    instance.on_plugin_load()
    assert instance.unhooked == [], "отказ openForView не должен ронять загрузку"
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `python tests/test_plugin.py`
Expected: `FAIL ... assert 'openForView' in ['getRecentStickers', 'getRecentStickersNoCopy']`

- [ ] **Step 3: Реализовать**

В `__install_hooks`, после блока `isStickerInFavorites`:

```python
        # Перехват тапа по файлу бекапа. Не обязателен: без него теряется
        # только импорт, а ради него ронять весь плагин незачем
        self.__hook_by_name(
            find_class("org.telegram.messenger.AndroidUtilities"),
            "openForView",
            ImportBackupHook(self.__on_backup_tap),
            unhooks,
            required=False,
        )
```

- [ ] **Step 4: Прогнать тесты**

Run: `python tests/test_plugin.py`
Expected: `67/67 прошло`

- [ ] **Step 5: Commit**

```bash
git add unlim_favorite_stickers.py tests/test_plugin.py
git commit -m "feat: подключить перехват тапа по файлу бекапа"
```

---

### Task 11: README и проверка на устройстве

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Дописать раздел после блока про кеширование стикеров**

```markdown
## Бекап

Избранное хранится только на устройстве, поэтому его стоит выносить наружу.

В настройках плагина две кнопки: «Экспорт текущего аккаунта» и «Экспорт всех
аккаунтов». Обе отправляют файл `.stickers` в «Избранное» — оттуда его видно с
любого устройства.

Чтобы восстановить, откройте этот файл в любом чате: плагин предложит добавить
стикеры к текущим или заменить их. Перед заменой в кеш кладётся слепок текущей
базы, путь пишется в лог.

Файл экспорта одного аккаунта не содержит идентификаторов — им можно
поделиться, чтобы передать подборку другому человеку.
```

- [ ] **Step 2: Обновить `__description__` в плагине**

```python
__description__ = "Remove limits on adding stickers to favorites, with backup"
```

- [ ] **Step 3: Прогнать тесты**

Run: `python tests/test_plugin.py`
Expected: `67/67 прошло`

- [ ] **Step 4: Commit**

```bash
git add README.md unlim_favorite_stickers.py
git commit -m "docs: описать бекап избранного (closes #5)"
```

- [ ] **Step 5: Проверка на устройстве**

Стенд подменяет Java, поэтому эти четыре пункта проверяются только руками:

1. Настройки плагина открываются, обе кнопки на месте, счётчики совпадают с числом стикеров в панели.
2. Нажатие «Экспорт текущего аккаунта» кладёт файл в «Избранное», плашка зелёная.
3. Тап по этому файлу показывает диалог; «Добавить» ничего не ломает, «Заменить» оставляет ровно содержимое файла.
4. **Тап по обычному вложению (pdf, фото, любой другой файл) по-прежнему открывает его.** Это главный риск задачи.

Логи смотреть так:

```bash
adb logcat -c && adb logcat --pid=$(adb shell pidof org.telegram.messenger | tr -d '\r') -v time | grep -i favstickers
```

---

## Что в объём не входит

- Обновление `access_hash` и `file_reference` у протухших стикеров (§10 аудита).
- Автоматические периодические бекапы (ToDo из README).
- Импорт кнопкой через системный выборщик файлов — решено делать тапом.
