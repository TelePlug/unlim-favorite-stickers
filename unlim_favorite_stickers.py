import json
import os

from android_utils import log
from base_plugin import BasePlugin, MethodHook
from hook_utils import find_class
from java import jclass, jint
from ui.bulletin import BulletinHelper

__id__ = "favstickers"
__name__ = "Unlim favorite stickers"
__description__ = "Remove limits on adding stickers to favorites"
__author__ = "@DaShMore"
__version__ = "2.0.1"
__icon__ = "plugins_covers/0"
__min_version__ = "11.12.0"


class Jclass:
    def __getattr__(self, name: str):
        return jclass(f"java.lang.{name.title()}")


J = Jclass()
TLRPC = jclass("org.telegram.tgnet.TLRPC")
ArrayList = jclass("java.util.ArrayList")


def serialize_sticker(sticker) -> dict[str, str | int]:
    # Иногда обертка стикера содержит .document
    if hasattr(sticker, "document") and sticker.document is not None:
        sticker = sticker.document

    data = {
        "id": int(sticker.id),
        "access_hash": int(sticker.access_hash),
        "dc_id": int(sticker.dc_id),
        "mime_type": str(sticker.mime_type),
    }

    if hasattr(sticker, "attributes") and sticker.attributes is not None:
        attr_list = sticker.attributes
        for i in range(attr_list.size()):
            att = attr_list.get(i)

            if hasattr(att, "stickerset") and att.stickerset is not None:
                # У TL_inputStickerSetEmpty и TL_inputStickerSetShortName
                # поля id нет вовсе - такой стикер сохраняем без набора
                set_id = getattr(att.stickerset, "id", None)
                if set_id is not None:
                    data["sticker_set_id"] = set_id
    return data


def deserialize_sticker(data: dict):
    """Десериализует словарь в телеграм стикер"""
    doc = TLRPC.TL_document()

    doc.id = data["id"]
    doc.access_hash = data["access_hash"]
    doc.dc_id = data["dc_id"]
    doc.mime_type = data["mime_type"]
    # FIXME
    doc.attributes = ArrayList()

    a = TLRPC.TL_documentAttributeSticker()
    ss = TLRPC.TL_inputStickerSetID()
    # serialize_sticker кладет sticker_set_id только если у стикера нашелся
    # атрибут stickerset - у стикера без набора ключа не будет
    ss.id = data.get("sticker_set_id", 0)
    a.stickerset = ss
    doc.attributes.add(a)

    return doc


