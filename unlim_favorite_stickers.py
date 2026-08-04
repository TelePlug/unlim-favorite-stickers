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
Build = jclass("android.os.Build")
ContentValues = jclass("android.content.ContentValues")
Intent = jclass("android.content.Intent")
Uri = jclass("android.net.Uri")

# Бэкап избранных стикеров
EXPORT_COMMAND = ".sticker_export"
BACKUP_FILENAME = "favorites.stickers"
BACKUP_SUFFIX = ".stickers"
BACKUP_MIME_TYPE = "application/octet-stream"


def get_app_context():
    """Возвращает контекст приложения"""
    current_app = jclass("android.app.ActivityThread").currentApplication()
    if not current_app:
        raise RuntimeError("app not find")
    return current_app


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

    def count_stickers(self, account: int) -> int:
        """Количество избранных стикеров аккаунта"""
        return len(self.__stickers["accounts"].get(account, []))

    def export_account(self, account: int) -> bytes:
        """Содержимое .stickers-файла: плоский список сериализованных стикеров"""
        serialized = [
            self.__stickers["stickers"][i]
            for i in self.__stickers["accounts"].get(account, [])
            if i in self.__stickers["stickers"]
        ]
        return json.dumps(serialized, ensure_ascii=False).encode("utf-8")

    def import_account(self, raw: bytes | str, account: int) -> int:
        """Добавление стикеров из .stickers-файла, возвращает число новых"""
        serialized = json.loads(raw)
        if not isinstance(serialized, list):
            raise ValueError("ожидался список стикеров")

        added = 0
        changed = False
        for data in serialized:
            # Пропускаем мусор, а не роняем весь импорт из-за одной записи
            if not isinstance(data, dict) or "id" not in data:
                log(f"[favstickers] Пропущена некорректная запись при импорте: {data}")
                continue
            sticker_id = str(data["id"])
            if sticker_id not in self.__stickers["stickers"]:
                self.__stickers["stickers"][sticker_id] = data
                changed = True
            if account not in self.__stickers["accounts"]:
                self.__stickers["accounts"][account] = []
            if sticker_id not in self.__stickers["accounts"][account]:
                self.__stickers["accounts"][account].append(sticker_id)
                added += 1
                changed = True
        if changed:
            self.__save_db()
        return added


def _find_download_uri(resolver, MediaStore, filename: str):
    """
    Ищет content:// URI файла с заданным именем в MediaStore.Downloads.

    Не создаёт Java-массивы (String[] для projection/selectionArgs) - просто
    запрашивает все столбцы и все строки, а фильтрует по имени в Python.
    """
    cursor = resolver.query(
        MediaStore.Downloads.EXTERNAL_CONTENT_URI, None, None, None, None
    )
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
                return Uri.withAppendedPath(
                    MediaStore.Downloads.EXTERNAL_CONTENT_URI, str(row_id)
                )
    finally:
        cursor.close()
    return None


def write_public_download(context, filename: str, json_bytes: bytes) -> str:
    """
    Пишет файл в общую папку Download и возвращает путь или content:// URI.

    На Android 10+ (API 29+) прямая запись через File API блокируется Scoped
    Storage на части форков/устройств (например AyuGram) - используем
    MediaStore.Downloads, который создан специально для этого случая и не
    требует WRITE_EXTERNAL_STORAGE. На старых Android - обычная запись в файл.
    """
    if int(Build.VERSION.SDK_INT) < 29:
        path = "/storage/emulated/0/Download/" + filename
        with open(path, "wb") as f:
            f.write(json_bytes)
        return path

    MediaStore = jclass("android.provider.MediaStore")
    resolver = context.getContentResolver()

    # Файл с таким именем мог остаться от прошлого экспорта - удаляем,
    # иначе система начнёт плодить "favorites (1).stickers"
    try:
        existing_uri = _find_download_uri(resolver, MediaStore, filename)
        if existing_uri is not None:
            resolver.delete(existing_uri, None, None)
    except Exception as e:
        log(f"[favstickers] Не удалось удалить старый файл экспорта: {e}")

    cv = ContentValues()
    cv.put(MediaStore.MediaColumns.DISPLAY_NAME, filename)
    cv.put(MediaStore.MediaColumns.MIME_TYPE, BACKUP_MIME_TYPE)
    cv.put(MediaStore.MediaColumns.RELATIVE_PATH, "Download/")
    target_uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, cv)
    if target_uri is None:
        raise IOError("MediaStore.insert вернул null, запись в Downloads не создана")

    pfd = resolver.openFileDescriptor(target_uri, "w")
    # detachFd передаёт владение нативным дескриптором нам
    with os.fdopen(pfd.detachFd(), "wb") as f:
        f.write(json_bytes)
    return str(target_uri)


