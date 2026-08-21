"""Локальный прогон логики плагина вне Android.

StickersDB и хуки - чистый Python: Java нужна им только через jclass,
который здесь подменён. Запуск: python test_plugin.py [путь_к_плагину]
"""

import collections
import importlib.util
import json
import os
import sys
import tempfile
import types
from unittest.mock import MagicMock

DEFAULT_PLUGIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "unlim_favorite_stickers.py",
)
PLUGIN_PATH = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PLUGIN

LOGS = []
BULLETINS = []


# --- подделки Java-стороны ------------------------------------------------


class FakeArrayList:
    """java.util.ArrayList в объёме, который использует плагин."""

    def __init__(self):
        self._items = []

    def add(self, item):
        self._items.append(item)

    def size(self):
        return len(self._items)

    def get(self, index):
        return self._items[index]


def _tl(name):
    """Пустой TL-класс: поля назначаются присваиванием, как в Java."""
    return type(name, (), {})


class FakeTLRPC:
    TL_document = _tl("TL_document")
    TL_documentAttributeSticker = _tl("TL_documentAttributeSticker")
    TL_inputStickerSetID = _tl("TL_inputStickerSetID")


class FakeBulletinHelper:
    @staticmethod
    def show_success(text, fragment=None):
        BULLETINS.append(("success", text))

    @staticmethod
    def show_error(text, fragment=None):
        BULLETINS.append(("error", text))

    @staticmethod
    def show_info(text, fragment=None):
        BULLETINS.append(("info", text))


class JInt:
    """java.jint: мост боксирует такое значение как java.lang.Integer."""

    def __init__(self, value):
        self.value = value


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


def fake_jclass(name):
    if name == "java.util.ArrayList":
        return FakeArrayList
    if name == "org.telegram.tgnet.TLRPC":
        return FakeTLRPC
    # java.lang.Integer.TYPE и прочая мелочь, которую плагин только передаёт дальше
    return MagicMock()


class FakeBasePlugin:
    """Запоминает, что и как плагин просил перехватить."""

    fail_on = None  # имя метода, перехват которого должен провалиться

    def __init__(self):
        self.hooked_methods = []
        self.hooked_by_name = []
        self.unhooked = []

    def hook_method(self, method, hook, priority=None):
        if self.fail_on == "addRecentSticker" and not self.hooked_methods:
            return None
        self.hooked_methods.append((method, hook))
        return object()

    def hook_all_methods(self, cls, method_name, hook, priority=None):
        if method_name == self.fail_on:
            return None
        self.hooked_by_name.append((method_name, hook))
        return [object()]

    def unhook_method(self, unhook):
        self.unhooked.append(unhook)


JAVA_CLASSES = collections.defaultdict(MagicMock)


def fake_find_class(name):
    return JAVA_CLASSES[name]


def install_stubs():
    android_utils = types.ModuleType("android_utils")
    android_utils.log = LOGS.append
    android_utils.run_on_ui_thread = lambda func, delay=0: func()

    base_plugin = types.ModuleType("base_plugin")
    base_plugin.BasePlugin = FakeBasePlugin
    base_plugin.MethodHook = type("MethodHook", (), {})

    hook_utils = types.ModuleType("hook_utils")
    hook_utils.find_class = fake_find_class

    java = types.ModuleType("java")
    java.jclass = fake_jclass
    java.jint = JInt

    ui = types.ModuleType("ui")
    ui_bulletin = types.ModuleType("ui.bulletin")
    ui_bulletin.BulletinHelper = FakeBulletinHelper

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

    sys.modules.update(
        {
            "android_utils": android_utils,
            "base_plugin": base_plugin,
            "hook_utils": hook_utils,
            "java": java,
            "ui": ui,
            "ui.bulletin": ui_bulletin,
            "ui.settings": ui_settings,
            "ui.alert": ui_alert,
            "client_utils": client_utils,
            "file_utils": file_utils,
        }
    )