class StickersDB:
    """
    Класс для работы с базой данных стикеров

    {
        "accounts": {
            <account_id>: [
                <sticker_id>,
                <sticker_id>,
            ]
        },
        "stickers": {
            <sticker_id>: {
                "id": <sticker_id>,
                "access_hash": <access_hash>,
                "dc_id": <dc_id>,
                "mime_type": <mime_type>,
                "sticker_set_id": <sticker_set_id>
            }
        }
    }
    """

    # HACK все ключи при сохранении становятся строками, так что иногда надо приводить их вручную через str()
    def __init__(self, db_path: str):
        self.__db_path = db_path
        self.__load_db()

    @staticmethod
    def __has_expected_shape(stickers) -> bool:
        return (
            isinstance(stickers, dict)
            and isinstance(stickers.get("accounts"), dict)
            and isinstance(stickers.get("stickers"), dict)
        )

    def __load_db(self):
        """Чтение всех стикеров из базы в self.stickers"""
        self.__stickers = {"accounts": {}, "stickers": {}}
        if not os.path.exists(self.__db_path):
            return
        try:
            with open(self.__db_path, encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError) as e:
            # Битый или недоступный файл - начинаем с пустой базы,
            # иначе плагин не загрузится вообще
            log(f"[favstickers] Не удалось прочитать базу стикеров: {e}")
            return
        if not self.__has_expected_shape(loaded):
            # Валидный JSON не той формы разбор проходит, а падает потом на
            # каждом обращении - уже внутри хука, где исключение никто не увидит
            log("[favstickers] База стикеров не той формы, начинаем с пустой")
            return
        self.__stickers = loaded

    def __save_db(self):
        """Сохранение всех стикеров из self.stickers в базу"""
        tmp_path = self.__db_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.__stickers, f, ensure_ascii=False)
            # Подмена файла целиком: прерывание записи не оставит битую базу
            os.replace(tmp_path, self.__db_path)
        except OSError as e:
            # Молча продолжать нельзя: пользователь будет добавлять стикеры
            # весь сеанс и потеряет их при перезапуске
            log(f"[favstickers] Не удалось сохранить базу стикеров: {e}")
            BulletinHelper.show_error("Failed to save favorite stickers")

    def get_all_stickers(self, account: int) -> list[dict[str, str | int]]:
        """Получение всех стикеров в виде объектов TLRPC$TL_document"""
        stickers = []
        for sticker_id in reversed(self.__stickers["accounts"].get(account, [])):
            data = self.__stickers["stickers"].get(sticker_id)
            if data is None:
                continue
            try:
                stickers.append(deserialize_sticker(data))
            except Exception as e:
                # Одна битая запись не должна уносить весь список: исключение
                # отсюда уходит в хук, где его глотает мост, и пользователь
                # видит ванильное избранное вместо своего
                log(f"[favstickers] Пропущен стикер {sticker_id}: {e}")
        return stickers

    def add_sticker(self, sticker, account: int):
        """Сериализация и добавление стикера в базу без дубликатов"""
        serialized_sticker = serialize_sticker(sticker)
        sticker_id = str(serialized_sticker["id"])
        changed = False
        if sticker_id not in self.__stickers["stickers"]:
            self.__stickers["stickers"][sticker_id] = serialized_sticker
            changed = True
        if account not in self.__stickers["accounts"]:
            self.__stickers["accounts"][account] = []
        if sticker_id not in self.__stickers["accounts"][account]:
            self.__stickers["accounts"][account].append(sticker_id)
            changed = True
        if changed:
            self.__save_db()

    def remove_sticker(self, sticker, account: int):
        """Удаление стикера из базы и self.stickers."""
        serialized_sticker = serialize_sticker(sticker)
        sticker_id = str(serialized_sticker["id"])
        changed = False
        if sticker_id in self.__stickers["accounts"].get(account, []):
            self.__stickers["accounts"][account].remove(sticker_id)
            changed = True
        # Если ни у кого стикер не сохранен - удаляем из общего списка
        if sticker_id in self.__stickers["stickers"] and not any(
            sticker_id in i for i in self.__stickers["accounts"].values()
        ):
            del self.__stickers["stickers"][sticker_id]
            changed = True
        if changed:
            self.__save_db()

    def is_sticker_favorite(self, sticker, account: int):
        """Проверка, есть ли стикер в избранных"""
        serialized_sticker = serialize_sticker(sticker)
        return str(serialized_sticker["id"]) in self.__stickers["accounts"].get(
            account, []
        )


class ChangeFavoriteStickerHook(MethodHook):
    def __init__(self, on_add_favorite, on_remove_favorite, refresh_panel, get_account_id):
        self.__on_add_favorite = on_add_favorite
        self.__on_remove_favorite = on_remove_favorite
        self.__refresh_panel = refresh_panel
        self.__get_account_id = get_account_id

    def before_hooked_method(self, param):
        # Проверяем, что выбран пункт добавления / удаления из избранное (TYPE_FAVE = 2)
        if param.args[0] != 2:
            return
        try:
            self.__change_favorite(param)
        except Exception as e:
            # Мост глотает исключение молча: без setResult отработает оригинал
            # и уведет стикер в ванильную пятерку. Скажем хотя бы об этом
            log(f"[favstickers] Не удалось изменить избранное: {e!r}")
            BulletinHelper.show_error("Failed to change favorites")

    def __change_favorite(self, param):
        sticker = param.args[2]
        account_id = self.__get_account_id()
        # И что стикер не находится в избранном (not inFavs)
        if not param.args[4]:
            self.__on_add_favorite(sticker, account_id)
            BulletinHelper.show_success("Sticker added to favorites")
        else:
            self.__on_remove_favorite(sticker, account_id)
            BulletinHelper.show_error("Sticker removed from favorites")
        param.setResult(None)
        # Оригинал отменен, а перерисовку панели делал именно он
        self.__refresh_panel()


