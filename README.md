# Unlim favorite stickers

Плагин для [exteraGram](https://exteragram.app) убирающий лимит на добавление стикеров в избранные

> [!CAUTION]
> Стикеры сохраняются только локально, без синхронизации между устройствами

Скачать: [релизы](https://github.com/TelePlug/unlim-favorite-stickers/releases)

### ToDo
- [ ] Добавить возможность делать и восстанавливать бекапы
- [ ] Добавить авто-сохранение бекапов в избранное
- [ ] Сделать авто полученение нового access_hash
- [ ] Удаление стикера при обновление хеша если стикер не найден в паке

## Для разработчиков
### debug run
```
uv run extera ./unlim_favorite_stickers.plugin --debug

adb logcat | grep "D \[PyObject\]"
```

### Кеширование стикеров
Для кеширования и отправки стикера достаточно хранить:
- sticker.id
- sticker.dc_id
- sticker.access_hash

Чтобы он сразу красиво отображался при отправке:
- mime_type

Чтобы можно было восстановить access_id:
- stickerset_id
