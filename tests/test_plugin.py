"""Локальный прогон логики плагина вне Android.

StickersDB и хуки - чистый Python: Java нужна им только через jclass,
который здесь подменён. Запуск: python test_plugin.py [путь_к_плагину]
"""

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
    def show_success(text):
        BULLETINS.append(("success", text))

    @staticmethod
    def show_error(text):
        BULLETINS.append(("error", text))


class JInt:
    """java.jint: мост боксирует такое значение как java.lang.Integer."""

    def __init__(self, value):
        self.value = value


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


JAVA_CLASSES = {}


def fake_find_class(name):
    return JAVA_CLASSES.setdefault(name, MagicMock())


def install_stubs():
    android_utils = types.ModuleType("android_utils")
    android_utils.log = LOGS.append

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

    sys.modules.update(
        {
            "android_utils": android_utils,
            "base_plugin": base_plugin,
            "hook_utils": hook_utils,
            "java": java,
            "ui": ui,
            "ui.bulletin": ui_bulletin,
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