install_stubs()
spec = importlib.util.spec_from_file_location("plugin", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


# --- подделки телеграмных объектов ----------------------------------------


class FakeStickerSet:
    def __init__(self, set_id):
        self.id = set_id


class FakeAttribute:
    def __init__(self, stickerset):
        self.stickerset = stickerset


class FakeSticker:
    """TLRPC.Document в объёме, который читает serialize_sticker."""

    def __init__(self, sticker_id, set_id=7, dc_id=2):
        self.id = sticker_id
        self.access_hash = sticker_id * 10
        self.dc_id = dc_id
        self.mime_type = "image/webp"
        self.attributes = FakeArrayList()
        if set_id is not None:
            self.attributes.add(FakeAttribute(FakeStickerSet(set_id)))


class FakeParam:
    """XC_MethodHook.MethodHookParam: важно, вызвали ли setResult."""

    _UNSET = object()

    def __init__(self, args, result=None):
        self.args = args
        self.result = result
        self.set_result_value = self._UNSET

    def setResult(self, value):
        self.set_result_value = value

    def getResult(self):
        return self.result

    @property
    def result_was_set(self):
        return self.set_result_value is not self._UNSET


def make_db(payload=None):
    """Свежая StickersDB на временном файле."""
    path = os.path.join(tempfile.mkdtemp(), "stickers.json")
    if payload is not None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    return plugin.StickersDB(path), path


TYPE_IMAGE, TYPE_MASK, TYPE_FAVE, TYPE_PREMIUM = 0, 1, 2, 7


# --- StickersDB -----------------------------------------------------------


def test_add_then_read_returns_sticker():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    assert len(db.get_all_stickers("42")) == 1


def test_read_order_is_newest_first():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    db.add_sticker(FakeSticker(200), "42")
    ids = [doc.id for doc in db.get_all_stickers("42")]
    assert ids == [200, 100], ids


def test_add_is_idempotent():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    db.add_sticker(FakeSticker(100), "42")
    assert len(db.get_all_stickers("42")) == 1


def test_survives_on_disk():
    db, path = make_db()
    db.add_sticker(FakeSticker(100), "42")
    assert len(plugin.StickersDB(path).get_all_stickers("42")) == 1


def test_broken_record_does_not_kill_the_list():
    """Ключевая правка: битая запись стоит один стикер, а не весь список."""
    db, _ = make_db(
        {
            "accounts": {"42": ["100", "300", "400"]},
            "stickers": {
                "100": {"id": 100, "access_hash": 1, "dc_id": 2,
                        "mime_type": "image/webp", "sticker_set_id": 7},
                "300": {"id": 300, "dc_id": 2, "mime_type": "image/webp"},
            },
        }
    )
    before = len(LOGS)
    result = db.get_all_stickers("42")
    assert len(result) == 1, len(result)
    assert any("300" in line for line in LOGS[before:]), LOGS[before:]


def test_sticker_without_set_is_readable():
    """Стикер без набора: sticker_set_id отсутствует, читаться должен."""
    db, _ = make_db(
        {
            "accounts": {"42": ["200"]},
            "stickers": {
                "200": {"id": 200, "access_hash": 1, "dc_id": 2,
                        "mime_type": "image/webp"}
            },
        }
    )
    assert len(db.get_all_stickers("42")) == 1


def test_account_key_survives_int_and_str():
    """JSON хранит ключи строками, а id аккаунта может прийти числом."""
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), 42)
    assert len(db.get_all_stickers("42")) == 1
    assert db.is_sticker_favorite(FakeSticker(100), 42) is True
    db.remove_sticker(FakeSticker(100), "42")
    assert db.get_all_stickers(42) == []


def test_repeated_reads_reuse_deserialized_documents():
    """Разбор идет через JNI, а метод зовут на каждой перерисовке панели."""
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    first = db.get_all_stickers("42")
    assert db.get_all_stickers("42") is first