def share_file(uri_string: str, mime_type: str = BACKUP_MIME_TYPE) -> bool:
    """
    Открывает родной экран выбора чата для отправки файла.

    setPackage на свой же пакет минует системный выбор "через какое приложение
    отправить": exteraGram сам обрабатывает ACTION_SEND и показывает тот же
    экран, что и при нажатии "Переслать".
    """
    try:
        fragment = get_last_fragment()
        activity = fragment.getParentActivity() if fragment else None
        context = activity if activity is not None else get_app_context()

        send_intent = Intent(Intent.ACTION_SEND)
        send_intent.setType(mime_type)
        send_intent.putExtra(Intent.EXTRA_STREAM, Uri.parse(uri_string))
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
    Очищает поле ввода и черновик после перехваченной команды.

    Отправку мы отменяем через HookStrategy.CANCEL, поэтому штатная логика
    Telegram по очистке ввода не срабатывает - делаем это вручную. Опирается
    на внутренние поля ChatActivity, поэтому каждый шаг отдельно защищён:
    на незнакомой сборке просто ничего не почистится.
    """
    fragment = get_last_fragment()
    if fragment is None:
        return

    try:
        MediaDataController = find_class("org.telegram.messenger.MediaDataController")
        mdc = MediaDataController.getInstance(account)
        dialog_id = fragment.getDialogId()
        try:
            mdc.cleanDraft(dialog_id, 0, False)
        except Exception:
            # На части версий сигнатура без topicId
            mdc.cleanDraft(dialog_id, False)
    except Exception as e:
        log(f"[favstickers] Не удалось очистить черновик: {e}")

    try:
        enter_view_field = None
        klass = fragment.getClass()
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


class ImportBackupHook(MethodHook):
    """Перехват тапа по .stickers-файлу в чате: предлагает импортировать бэкап"""

    def __init__(self, import_backup, get_account_id, get_account_slot, on_update):
        self.__import_backup = import_backup
        # account_id - это clientUserId, которым StickersDB ключует стикеры,
        # account_slot - индекс слота аккаунта, его ждут getInstance у
        # телеграмовских контроллеров. Это разные числа, путать их нельзя
        self.__get_account_id = get_account_id
        self.__get_account_slot = get_account_slot
        self.__on_update = on_update

    def before_hooked_method(self, param):
        try:
            if not param.args:
                return
            file_path = self.__resolve_path(param.args[0])
            if file_path is None:
                return

            # Файл наш - штатное открытие не нужно в любом случае
            param.setResult(False)

            if not os.path.exists(file_path):
                BulletinHelper.show_info(
                    "Файл ещё скачивается, дождитесь окончания загрузки",
                    get_last_fragment(),
                )
                return

            account = self.__get_account_id()
            run_on_ui_thread(lambda: self.__confirm_import(file_path, account))
        except Exception as e:
            log(f"[favstickers] Ошибка обработки тапа по файлу: {e}")

    def __resolve_path(self, arg) -> str | None:
        """Путь к .stickers-файлу из аргумента хука, иначе None"""
        if arg is None:
            return None

        if hasattr(arg, "getAbsolutePath"):
            path = str(arg.getAbsolutePath())
            return path if path.endswith(BACKUP_SUFFIX) else None

        arg_class = str(arg.getClass().getName()) if hasattr(arg, "getClass") else ""
        if "MessageObject" not in arg_class:
            return None

        name = str(arg.getDocumentName()) if hasattr(arg, "getDocumentName") else ""
        if not name.endswith(BACKUP_SUFFIX):
            return None

        # Уже скачанный файл лежит по attachPath, иначе спрашиваем FileLoader
        if getattr(getattr(arg, "messageOwner", None), "attachPath", None):
            return str(arg.messageOwner.attachPath)

        doc = arg.getDocument()
        if doc is None:
            return None
        try:
            FileLoader = find_class("org.telegram.messenger.FileLoader")
            loader = FileLoader.getInstance(self.__get_account_slot())
            return str(loader.getPathToAttach(doc, True).getAbsolutePath())
        except Exception as e:
            log(f"[favstickers] Не удалось определить путь к файлу: {e}")
            return None

    def __confirm_import(self, file_path: str, account):
        """Диалог подтверждения, а если Activity недоступна - импорт сразу"""
        try:
            from ui.alert import AlertDialogBuilder

            fragment = get_last_fragment()
            activity = fragment.getParentActivity() if fragment else None
            if activity is None:
                self.__import(file_path, account)
                return

            builder = AlertDialogBuilder(activity)
            builder.set_title("Импорт стикеров")
            builder.set_message(
                "Обнаружен файл резервной копии стикеров. Импортировать его?"
            )
            builder.set_positive_button(
                "Импортировать",
                lambda b, w: (b.dismiss(), self.__import(file_path, account)),
            )
            builder.set_negative_button("Отмена", lambda b, w: b.dismiss())
            builder.show()
        except Exception as e:
            log(f"[favstickers] Не удалось показать диалог импорта: {e}")
            self.__import(file_path, account)

    def __import(self, file_path: str, account):
        try:
            with open(file_path, "rb") as f:
                added = self.__import_backup(f.read(), account)
        except Exception as e:
            log(f"[favstickers] Ошибка импорта: {e}")
            BulletinHelper.show_error("Не удалось импортировать стикеры", get_last_fragment())
            return

        BulletinHelper.show_success(
            f"Импортировано стикеров: {added}" if added else "Новых стикеров нет",
            get_last_fragment(),
        )
        # Импорт уже состоялся - сбой обновления панели не должен
        # выглядеть как провал самого импорта
        try:
            self.__on_update()
        except Exception as e:
            log(f"[favstickers] Не удалось обновить панель стикеров: {e}")


class MyPlugin(BasePlugin):
    __DB = None

    @property
    def db(self):
        if self.__DB is None:
            self.__DB = StickersDB(
                os.path.join(str(get_app_context().getFilesDir()), "stickers.json")
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
        """Регистрация хуков экспорта по команде и импорта тапом по файлу"""
        # Официальный способ перехвата исходящих сообщений. Ручной перебор
        # перегрузок SendMessagesHelper.sendMessage через reflection оказался
        # ненадёжным на части сборок
        try:
            self.add_on_send_message_hook()
        except Exception as e:
            log(f"[favstickers] Не удалось перехватить отправку сообщений: {e}")

        # Перехват открытия файла: у openForView несколько перегрузок и на
        # форках их набор отличается, поэтому берём все по имени
        try:
            AndroidUtilities = J.Class.forName("org.telegram.messenger.AndroidUtilities")
            for method in AndroidUtilities.getMethods():
                if method.getName() != "openForView":
                    continue
                if len(method.getParameterTypes()) < 1:
                    continue
                try:
                    method.setAccessible(True)
                    self.hook_method(
                        method,
                        ImportBackupHook(
                            import_backup=self.db.import_account,
                            get_account_id=self.__get_current_account_id,
                            get_account_slot=self.__get_current_account,
                            on_update=media_instance.processLoadedRecentDocuments,
                        ),
                    )
                except Exception as e:
                    log(f"[favstickers] Не удалось захукать {method}: {e}")
        except Exception as e:
            log(f"[favstickers] Ошибка перехвата открытия файлов: {e}")

    def on_send_message_hook(self, account: int, params) -> HookResult:
        """Перехват команды экспорта вместо отправки её текста в чат"""
        if not isinstance(getattr(params, "message", None), str):
            return HookResult()
        if params.message.strip() != EXPORT_COMMAND:
            return HookResult()

        try:
            # account - индекс слота аккаунта, и это может быть не тот аккаунт,
            # что выбран на экране. Приводим к account_id, которым ключует база
            UserConfig = find_class("org.telegram.messenger.UserConfig")
            account_id = str(UserConfig.getInstance(account).clientUserId)

            clear_input_and_draft(account)

            if not self.db.count_stickers(account_id):
                BulletinHelper.show_error(
                    "Нет сохранённых стикеров для экспорта", get_last_fragment()
                )
                return HookResult(strategy=HookStrategy.CANCEL)

            uri = write_public_download(
                get_app_context(), BACKUP_FILENAME, self.db.export_account(account_id)
            )
            BulletinHelper.show_info(
                "Выберите чат для отправки файла", get_last_fragment()
            )
            share_file(uri)
        except Exception as e:
            log(f"[favstickers] Ошибка экспорта: {e}")
            BulletinHelper.show_error(
                "Не удалось выгрузить стикеры", get_last_fragment()
            )

        return HookResult(strategy=HookStrategy.CANCEL)


