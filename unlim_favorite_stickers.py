import json
import os

from android_utils import log
from base_plugin import BasePlugin, MethodHook, HookResult, HookStrategy
from hook_utils import find_class
from java import jclass
from ui.bulletin import BulletinHelper
from client_utils import get_last_fragment
from android_utils import run_on_ui_thread

__id__ = "favstickers"
__name__ = "Unlim favorite stickers"
__description__ = "Remove limits on adding stickers to favorites"
__author__ = "@DaShMore, @teleplugit & @InLoveWithUmi"
__version__ = "2.1.0"
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
                data["sticker_set_id"] = getattr(att.stickerset, "id")
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

    def __load_db(self):
        """Чтение всех стикеров из базы в self.stickers"""
        self.__stickers = {"accounts": {}, "stickers": {}}
        if not os.path.exists(self.__db_path):
            return
        try:
            with open(self.__db_path, encoding="utf-8") as f:
                self.__stickers = json.load(f)
        except (OSError, ValueError) as e:
            # Битый или недоступный файл - начинаем с пустой базы,
            # иначе плагин не загрузится вообще
            log(f"[favstickers] Не удалось прочитать базу стикеров: {e}")

    def __save_db(self):
        """Сохранение всех стикеров из self.stickers в базу"""
        tmp_path = self.__db_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.__stickers, f, ensure_ascii=False)
            # Подмена файла целиком: прерывание записи не оставит битую базу
            os.replace(tmp_path, self.__db_path)
        except OSError as e:
            log(f"[favstickers] Не удалось сохранить базу стикеров: {e}")

    def get_all_stickers(self, account: int) -> list[dict[str, str | int]]:
        """Получение всех стикеров в виде объектов TLRPC$TL_document"""
        return [
            deserialize_sticker(self.__stickers["stickers"][i])
            for i in reversed(self.__stickers["accounts"].get(account, []))
            if i in self.__stickers["stickers"]
        ]

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
    def __init__(self, on_add_favorite, on_remove_favorite, on_update, get_account_id):
        self.__on_add_favorite = on_add_favorite
        self.__on_remove_favorite = on_remove_favorite
        self.__on_update = on_update
        self.__get_account_id = get_account_id

    def before_hooked_method(self, param):
        sticker = param.args[2]
        account_id = self.__get_account_id()
        # Проверяем, что выбран пункт добавления / удаления из избранное (TYPE_FAVE = 2)
        # И что стикер не находится в избранном (not inFavs)
        if param.args[0] == 2 and not param.args[4]:
            self.__on_add_favorite(sticker, account_id)
            BulletinHelper.show_success("Sticker added to favorites")
        elif param.args[0] == 2:
            self.__on_remove_favorite(sticker, account_id)
            BulletinHelper.show_error("Sticker removed from favorites")
        self.__on_update()
        param.setResult(None)