def test_cache_is_dropped_on_change():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    before = db.get_all_stickers("42")
    db.add_sticker(FakeSticker(200), "42")
    after = db.get_all_stickers("42")
    assert after is not before
    assert [doc.id for doc in after] == [200, 100]
    db.remove_sticker(FakeSticker(200), "42")
    assert [doc.id for doc in db.get_all_stickers("42")] == [100]


def test_cache_does_not_leak_between_accounts():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    db.add_sticker(FakeSticker(200), "43")
    assert [doc.id for doc in db.get_all_stickers("42")] == [100]
    assert [doc.id for doc in db.get_all_stickers("43")] == [200]


def test_unknown_account_is_empty_not_error():
    db, _ = make_db()
    assert db.get_all_stickers("нет такого") == []
    assert db.is_sticker_favorite(FakeSticker(1), "нет такого") is False


def test_remove_drops_sticker():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    db.remove_sticker(FakeSticker(100), "42")
    assert db.get_all_stickers("42") == []


def test_remove_keeps_sticker_shared_with_other_account():
    db, path = make_db()
    db.add_sticker(FakeSticker(100), "42")
    db.add_sticker(FakeSticker(100), "43")
    db.remove_sticker(FakeSticker(100), "42")
    assert db.get_all_stickers("42") == []
    assert len(db.get_all_stickers("43")) == 1
    with open(path, encoding="utf-8") as f:
        assert "100" in json.load(f)["stickers"]


def test_corrupt_json_falls_back_to_empty():
    path = os.path.join(tempfile.mkdtemp(), "stickers.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{это не json")
    assert plugin.StickersDB(path).get_all_stickers("42") == []


# --- известные дыры: тесты фиксируют текущее поведение --------------------