class GetFavoriteStickersHook(MethodHook):
    def __init__(self, get_favorite_stickers, get_account_id, import_stickers):
        self.__get_favorite_stickers = get_favorite_stickers
        self.__get_account_id = get_account_id
        self.__import_stickers = import_stickers

    def after_hooked_method(self, param):
        # Хук висит на всех перегрузках getRecentStickers / getRecentStickersNoCopy,
        # а тип списка везде идет первым аргументом (TYPE_FAVE = 2).
        # Недавние, маски и премиум-стикеры не наши - их подменять нельзя.
        if param.args[0] != 2:
            return
        try:
            self.__replace_with_saved(param)
        except Exception as e:
            # Без setResult останется ванильный список - урезанный, но живой
            log(f"[favstickers] Не удалось прочитать избранное: {e!r}")

    def __replace_with_saved(self, param):
        account = self.__get_account_id()
        favorite_stickers = self.__get_favorite_stickers(account)
        if not favorite_stickers:
            # Пустая база - это еще не заполненная база. Засеваем ее из
            # ванильного списка и пропускаем его дальше: на загрузке плагина
            # он мог быть еще не подтянут с сервера, а здесь он уже готов
            self.__import_stickers(param.getResult(), account)
            return
        new_list = jclass("java.util.ArrayList")()
        for sticker in favorite_stickers:
            new_list.add(sticker)
        param.setResult(new_list)


class IsStickerInFavoritesHook(MethodHook):
    def __init__(self, is_favorite_sticker, get_account_id):
        self.__is_favorite_sticker = is_favorite_sticker
        self.__get_account_id = get_account_id

    def before_hooked_method(self, param):
        try:
            favorite = self.__is_favorite_sticker(param.args[0], self.__get_account_id())
        except Exception as e:
            # Без setResult ответит оригинал: соврет про наши стикеры,
            # но меню стикера откроется
            log(f"[favstickers] Не удалось проверить избранное: {e!r}")
            return
        param.setResult(favorite)