class GetFavoriteStickersHook(MethodHook):
    def __init__(self, get_favorite_stickers, get_account_id):
        self.__get_favorite_stickers = get_favorite_stickers
        self.__get_account_id = get_account_id

    def after_hooked_method(self, param):
        account = self.__get_account_id()
        favorite_stickers = self.__get_favorite_stickers(account)
        if not favorite_stickers:
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
        sticker = param.args[0]
        account = self.__get_account_id()
        param.setResult(self.__is_favorite_sticker(sticker, account))


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

    def __load_favorite_stickers(self, mediaController):
        account = self.__get_current_account_id()
        if not self.db.get_all_stickers(account):
            stickers = mediaController.getRecentStickers(2)
            for sticker in range(stickers.size() - 1, -1, -1):
                self.db.add_sticker(stickers.get(sticker), account)

    def on_plugin_load(self):
        MediaController = find_class("org.telegram.messenger.MediaDataController")
        TLRPCDocument = find_class("org.telegram.tgnet.TLRPC$Document")
        media_instance = MediaController.getInstance(self.__get_current_account())
        media_class = media_instance.getClass()

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
        self.hook_method(
            addRecentStickerMethod,
            ChangeFavoriteStickerHook(
                on_add_favorite=self.db.add_sticker,
                on_remove_favorite=self.db.remove_sticker,
                on_update=media_instance.processLoadedRecentDocuments,
                get_account_id=self.__get_current_account_id,
            ),
        )

        # Перехват метода получения списка избранных стикеров
        getRecentStickersMethod = media_class.getDeclaredMethod(
            "getRecentStickers",
            J.Integer.TYPE,
        )
        getRecentStickersMethod.setAccessible(True)
        self.hook_method(
            getRecentStickersMethod,
            GetFavoriteStickersHook(
                self.db.get_all_stickers, self.__get_current_account_id
            ),
        )

        # Перехват метода проверки наличия стикера в избранных
        isStickerInFavoritesMethod = media_class.getDeclaredMethod(
            "isStickerInFavorites", jclass("org.telegram.tgnet.TLRPC$Document")
        )
        isStickerInFavoritesMethod.setAccessible(True)
        self.hook_method(
            isStickerInFavoritesMethod,
            IsStickerInFavoritesHook(
                self.db.is_sticker_favorite, self.__get_current_account_id
            ),
        )

        self.__load_favorite_stickers(media_instance)
        self.__setup_backup_hooks(media_instance)

    def __setup_backup_hooks(self, media_instance):
        """
        Регистрирует хуки для команды .sticker_export / импорта .stickers-файла.
        Не трогает StickersDB - использует только её публичный API
        (get_all_stickers/add_sticker), которое уже существует в этом классе.
        """
        # Официальный, документированный способ перехвата исходящих сообщений
        # (вместо ручного перебора всех перегрузок SendMessagesHelper.sendMessage
        # через reflection, который оказался ненадёжным на части сборок).
        try:
            self.add_on_send_message_hook()
            log("[favstickers] add_on_send_message_hook зарегистрирован")
        except Exception as e:
            log(f"[favstickers] Не удалось зарегистрировать add_on_send_message_hook: {e}")

        try:
            JavaClassRef = jclass("java.lang.Class")
            AndroidUtilitiesClass = JavaClassRef.forName("org.telegram.messenger.AndroidUtilities")
            for m in AndroidUtilitiesClass.getMethods():
                if m.getName() == "openForView" and len(m.getParameterTypes()) >= 1:
                    try:
                        m.setAccessible(True)
                        self.hook_method(
                            m,
                            AndroidOpenFileHook(
                                import_func=self.db.add_sticker,
                                get_account_id=self.__get_current_account_id,
                                media_update_func=media_instance.processLoadedRecentDocuments,
                            ),
                        )
                    except Exception as e:
                        log(f"[favstickers] Не удалось захукать перегрузку openForView {m}: {e}")
        except Exception as e:
            log(f"[favstickers] Ошибка инжекции импорта UI: {e}")

    def on_send_message_hook(self, account: int, params) -> HookResult:
        """
        Официальный хук исходящих сообщений. Ловит команду .sticker_export
        и отменяет обычную отправку (HookStrategy.CANCEL), вместо неё запускает
        экспорт бэкапа избранных стикеров.
        """
        if not hasattr(params, "message") or not isinstance(params.message, str):
            return HookResult()
        if params.message.strip() != ".sticker_export":
            return HookResult()

        try:
            # account тут - индекс слота аккаунта (может быть не тем, что выбран
            # на экране, если сработало для фонового аккаунта) - конвертируем
            # его в тот же account_id (user_id), которым StickersDB ключует стикеры.
            UserConfig = find_class("org.telegram.messenger.UserConfig")
            account_id = str(UserConfig.getInstance(account).clientUserId)

            clear_input_and_draft(account)

            stickers = self.db.get_all_stickers(account_id)
            if not stickers:
                BulletinHelper.show_error("Нет сохранённых стикеров для экспорта", get_last_fragment())
                return HookResult(strategy=HookStrategy.CANCEL)

            serialized = [serialize_sticker(s) for s in stickers]
            json_bytes = json.dumps(serialized, ensure_ascii=False).encode("utf-8")

            context = self.__get_context()
            uri = write_public_download(context, "favorites.stickers", json_bytes)

            BulletinHelper.show_info("Выберите чат для отправки файла", get_last_fragment())
            share_file(uri)
        except Exception as e:
            log(f"[favstickers] on_send_message_hook Error: {e}")

        return HookResult(strategy=HookStrategy.CANCEL)


# Новый функционал: экспорт/импорт бэкапа избранных стикеров.
# Использует только публичный API StickersDB (get_all_stickers/add_sticker)
# и уже существующие serialize_sticker/deserialize_sticker - сам класс
# StickersDB и остальная логика плагина выше не изменены.