def test_wrong_shape_json_falls_back_to_empty():
    """Валидный JSON не той формы разбор проходит, а роняет чтение."""
    for payload in ([{"id": 1}], {"accounts": {}}, "строка", 42):
        path = os.path.join(tempfile.mkdtemp(), "stickers.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        assert plugin.StickersDB(path).get_all_stickers("42") == [], payload


def test_sticker_without_stickerset_id_is_saved():
    """TL_inputStickerSetEmpty: поля id нет, но стикер сохранить надо."""
    sticker = FakeSticker(100, set_id=None)
    sticker.attributes.add(FakeAttribute(object()))
    data = plugin.serialize_sticker(sticker)
    assert data["id"] == 100
    assert "sticker_set_id" not in data, data


def test_failed_save_tells_the_user():
    """Иначе пользователь добавляет стикеры весь сеанс и теряет их."""
    db = plugin.StickersDB(os.path.join(tempfile.mkdtemp(), "нет-каталога", "db.json"))
    before = len(BULLETINS)
    db.add_sticker(FakeSticker(100), "42")
    assert BULLETINS[before:] and BULLETINS[-1][0] == "error", BULLETINS[before:]


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
    assert data["accounts"]["42"] == ["100"], data["accounts"]
    assert data["stickers"]["100"]["id"] == 100


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


def test_import_all_rejects_broken_snapshot_before_touching_db():
    """Падение на середине оставило бы базу восстановленной наполовину."""
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    for broken in ({"accounts": [], "stickers": {}}, {"accounts": {}, "stickers": []}):
        try:
            db.import_all(broken, replace=True)
        except plugin.BackupError:
            pass
        else:
            raise AssertionError(f"испорченный слепок принят: {broken}")
    assert [doc.id for doc in db.get_all_stickers("42")] == [100]


def test_import_all_skips_account_with_broken_list():
    """Строка вместо списка развернулась бы reversed() молча."""
    source, _ = make_db()
    source.add_sticker(FakeSticker(100), "42")
    backup = source.export_all()
    backup["accounts"]["43"] = "не список"
    target, _ = make_db()
    before = len(LOGS)
    applied, skipped = target.import_all(backup)
    assert applied == 1, applied
    assert target.get_all_stickers("43") == []
    assert [doc.id for doc in target.get_all_stickers("42")] == [100]
    assert any("43" in line for line in LOGS[before:]), LOGS[before:]


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


def test_import_of_wrong_format_does_not_wipe_account():
    """Слепок базы в import_account: замена не должна стирать избранное."""
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    snapshot = db.export_all()
    try:
        db.import_account(snapshot, "42", replace=True)
    except plugin.BackupError:
        pass
    else:
        raise AssertionError("чужой формат должен быть отвергнут")
    assert [doc.id for doc in db.get_all_stickers("42")] == [100]


def test_import_survives_record_that_breaks_on_str():
    """Запись из файла может оказаться чем угодно, включая ломающую str()."""

    class Hostile(dict):
        def __str__(self):
            raise ValueError("строковое представление недоступно")

    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    applied, skipped = db.import_account(
        {"stickers": [Hostile(), {"id": 200, "access_hash": 1, "dc_id": 2,
                                  "mime_type": "image/webp"}]},
        "42",
    )
    assert (applied, skipped) == (1, 1), (applied, skipped)
    assert [doc.id for doc in db.get_all_stickers("42")] == [200, 100]


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


def test_parse_backup_without_version_is_not_a_backup():
    """Чужой .stickers не должен советовать обновить плагин."""
    try:
        plugin.parse_backup(json.dumps({"stickers": []}))
    except plugin.BackupError as e:
        assert "не похож" in str(e), str(e)
    else:
        raise AssertionError("файл без версии должен быть отклонён")


def test_parse_backup_survives_deeply_nested_json():
    """C-декодер json кидает RecursionError, а не ValueError."""
    try:
        plugin.parse_backup("[" * 60000)
    except plugin.BackupError:
        return
    raise AssertionError("глубокая вложенность должна давать BackupError")


def test_parse_backup_rejects_garbage():
    for text in ("не json", json.dumps([1, 2]), json.dumps({"version": 1})):
        try:
            plugin.parse_backup(text)
        except plugin.BackupError:
            continue
        raise AssertionError(f"мусор принят за бекап: {text}")


def test_scope_agrees_with_parser_on_mixed_file():
    """accounts словарём, но stickers списком - это формат одного аккаунта."""
    text = json.dumps({"version": 1, "accounts": {"42": ["1"]}, "stickers": []})
    assert plugin.detect_scope(plugin.parse_backup(text)) == "account"


def test_backup_filename_marks_scope():
    assert plugin.backup_filename("account").endswith(".stickers")
    assert "-all-" in plugin.backup_filename("all")
    assert "-all-" not in plugin.backup_filename("account")


def test_counts_for_settings_screen():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    db.add_sticker(FakeSticker(200), "42")
    db.add_sticker(FakeSticker(300), "43")
    assert db.count_stickers("42") == 2
    assert db.count_all() == (3, 2)


def test_counts_ignore_dangling_ids():
    """Счётчик на кнопке экспорта должен совпадать с содержимым файла."""
    db, _ = make_db(
        {
            "accounts": {"42": ["100", "нет-такого"]},
            "stickers": {
                "100": {"id": 100, "access_hash": 1, "dc_id": 2,
                        "mime_type": "image/webp"}
            },
        }
    )
    assert db.count_stickers("42") == 1
    assert len(db.export_account("42")["stickers"]) == 1


def test_emptied_account_is_not_counted():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    db.remove_sticker(FakeSticker(100), "42")
    assert db.count_all() == (0, 0)


def test_account_of_dangling_ids_is_not_counted():
    """Список не пуст, но реальных стикеров в нём нет."""
    db, _ = make_db({"accounts": {"42": ["нет-такого"]}, "stickers": {}})
    assert db.count_stickers("42") == 0
    assert db.count_all() == (0, 0)


# --- ChangeFavoriteStickerHook -------------------------------------------


def make_change_hook():
    calls = {"add": [], "remove": [], "refresh": []}
    hook = plugin.ChangeFavoriteStickerHook(
        on_add_favorite=lambda s, a: calls["add"].append((s, a)),
        on_remove_favorite=lambda s, a: calls["remove"].append((s, a)),
        refresh_panel=lambda: calls["refresh"].append(True),
        get_account_id=lambda: "42",
    )
    return hook, calls


def test_fave_add_saves_and_cancels_original():
    hook, calls = make_change_hook()
    param = FakeParam([TYPE_FAVE, None, FakeSticker(100), 0, False])
    hook.before_hooked_method(param)
    assert len(calls["add"]) == 1
    assert param.result_was_set, "оригинал должен быть отменён"


def test_fave_remove_goes_to_remove_branch():
    hook, calls = make_change_hook()
    param = FakeParam([TYPE_FAVE, None, FakeSticker(100), 0, True])
    hook.before_hooked_method(param)
    assert len(calls["remove"]) == 1
    assert param.result_was_set


def test_fave_change_refreshes_panel():
    """Панель обновлял отмененный нами оригинал - теперь это наша забота."""
    for remove in (False, True):
        hook, calls = make_change_hook()
        hook.before_hooked_method(
            FakeParam([TYPE_FAVE, None, FakeSticker(100), 0, remove])
        )
        assert calls["refresh"] == [True], (remove, calls["refresh"])


def test_refresh_passes_java_integer():
    """Голый int мост боксирует в Long, а слушатель ждет Integer."""
    plugin.MyPlugin._MyPlugin__notify_favorites_changed()
    center = JAVA_CLASSES["org.telegram.messenger.NotificationCenter"]
    args = center.getInstance.return_value.postNotificationName.call_args[0]
    assert isinstance(args[2], JInt), type(args[2])
    assert args[2].value == TYPE_FAVE


def test_recent_sticker_is_left_alone():
    """Отправка стикера: TYPE_IMAGE не наш, оригинал отменять нельзя."""
    hook, calls = make_change_hook()
    param = FakeParam([TYPE_IMAGE, None, FakeSticker(100), 0, False])
    hook.before_hooked_method(param)
    assert calls["add"] == [] and calls["remove"] == []
    assert not param.result_was_set, "недавние перестанут пополняться"
    assert calls["refresh"] == []


def test_mask_is_left_alone():
    hook, calls = make_change_hook()
    param = FakeParam([TYPE_MASK, None, FakeSticker(100), 0, False])
    hook.before_hooked_method(param)
    assert not param.result_was_set


# --- GetFavoriteStickersHook ---------------------------------------------


def make_get_hook(stickers_by_account, imported=None):
    return plugin.GetFavoriteStickersHook(
        lambda account: stickers_by_account.get(account, []),
        lambda: "42",
        lambda stickers, account: (imported if imported is not None else []).append(
            (stickers, account)
        ),
    )


def test_fave_list_is_replaced():
    hook = make_get_hook({"42": [FakeSticker(1), FakeSticker(2), FakeSticker(3)]})
    param = FakeParam([TYPE_FAVE])
    hook.after_hooked_method(param)
    assert param.result_was_set
    assert param.set_result_value.size() == 3


def test_recent_list_is_not_replaced():
    """§3: без этой проверки в 'Недавние' подставилось бы избранное."""
    hook = make_get_hook({"42": [FakeSticker(1)]})
    for type_id in (TYPE_IMAGE, TYPE_MASK, TYPE_PREMIUM):
        param = FakeParam([type_id])
        hook.after_hooked_method(param)
        assert not param.result_was_set, f"подменён список типа {type_id}"


def test_two_arg_overload_is_handled():
    """getRecentStickers(type, firstEmpty): тип по-прежнему первый аргумент."""
    hook = make_get_hook({"42": [FakeSticker(1)]})
    param = FakeParam([TYPE_FAVE, True])
    hook.after_hooked_method(param)
    assert param.result_was_set


def test_empty_db_falls_through_to_vanilla():
    """Так задумано: из ванильного списка идет первичный импорт базы."""
    hook = make_get_hook({})
    param = FakeParam([TYPE_FAVE])
    hook.after_hooked_method(param)
    assert not param.result_was_set


def test_empty_db_seeds_itself_from_vanilla_list():
    """Импорт идет отсюда, а не с загрузки плагина: тут список уже готов."""
    imported = []
    hook = make_get_hook({}, imported)
    vanilla = FakeArrayList()
    vanilla.add(FakeSticker(1))
    param = FakeParam([TYPE_FAVE], result=vanilla)
    hook.after_hooked_method(param)
    assert imported == [(vanilla, "42")], imported


def test_filled_db_is_not_reimported():
    imported = []
    hook = make_get_hook({"42": [FakeSticker(1)]}, imported)
    hook.after_hooked_method(FakeParam([TYPE_FAVE], result=FakeArrayList()))
    assert imported == []


def test_import_survives_empty_vanilla_list():
    """Список еще не подтянулся с сервера - просто ждем следующего чтения."""
    db, _ = make_db()
    plugin_instance = plugin.MyPlugin()
    plugin_instance._MyPlugin__DB = db
    plugin_instance._MyPlugin__import_vanilla_favorites(None, "42")
    plugin_instance._MyPlugin__import_vanilla_favorites(FakeArrayList(), "42")
    assert db.get_all_stickers("42") == []


def test_import_keeps_panel_order():
    """Ванильный список идет новыми сверху, база хранит наоборот."""
    db, _ = make_db()
    plugin_instance = plugin.MyPlugin()
    plugin_instance._MyPlugin__DB = db
    vanilla = FakeArrayList()
    vanilla.add(FakeSticker(100))
    vanilla.add(FakeSticker(200))
    plugin_instance._MyPlugin__import_vanilla_favorites(vanilla, "42")
    assert [doc.id for doc in db.get_all_stickers("42")] == [100, 200]


# --- IsStickerInFavoritesHook --------------------------------------------


def test_is_favorite_answers_from_db():
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    hook = plugin.IsStickerInFavoritesHook(db.is_sticker_favorite, lambda: "42")
    for sticker_id, expected in ((100, True), (999, False)):
        param = FakeParam([FakeSticker(sticker_id)])
        hook.before_hooked_method(param)
        assert param.set_result_value is expected, sticker_id


# --- on_plugin_load -------------------------------------------------------


class FakeDB:
    def get_all_stickers(self, account):
        return ["не пусто, чтобы пропустить первичный импорт"]

    def add_sticker(self, sticker, account):
        pass

    def remove_sticker(self, sticker, account):
        pass

    def is_sticker_favorite(self, sticker, account):
        return False


def boom(*args):
    raise RuntimeError("база недоступна")


def test_failed_write_leaves_original_alone():
    """Запись упала - пусть стикер уйдет хотя бы в ванильное избранное."""
    hook = plugin.ChangeFavoriteStickerHook(boom, boom, lambda: None, lambda: "42")
    param = FakeParam([TYPE_FAVE, None, FakeSticker(100), 0, False])
    hook.before_hooked_method(param)
    assert not param.result_was_set, "оригинал отменять нечем - база не записана"
    assert BULLETINS[-1][0] == "error", BULLETINS[-1]


def test_failed_read_falls_back_to_vanilla():
    hook = plugin.GetFavoriteStickersHook(boom, lambda: "42", boom)
    param = FakeParam([TYPE_FAVE])
    hook.after_hooked_method(param)
    assert not param.result_was_set


def test_failed_favorite_check_falls_back_to_vanilla():
    hook = plugin.IsStickerInFavoritesHook(boom, lambda: "42")
    param = FakeParam([FakeSticker(1)])
    hook.before_hooked_method(param)
    assert not param.result_was_set


def test_hook_failures_are_logged():
    """Мост молчит про исключения в хуках - след должен остаться у нас."""
    before = len(LOGS)
    plugin.GetFavoriteStickersHook(boom, lambda: "42", boom).after_hooked_method(
        FakeParam([TYPE_FAVE])
    )
    assert len(LOGS) > before, "падение хука прошло бесследно"


def test_all_list_accessors_are_hooked():
    """Правка охвата: оба аксессора вешаются по имени, а не по сигнатуре."""
    plugin.MyPlugin._MyPlugin__DB = FakeDB()
    try:
        instance = plugin.MyPlugin()
        instance.on_plugin_load()
        names = [name for name, _ in instance.hooked_by_name]
        assert names == ["getRecentStickers", "getRecentStickersNoCopy"], names
        assert len(instance.hooked_methods) == 2, "addRecentSticker + isStickerInFavorites"
        assert instance.unhooked == []
    finally:
        plugin.MyPlugin._MyPlugin__DB = None


def test_partial_install_is_rolled_back():
    """Полурабочий плагин хуже неработающего: снимаем уже поставленное."""
    plugin.MyPlugin._MyPlugin__DB = FakeDB()
    try:
        instance = plugin.MyPlugin()
        instance.fail_on = "getRecentStickers"
        try:
            instance.on_plugin_load()
        except RuntimeError:
            pass
        else:
            raise AssertionError("загрузка должна была прерваться")
        # addRecentSticker успел встать до отказа - он должен быть снят
        assert len(instance.unhooked) == 1, instance.unhooked
    finally:
        plugin.MyPlugin._MyPlugin__DB = None


def test_optional_accessor_absence_does_not_block_load():
    """getRecentStickersNoCopy есть не во всех сборках - это не отказ."""
    plugin.MyPlugin._MyPlugin__DB = FakeDB()
    try:
        instance = plugin.MyPlugin()
        instance.fail_on = "getRecentStickersNoCopy"
        instance.on_plugin_load()
        assert [n for n, _ in instance.hooked_by_name] == ["getRecentStickers"]
        assert instance.unhooked == []
    finally:
        plugin.MyPlugin._MyPlugin__DB = None


# --- create_settings / export --------------------------------------------


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


def test_settings_screen_survives_broken_db():
    """Экран строится в Java-UI: исключение отсюда никто не увидит."""

    class BrokenDB:
        def count_stickers(self, account):
            raise RuntimeError("база недоступна")

        def count_all(self):
            raise RuntimeError("база недоступна")

    instance = make_plugin_with_db(BrokenDB())
    before = len(LOGS)
    items = instance.create_settings()
    assert items, "экран не должен оказаться пустым"
    assert any("favstickers" in line for line in LOGS[before:]), LOGS[before:]


def test_export_reports_failure_without_fragment():
    """Падение get_last_fragment не должно съедать плашку об ошибке.

    Плагин импортировал get_last_fragment по имени (from client_utils
    import get_last_fragment), поэтому подмена атрибута на модуле
    client_utils плагина не коснётся - __export резолвит имя в
    пространстве имён самого модуля плагина. Подменяем там.
    """
    db, _ = make_db()
    db.add_sticker(FakeSticker(100), "42")
    instance = make_plugin_with_db(db)

    def broken_get_last_fragment():
        raise RuntimeError("нет фрагмента")

    original = plugin.get_last_fragment
    plugin.get_last_fragment = broken_get_last_fragment
    try:
        instance.create_settings()[1].on_click(None)
    finally:
        plugin.get_last_fragment = original
    assert BULLETINS[-1][0] == "error", BULLETINS[-1]


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


# --- раннер ---------------------------------------------------------------

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = []
    for name, func in tests:
        try:
            func()
            mark = "GAP " if "KNOWN_GAP" in name else "ok  "
            print(f"  {mark}{name}")
        except Exception as e:
            failed.append(name)
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} прошло")
    sys.exit(1 if failed else 0)