class MyPlugin(BasePlugin):
    __DB = None

    def __get_context(self):
        """
        Возвращает контекст приложения
        """
        current_app = jclass("android.app.ActivityThread").currentApplication()
        if not current_app:
            raise RuntimeError("app not find")
        return current_app

    @property
    def db(self):
        if self.__DB is None:
            self.__DB = StickersDB(
                os.path.join(str(self.__get_context().getFilesDir()), "stickers.json")
            )
        return self.__DB

    @staticmethod
    def __get_current_account() -> int:
        UserConfig = find_class("org.telegram.messenger.UserConfig")
        # currentAccount — это индекс аккаунта (обычно 0)
        return UserConfig.selectedAccount

    @staticmethod
    def __get_current_account_id() -> str:
        UserConfig = find_class("org.telegram.messenger.UserConfig")
        user_id = UserConfig.getInstance(UserConfig.selectedAccount).clientUserId
        return str(user_id)

    @staticmethod
    def __notify_favorites_changed():
        """Просит панель стикеров перечитать избранное

        В стоке это уведомление посылал addRecentSticker, а мы его
        отменяем - без замены список не перерисуется, пока панель не
        закрыть и открыть заново.

        jint обязателен: postNotificationName принимает Object..., мост
        боксирует питоновский int в Long, а слушатель делает
        (Integer) args[1] и падает с ClassCastException внутри UI.
        """
        TYPE_FAVE = 2
        NotificationCenter = find_class("org.telegram.messenger.NotificationCenter")
        center = NotificationCenter.getInstance(MyPlugin.__get_current_account())
        center.postNotificationName(
            NotificationCenter.recentDocumentsDidLoad, False, jint(TYPE_FAVE)
        )

    def __import_vanilla_favorites(self, stickers, account: str):
        """Первичное заполнение базы ванильным избранным

        Зовется из хука на чтение списка, а не при загрузке плагина: там
        recentStickers еще мог не подтянуться с сервера, и импорт молча
        забирал пустоту или огрызок.
        """
        if stickers is None:
            return
        # С конца, чтобы порядок в базе совпал с порядком в панели
        for i in range(stickers.size() - 1, -1, -1):
            self.db.add_sticker(stickers.get(i), account)

    def __hook_one(self, method, hook, name: str, unhooks: list):
        """Перехват конкретной сигнатуры.

        hook_method умеет вернуть None вместо исключения - для нас это
        такой же отказ, молчать о нем нельзя.
        """
        unhook = self.hook_method(method, hook)
        if unhook is None:
            raise RuntimeError(f"не удалось перехватить {name}")
        unhooks.append(unhook)

    def __hook_by_name(self, media_class, name: str, hook, unhooks: list, required=True):
        """Перехват всех перегрузок метода по имени"""
        installed = self.hook_all_methods(media_class, name, hook)
        if not installed:
            if required:
                raise RuntimeError(f"не удалось перехватить {name}")
            # Метод есть не во всех сборках, и без него теряется только
            # часть функциональности - не повод отказывать в загрузке
            log(f"[favstickers] {name} перехватить не удалось, пропускаем")
            return
        unhooks.extend(installed)

    def on_plugin_load(self):
        MediaController = find_class("org.telegram.messenger.MediaDataController")
        media_instance = MediaController.getInstance(self.__get_current_account())
        media_class = media_instance.getClass()

        # Список копится по ходу установки, чтобы откатить уже поставленное:
        # полурабочий плагин хуже неработающего. Если оборвать загрузку на
        # середине, добавление продолжит уходить в базу, а список и звездочка
        # будут браться из ванили - и пользователь увидит вечные пять стикеров
        unhooks = []
        try:
            self.__install_hooks(media_class, media_instance, unhooks)
        except Exception:
            for unhook in unhooks:
                self.unhook_method(unhook)
            raise

    def __install_hooks(self, media_class, media_instance, unhooks: list):
        """Ставит все хуки, складывая хендлы отката в unhooks

        Хендлы нужны только на время загрузки: при выгрузке плагина мы хуки
        намеренно не снимаем.
        """
        TLRPCDocument = find_class("org.telegram.tgnet.TLRPC$Document")

        # Перехват метода добавления стикера в избраные
        addRecentStickerMethod = media_class.getDeclaredMethod(
            "addRecentSticker",
            J.Integer.TYPE,
            J.Object,
            TLRPCDocument,
            J.Integer.TYPE,
            J.Boolean.TYPE,
        )
        addRecentStickerMethod.setAccessible(True)
        self.__hook_one(
            addRecentStickerMethod,
            ChangeFavoriteStickerHook(
                on_add_favorite=self.db.add_sticker,
                on_remove_favorite=self.db.remove_sticker,
                refresh_panel=self.__notify_favorites_changed,
                get_account_id=self.__get_current_account_id,
            ),
            "addRecentSticker",
            unhooks,
        )

        # Перехват методов получения списка избранных стикеров.
        # hook_all_methods, а не getDeclaredMethod: набор перегрузок
        # getRecentStickers в разных сборках exteraGram отличается, и привязка
        # к одной сигнатуре молча промахивается мимо той, которую зовет UI.
        # getRecentStickersNoCopy идет тем же хуком - через него читают
        # избранное подсказки стикеров по эмодзи.
        # NoCopy не обязателен: без него избранное не увидят только подсказки
        for method_name, required in (
            ("getRecentStickers", True),
            ("getRecentStickersNoCopy", False),
        ):
            self.__hook_by_name(
                media_class,
                method_name,
                GetFavoriteStickersHook(
                    self.db.get_all_stickers,
                    self.__get_current_account_id,
                    self.__import_vanilla_favorites,
                ),
                unhooks,
                required=required,
            )

        # Перехват метода проверки наличия стикера в избранных
        isStickerInFavoritesMethod = media_class.getDeclaredMethod(
            "isStickerInFavorites", jclass("org.telegram.tgnet.TLRPC$Document")
        )
        isStickerInFavoritesMethod.setAccessible(True)
        self.__hook_one(
            isStickerInFavoritesMethod,
            IsStickerInFavoritesHook(
                self.db.is_sticker_favorite, self.__get_current_account_id
            ),
            "isStickerInFavorites",
            unhooks,
        )