def _find_download_uri(resolver, MediaStore, filename: str):
    """
    Ищет content:// URI файла с заданным именем в MediaStore.Downloads.
    Не создаёт Java-массивы (String[] для projection/selectionArgs) - просто
    запрашивает все столбцы и все строки, а фильтрует по имени в Python.
    """
    cursor = resolver.query(MediaStore.Downloads.EXTERNAL_CONTENT_URI, None, None, None, None)
    if cursor is None:
        return None
    try:
        name_idx = cursor.getColumnIndex("_display_name")
        id_idx = cursor.getColumnIndex("_id")
        if name_idx < 0 or id_idx < 0:
            return None
        while cursor.moveToNext():
            if str(cursor.getString(name_idx)) == filename:
                row_id = cursor.getLong(id_idx)
                Uri = jclass("android.net.Uri")
                return Uri.withAppendedPath(MediaStore.Downloads.EXTERNAL_CONTENT_URI, str(row_id))
    finally:
        cursor.close()
    return None


def write_public_download(context, filename: str, json_bytes: bytes) -> str:
    """
    Пишет файл в общую папку Download.
    На Android 10+ (API 29+) прямая запись через File API блокируется Scoped
    Storage на части форков/устройств (например AyuGram) - используем
    MediaStore.Downloads, который создан специально для этого случая и не
    требует WRITE_EXTERNAL_STORAGE. На старых Android - обычная запись в файл.
    """
    Build = jclass("android.os.Build")
    sdk_int = int(Build.VERSION.SDK_INT)

    if sdk_int >= 29:
        ContentValues = jclass("android.content.ContentValues")
        MediaStore = jclass("android.provider.MediaStore")
        resolver = context.getContentResolver()

        try:
            existing_uri = _find_download_uri(resolver, MediaStore, filename)
            if existing_uri is not None:
                resolver.delete(existing_uri, None, None)
        except Exception as e:
            log(f"[favstickers] Не удалось удалить старый файл экспорта: {e}")

        cv = ContentValues()
        cv.put(MediaStore.MediaColumns.DISPLAY_NAME, filename)
        cv.put(MediaStore.MediaColumns.MIME_TYPE, "application/octet-stream")
        cv.put(MediaStore.MediaColumns.RELATIVE_PATH, "Download/")
        target_uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, cv)
        if target_uri is None:
            raise IOError("MediaStore.insert вернул null - не удалось создать запись в Downloads")

        pfd = resolver.openFileDescriptor(target_uri, "w")
        fd = pfd.detachFd()
        with os.fdopen(fd, "wb") as f:
            f.write(json_bytes)
        return str(target_uri)
    else:
        path = "/storage/emulated/0/Download/" + filename
        with open(path, "wb") as f:
            f.write(json_bytes)
        return path


def share_file(uri_string: str, mime_type: str = "application/octet-stream"):
    """
    Отправляет файл сразу в само приложение (setPackage на свой же пакет),
    минуя системный выбор "через какое приложение отправить". Telegram/AyuGram/
    exteraGram сами обрабатывают ACTION_SEND и показывают свой родной экран
    выбора чата - точно так же, как при нажатии "Переслать".
    """
    try:
        Intent = jclass("android.content.Intent")
        Uri = jclass("android.net.Uri")
        uri = Uri.parse(uri_string)

        fragment = get_last_fragment()
        activity = fragment.getParentActivity() if fragment else None
        context = activity if activity is not None else jclass("android.app.ActivityThread").currentApplication()

        send_intent = Intent(Intent.ACTION_SEND)
        send_intent.setType(mime_type)
        send_intent.putExtra(Intent.EXTRA_STREAM, uri)
        send_intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        send_intent.setPackage(context.getPackageName())

        if activity is None:
            send_intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

        context.startActivity(send_intent)
        return True
    except Exception as e:
        log(f"[favstickers] Ошибка открытия меню отправки: {e}")
        return False


def clear_input_and_draft(account: int):
    """
    Мы перехватываем sendMessage очень глубоко и полностью подменяем результат,
    из-за чего обычная логика Telegram (очистка поля ввода + черновика после
    отправки) не успевает сработать сама. Чистим руками.
    """
    try:
        fragment = get_last_fragment()
        if fragment is None:
            return

        try:
            dialog_id = fragment.getDialogId()
            MediaDataController = find_class("org.telegram.messenger.MediaDataController")
            mdc = MediaDataController.getInstance(account)
            try:
                mdc.cleanDraft(dialog_id, 0, False)
            except Exception:
                mdc.cleanDraft(dialog_id, False)
        except Exception as e:
            log(f"[favstickers] Не удалось очистить черновик: {e}")

        try:
            fragment_class = fragment.getClass()
            enter_view_field = None
            klass = fragment_class
            while klass is not None and enter_view_field is None:
                try:
                    enter_view_field = klass.getDeclaredField("chatActivityEnterView")
                except Exception:
                    klass = klass.getSuperclass()
            if enter_view_field is not None:
                enter_view_field.setAccessible(True)
                enter_view = enter_view_field.get(fragment)
                if enter_view is not None:
                    enter_view.setFieldText("")
        except Exception as e:
            log(f"[favstickers] Не удалось очистить поле ввода: {e}")

    except Exception as e:
        log(f"[favstickers] clear_input_and_draft общая ошибка: {e}")


class AndroidOpenFileHook(MethodHook):
    """Перехватывает тап по .stickers-файлу в чате и предлагает импортировать."""

    def __init__(self, import_func, get_account_id, media_update_func):
        self.import_func = import_func
        self.get_account_id = get_account_id
        self.media_update = media_update_func

    def before_hooked_method(self, param):
        try:
            if not param.args or param.args[0] is None:
                return

            arg = param.args[0]
            arg_class = str(arg.getClass().getName()) if hasattr(arg, "getClass") else ""
            file_path = None

            if "MessageObject" in arg_class:
                name = str(arg.getDocumentName()) if hasattr(arg, "getDocumentName") else ""
                if name.endswith(".stickers"):
                    if hasattr(arg, "messageOwner") and hasattr(arg.messageOwner, "attachPath") and arg.messageOwner.attachPath:
                        file_path = str(arg.messageOwner.attachPath)
                    else:
                        FileLoader = find_class("org.telegram.messenger.FileLoader")
                        doc = arg.getDocument()
                        if doc:
                            try:
                                file_path = str(FileLoader.getInstance(int(self.get_account_id())).getPathToAttach(doc, True).getAbsolutePath())
                            except Exception:
                                pass
            elif hasattr(arg, "getAbsolutePath"):
                path = str(arg.getAbsolutePath())
                if path.endswith(".stickers"):
                    file_path = path

            if not file_path or not file_path.endswith(".stickers"):
                return

            param.setResult(False)

            if not os.path.exists(file_path):
                BulletinHelper.show_info("Файл еще скачивается! Дождитесь окончания загрузки.", get_last_fragment())
                return

            account = self.get_account_id()

            def do_import():
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        serialized_list = json.load(f)
                    for data in serialized_list:
                        sticker = deserialize_sticker(data)
                        self.import_func(sticker, account)
                except Exception as e:
                    log(f"[favstickers] Ошибка импорта: {e}")
                    BulletinHelper.show_error("Ошибка при импорте.", get_last_fragment())
                    return

                # Импорт уже успешен на этом моменте - дальше только обновление UI,
                # его ошибка не должна выглядеть как провал самого импорта.
                BulletinHelper.show_success("Стикеры успешно импортированы! Откройте панель стикеров.", get_last_fragment())
                try:
                    self.media_update()
                except Exception as e:
                    log(f"[favstickers] Не удалось обновить UI сразу (стикеры всё равно импортированы): {e}")

            def show_import_dialog():
                try:
                    from ui.alert import AlertDialogBuilder

                    fragment = get_last_fragment()
                    activity = fragment.getParentActivity() if fragment else None
                    if not activity:
                        do_import()
                        return

                    builder = AlertDialogBuilder(activity)
                    builder.set_title("Импорт стикеров")
                    builder.set_message("Обнаружен файл резервной копии .stickers. Импортировать его?")
                    builder.set_positive_button("Импортировать", lambda b, w: (b.dismiss(), do_import()))
                    builder.set_negative_button("Отмена", lambda b, w: b.dismiss())
                    builder.show()
                except Exception as e:
                    log(f"[favstickers] Dialog UI Exception: {e}")
                    do_import()

            run_on_ui_thread(show_import_dialog)
        except Exception as e:
            log(f"[favstickers] AndroidOpenFileHook Error: {e}")