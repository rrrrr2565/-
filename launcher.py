# -*- coding: utf-8 -*-
"""
MyLauncher — простой десктопный лаунчер Minecraft
Стиль: тёмный Minecraft (тёмный фон, зелёная кнопка ИГРАТЬ)
Функции:
  - список версий: релизы, снапшоты, Fabric, Forge (+ автообновление списка)
  - скачивание и установка выбранной версии
  - запуск игры (оффлайн-вход по нику)
  - полный каталог модов Modrinth: поиск, пагинация,
    автоматическая загрузка обязательных зависимостей
  - улучшенная консоль: время, цвета по типу события,
    фильтр сообщений, сохранение лога в файл
  - авто-загрузка Java: если системной Java нет или она старая,
    лаунчер сам скачивает официальный рантайм Mojang
  - при установке мода старые версии этого же мода удаляются из mods
  - тихий лог игры (1.4.2): без двойного времени, спама и offline-шума
"""

import os
import re
import sys
import json
import time
import queue
import shutil
import threading
import subprocess
from collections import deque

try:
    import requests
except ImportError:
    print("Не найден requests. Установите: pip install requests")
    sys.exit(1)

try:
    import customtkinter as ctk
except ImportError:
    print("Не найден customtkinter. Установите: pip install customtkinter")
    sys.exit(1)

try:
    import minecraft_launcher_lib as mll
except ImportError:
    print("Не найден minecraft-launcher-lib. Установите: pip install minecraft-launcher-lib")
    sys.exit(1)


# ----------------------------------------------------------------------------
# Настройки
# ----------------------------------------------------------------------------
APP_NAME = "MyLauncher"
APP_VERSION = "1.4.2"

# Безвредный шум offline-режима: ошибки авторизации/Realms/PDH не крашат игру,
# поэтому в лог они не попадают (в конце игры пишется только их количество)
GAME_NOISE_RE = re.compile(
    r"InvalidCredentialsException"
    r"|Failed to fetch user properties"
    r"|Could not authorize you against Realms"
    r"|RealmsServiceException"
    r"|Failed to fetch Realms feature flags"
    r"|Couldn'?t connect to realms"
    r"|PDH Counter|PDH counter"
    r"|Win32_PerfRawData"
    r"|COM exception",
    re.IGNORECASE,
)

MODRINTH_API = "https://api.modrinth.com/v2"
MODRINTH_HEADERS = {"User-Agent": f"{APP_NAME}/{APP_VERSION} (Minecraft launcher)"}

# --- Обновление по воздуху ---
# Файлы программы, которые обновляет самообновление (папка .minecraft не трогается)
UPDATABLE_FILES = ["launcher.py", "README.md", "requirements.txt", "run.bat", "build_exe.bat"]
LOCAL_UPDATE_ZIP = "MyLauncher-update.zip"   # если этот файл лежит рядом с launcher.py — обновится из него
# GitHub-репозиторий обновлений: лаунчер сам проверяет манифест и качает новые файлы
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/rrrrr2565/-/main/version.json"
# URL можно также вписать в файл update_url.txt в папке launcher_data — код менять не нужно

# Каталог модов берётся живым поиском с Modrinth — новых модов подхватывать не нужно.
# Количество модов на страницу каталога
MODS_PAGE_SIZE = 25


def _app_dir() -> str:
    """Папка самого лаунчера.
    Для exe (PyInstaller onefile) это папка exe — НЕ временная папка _MEI,
    которая удаляется при выходе (из-за этого пропадали .minecraft и логи)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR_NAME = "launcher_data"
# Служебные файлы лаунчера храним в подпапке launcher_data,
# чтобы не захламлять папку программы (логи, маркеры, бэкапы).
_DATA_FILES = [
    "launcher.log", "launcher.log.old", "launcher_log.txt",
    "last_mc_version.txt", "launcher_dir.txt", "update_url.txt",
    "launcher.py.bak", LOCAL_UPDATE_ZIP, LOCAL_UPDATE_ZIP + ".applied",
]

# Папка игры: по умолчанию стандартный .minecraft рядом с лаунчером,
# свой путь можно указать в файле launcher_data/launcher_dir.txt.
DEFAULT_MC_DIR = os.path.join(_app_dir(), ".minecraft")


def _data_dir() -> str:
    d = os.path.join(_app_dir(), DATA_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def migrate_old_files() -> int:
    """Разово переносит служебные файлы из корня программы в launcher_data."""
    moved = 0
    for name in _DATA_FILES:
        src = os.path.join(_app_dir(), name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(_data_dir(), name)
        try:
            if os.path.isfile(dst):
                os.remove(src)          # в launcher_data уже свежий — дубликат убираем
            else:
                os.replace(src, dst)
            moved += 1
        except OSError:
            pass
    return moved


def get_minecraft_dir() -> str:
    marker = os.path.join(_data_dir(), "launcher_dir.txt")
    if os.path.isfile(marker):
        with open(marker, "r", encoding="utf-8") as f:
            custom = f.read().strip()
        if custom:
            return custom
    return DEFAULT_MC_DIR


# ----------------------------------------------------------------------------
# Callback прогресса установки для minecraft-launcher-lib (v8: dict с функциями)
# ----------------------------------------------------------------------------
class InstallProgress:
    """Собирает callback-dict: {"setStatus", "setMax", "setProgress"} для GUI."""

    def __init__(self, on_status, on_progress):
        self._on_status = on_status
        self._on_progress = on_progress
        self._max_value = 0

    def build(self) -> dict:
        return {
            "setStatus": self._set_status,
            "setMax": self._set_max,
            "setProgress": self._set_value,
        }

    def _set_status(self, text):
        self._on_status(str(text))

    def _set_max(self, value):
        self._max_value = int(value)

    def _set_value(self, value):
        if self._max_value > 0:
            self._on_progress(int(value) / self._max_value)


def get_offline_uuid(username: str) -> str:
    """UUID для оффлайн-режима — как Minecraft считает по алгоритму OfflinePlayer."""
    import hashlib
    import uuid as uuid_mod

    data = hashlib.md5(("OfflinePlayer:" + username).encode("utf-8")).digest()
    b = bytearray(data)
    b[6] = (b[6] & 0x0F) | 0x30
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid_mod.UUID(bytes=bytes(b)))


def search_mods(query: str, mc_version: str, loader: str,
                offset: int = 0, limit: int = MODS_PAGE_SIZE,
                use_version_filter: bool = True):
    """Живой поиск по каталогу Modrinth.
    Возвращает (список хитов, общее число найденного).
    Хит: {slug, title, description, downloads}."""
    facets = [["project_type:mod"], [f"categories:{loader}"]]
    if use_version_filter:
        facets.append([f"versions:{mc_version}"])
    params = {
        "limit": limit,
        "offset": offset,
        "index": "relevance" if query else "downloads",
        "facets": json.dumps(facets),
    }
    if query:
        params["query"] = query
    r = requests.get(f"{MODRINTH_API}/search", params=params,
                     headers=MODRINTH_HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    hits = [{
        "slug": h.get("slug") or h.get("project_id"),
        "title": h.get("title", ""),
        "description": h.get("description", ""),
        "downloads": h.get("downloads", 0),
    } for h in data.get("hits", [])]
    return hits, int(data.get("total_hits", 0))


def resolve_mod_version(slug: str, mc_version: str, loader: str):
    """Последняя совместимая версия мода.
    Возвращает dict {filename, url, deps} или None, если версии нет.
    deps — project_id обязательных зависимостей."""
    r = requests.get(
        f"{MODRINTH_API}/project/{slug}/version",
        params={
            "loaders": json.dumps([loader]),
            "game_versions": json.dumps([mc_version]),
        },
        headers=MODRINTH_HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    versions = r.json()
    if not versions:
        return None
    v = versions[0]
    files = v.get("files", [])
    primary = None
    for f in files:
        if f.get("primary"):
            primary = f
            break
    if primary is None and files:
        primary = files[0]
    if primary is None:
        return None
    deps = [d["project_id"] for d in v.get("dependencies", [])
            if d.get("dependency_type") == "required" and d.get("project_id")]
    return {"filename": primary["filename"], "url": primary["url"], "deps": deps}


def get_project_title(pid_or_slug: str) -> str:
    """Человекочитаемое название мода по slug или project_id."""
    try:
        r = requests.get(f"{MODRINTH_API}/project/{pid_or_slug}",
                         headers=MODRINTH_HEADERS, timeout=20)
        r.raise_for_status()
        return r.json().get("title") or pid_or_slug
    except Exception:
        return pid_or_slug


def collect_mods_with_deps(slugs, mc_version: str, loader: str, log_fn=print):
    """Строит план скачивания: выбранные моды + цепочка обязательных зависимостей.
    Обходит зависимости в ширину (BFS), дубликаты и циклы отсекает.
    Возвращает список dict: {slug, title, filename, url, is_dep}."""
    chosen = set(slugs)
    plan = []
    taken_files = set()
    seen = set()
    q = deque(slugs)
    while q:
        proj = q.popleft()
        if proj in seen:
            continue
        seen.add(proj)
        info = resolve_mod_version(proj, mc_version, loader)
        if info is None:
            log_fn(f"[МОД] {proj}: нет версии для MC {mc_version} ({loader}) — пропускаю")
            continue
        if info["filename"] in taken_files:
            continue
        taken_files.add(info["filename"])
        title = get_project_title(proj)
        kind = "Мод" if proj in chosen else "Зависимость"
        log_fn(f"[МОД] {kind} {title}: выбран {info['filename']}")
        plan.append({
            "slug": proj,
            "title": title,
            "filename": info["filename"],
            "url": info["url"],
            "is_dep": proj not in chosen,
        })
        for dep in info["deps"]:
            if dep not in seen:
                q.append(dep)
    return plan


def download_mod_file(url: str, dest_path: str):
    """Скачивает файл мода с потоком (для больших jar)."""
    with requests.get(url, headers=MODRINTH_HEADERS, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)


def mod_base_name(filename: str) -> str:
    """«fabric-api-0.167.0+26.3.jar» -> «fabricapi» (имя мода без версии)."""
    stem = filename
    if stem.lower().endswith(".jar"):
        stem = stem[:-4]
    # отрезаем версию: всё после первого разделителя, за которым идёт цифра
    stem = re.split(r"[-_.+]\d", stem, maxsplit=1)[0]
    return re.sub(r"[^a-z0-9]", "", stem.lower())


def remove_old_mod_files(mods_dir: str, slug: str, title: str,
                         keep_filename: str, log_fn=print) -> int:
    """Удаляет из mods старые jar того же мода (другие версии),
    чтобы два одинаковых мода не конфликтовали при запуске игры."""
    targets = {mod_base_name(slug), mod_base_name(title)}
    targets.discard("")
    keep = os.path.basename(keep_filename).lower()
    removed = 0
    try:
        entries = sorted(os.listdir(mods_dir))
    except OSError:
        return 0
    for fn in entries:
        if not fn.lower().endswith((".jar", ".disabled")):
            continue
        if fn.lower() == keep:
            continue
        if mod_base_name(fn) in targets:
            path = os.path.join(mods_dir, fn)
            try:
                os.remove(path)
                removed += 1
                log_fn(f"[МОД] Удалён старый {fn} (заменён на {keep})")
            except OSError as e:
                log_fn(f"[МОД] Не удалось удалить старый {fn}: {e}")
    return removed


def get_mod_context(version_type: str, version: str):
    """Определяет (версия MC, загрузчик) для модов или None, если моды не поставить."""
    if version_type == "Forge" and "-" in version:
        return version.split("-")[0], "forge"
    if version_type == "Fabric":
        return version, "fabric"
    return None


def version_manifest_order() -> dict:
    """Словарь «id версии -> позиция в манифесте Mojang» (новые — первыми)."""
    try:
        return {v["id"]: i for i, v in enumerate(mll.utils.get_version_list())}
    except Exception:
        return {}


def sort_versions_like_manifest(ids):
    """Сортирует список версий как в манифесте Mojang (новые сверху).
    Неизвестные id уходят в конец, сохраняя исходный порядок."""
    order = version_manifest_order()
    return sorted(ids, key=lambda v: order.get(v, 10 ** 9))


def sort_forge_versions(versions):
    """Forge «1.20.1-47.2.0»: сначала новые версии MC, потом большие билды."""
    order = version_manifest_order()

    def key(v):
        base, _, build = v.partition("-")
        nums = tuple(int(x) for x in re.findall(r"\d+", build))
        # минусы: сравнение по убыванию (новый билд выше)
        return (order.get(base, 10 ** 9), tuple(-n for n in nums))

    return sorted(versions, key=key)


def java_major_version(java_path: str):
    """Мажорная версия Java (8, 17, 21, 25...) или None."""
    try:
        out = subprocess.run(
            [java_path, "-version"], capture_output=True,
            text=True, timeout=20, errors="replace",
        )
        text = (out.stderr or "") + (out.stdout or "")
        m = re.search(r'version "([^"]+)"', text)
        if not m:
            return None
        parts = m.group(1).split(".")
        if parts and parts[0] == "1" and len(parts) > 1:
            parts = parts[1:]          # «1.8.0_392» -> 8
        if parts and parts[0].isdigit():
            return int(parts[0])
        return None
    except Exception:
        return None


def get_version_release_year(version_id: str):
    """Год выпуска версии (или None, если не удалось определить)."""
    import datetime

    try:
        for v in mll.utils.get_version_list():
            if v["id"] == version_id:
                rt = v.get("releaseTime")
                if isinstance(rt, datetime.datetime):
                    return rt.year
                if isinstance(rt, str) and len(rt) >= 4 and rt[:4].isdigit():
                    return int(rt[:4])
    except Exception:
        pass
    return None


def _make_png(width: int, height: int) -> bytes:
    """Собирает валидный PNG без сторонних библиотек — квадратная текстура земли."""
    import struct
    import zlib

    raw = b""
    for y in range(height):
        raw += b"\x00"  # фильтр строки 0
        for x in range(width):
            base = 96 + ((x * 7 + y * 13) % 48)
            raw += bytes((base, base // 2, 32))

    def chunk(tag: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + tag + data
        out += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return out

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def ensure_legacy_icons(mc_dir: str):
    """Старые версии ищут иконки в assets/virtual/legacy/icons — если их нет,
    игра падает с javax.imageio.IIOException. Создаём заглушки."""
    icons_dir = os.path.join(mc_dir, "assets", "virtual", "legacy", "icons")
    os.makedirs(icons_dir, exist_ok=True)
    for name, size in (("icon_16x16.png", 16), ("icon_32x32.png", 32)):
        path = os.path.join(icons_dir, name)
        if not os.path.isfile(path):
            with open(path, "wb") as f:
                f.write(_make_png(size, size))


# ----------------------------------------------------------------------------
# Обновление по воздуху (OTA)
# ----------------------------------------------------------------------------
def _version_tuple(v: str) -> tuple:
    """"1.2" -> (1, 2) для корректного сравнения."""
    import re
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts[:3]) if parts else (0,)


def _launcher_dir() -> str:
    return _app_dir()


def _read_update_manifest_url() -> str:
    url_file = os.path.join(_data_dir(), "update_url.txt")
    if os.path.isfile(url_file):
        with open(url_file, "r", encoding="utf-8") as f:
            url = f.read().strip()
        if url:
            return url
    return UPDATE_MANIFEST_URL


def read_zip_launcher_version(zip_path: str):
    """Достаёт номер версии из launcher.py внутри zip-архива."""
    import re
    import zipfile
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            member = "launcher.py" if "launcher.py" in names else None
            if member is None:
                for n in names:
                    if n.endswith("/launcher.py"):
                        member = n
                        break
            if member is None:
                return None
            src = z.read(member).decode("utf-8", errors="replace")
            m = re.search(r'APP_VERSION\s*=\s*"([\d.]+)"', src)
            return m.group(1) if m else None
    except Exception:
        return None


def check_update_source():
    """Ищет доступное обновление. Возвращает dict или None, если всё актуально.
    dict: {"version", "zip_path"} для локального zip либо
          {"version", "files"} для скачивания по манифесту либо
          {"version", "zip_url"} для скачивания zip по ссылке."""
    # 1) манифест по URL (обновление по воздуху)
    manifest_url = _read_update_manifest_url()
    if manifest_url:
        try:
            r = requests.get(manifest_url, headers=MODRINTH_HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()
            remote_ver = str(data.get("version", "")).strip()
            if remote_ver and _version_tuple(remote_ver) > _version_tuple(APP_VERSION):
                # вариант 1: манифест перечисляет отдельные файлы (для GitHub)
                files = data.get("files")
                if isinstance(files, dict) and files:
                    return {"version": remote_ver,
                            "files": {str(k): str(v) for k, v in files.items()}}
                # вариант 2: манифест даёт ссылку на zip-архив обновления
                files_url = data.get("files_url") or data.get("url") or ""
                if files_url:
                    return {"version": remote_ver, "zip_url": files_url}
        except Exception:
            pass  # манифест недоступен — считаем, что обновлений нет

    # 2) локальный zip в папке launcher_data
    local = os.path.join(_data_dir(), LOCAL_UPDATE_ZIP)
    if os.path.isfile(local):
        ver = read_zip_launcher_version(local)
        if ver and _version_tuple(ver) > _version_tuple(APP_VERSION):
            return {"version": ver, "zip_path": local}

    return None


def download_update_zip(zip_url: str, log_fn=print) -> str:
    """Скачивает zip обновления в папку launcher_data и возвращает путь."""
    dest = os.path.join(_data_dir(), LOCAL_UPDATE_ZIP)
    log_fn(f"[ОБНОВЛЕНИЕ] Скачиваю {zip_url}")
    with requests.get(zip_url, headers=MODRINTH_HEADERS, stream=True, timeout=180) as r:
        r.raise_for_status()
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
    os.replace(tmp, dest)
    return dest


def apply_update_files(files: dict, log_fn=print):
    """Обновление отдельными файлами по URL из манифеста (вариант для GitHub).
    Качает только файлы из списка UPDATABLE_FILES, .minecraft не трогает."""
    me = os.path.join(_app_dir(), "launcher.py")
    if "launcher.py" in files and not getattr(sys, "frozen", False) and os.path.isfile(me):
        shutil.copy2(me, os.path.join(_data_dir(), "launcher.py.bak"))
        log_fn("[ОБНОВЛЕНИЕ] Бэкап старого launcher.py сохранён")

    updated = 0
    for fname, url in files.items():
        if fname not in UPDATABLE_FILES:
            log_fn(f"[ОБНОВЛЕНИЕ] Пропущен посторонний файл {fname}")
            continue
        dest = os.path.join(_app_dir(), fname)
        tmp = dest + ".part"
        with requests.get(url, headers=MODRINTH_HEADERS, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
        os.replace(tmp, dest)
        updated += 1
        log_fn(f"[ОБНОВЛЕНИЕ] Обновлён файл {fname}")
    if updated == 0:
        raise RuntimeError("Манифест не содержит файлов обновления")


def apply_update_zip(zip_path: str, log_fn=print):
    """Устанавливает файлы программы из zip. Папку .minecraft не трогает.
    Старый launcher.py сохраняется как launcher.py.bak."""
    import zipfile

    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        prefix = ""
        if "launcher.py" not in names:
            for n in names:
                if n.endswith("/launcher.py"):
                    prefix = n[: -len("launcher.py")]
                    break

        me = os.path.join(_app_dir(), "launcher.py")
        if not getattr(sys, "frozen", False) and os.path.isfile(me):
            shutil.copy2(me, os.path.join(_data_dir(), "launcher.py.bak"))
            log_fn("[ОБНОВЛЕНИЕ] Бэкап старого launcher.py сохранён")

        updated = 0
        for fname in UPDATABLE_FILES:
            member = prefix + fname
            if member in names:
                data = z.read(member)
                with open(os.path.join(_app_dir(), fname), "wb") as fh:
                    fh.write(data)
                updated += 1
                log_fn(f"[ОБНОВЛЕНИЕ] Обновлён файл {fname}")
        if updated == 0:
            raise RuntimeError("В архиве не найдено файлов обновления")

    # помечаем архив как применённый, чтобы не обновляться по кругу
    applied = zip_path + ".applied"
    try:
        os.replace(zip_path, applied)
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Основное приложение
# ----------------------------------------------------------------------------
class LauncherApp(ctk.CTk):

    VERSION_TYPES = ["Релизы", "Снапшоты", "Fabric", "Forge"]

    # цвет сообщений в консоли по уровню
    LOG_LEVEL_COLORS = {
        "error": "#ff7a7a",
        "warn": "#ffcc66",
        "mod": "#c9a2ff",
        "run": "#8ce68c",
        "game": "#9aa5b5",
        "update": "#7cc4ff",
        "plain": "#b9c8b9",
    }

    def __init__(self):
        super().__init__()

        # перенос служебных файлов в launcher_data — до первого обращения к логам
        self._migrated = migrate_old_files()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("860x680")
        self.minsize(760, 600)

        # состояние
        self.mc_dir = get_minecraft_dir()
        os.makedirs(self.mc_dir, exist_ok=True)

        self.version_type = "Релизы"
        self.versions = []          # список id версий для текущей вкладки
        self.installing = False     # идёт установка
        self.game_proc = None       # процесс игры
        self._game_stopped_by_user = False  # игру остановили кнопкой СТОП
        self._log_queue = queue.Queue()  # потокобезопасная очередь лога
        self._log_entries = []      # (уровень, строка) — для фильтра и сохранения
        self._log_filter = "Все"
        self._loading_versions = False
        self._log_file = self._prepare_log_file()

        # --- цвета в стиле тёмного Minecraft ---
        self.bg_main = "#15151f"      # почти чёрный с фиолетовым
        self.bg_panel = "#1e1e2c"     # панель
        self.bg_entry = "#252536"
        self.accent_green = "#3fa63f" # зелёный травы Minecraft
        self.accent_green_hover = "#4fc14f"
        self.accent_purple = "#7b5cd6"
        self.text_main = "#e8e8f0"
        self.text_dim = "#9a9ab0"

        # Шрифты с поддержкой кириллицы на каждой ОС
        if sys.platform == "win32":
            self._font_family = "Segoe UI"
            self._font_mono = "Consolas"
        elif sys.platform == "darwin":
            self._font_family = "Helvetica Neue"
            self._font_mono = "Menlo"
        else:
            self._font_family = "DejaVu Sans"
            self._font_mono = "DejaVu Sans Mono"

        self.configure(fg_color=self.bg_main)

        self._build_ui()
        self._poll_log()          # запуск выгрузки очереди лога
        self._load_versions_async()
        self.after(2500, self._auto_check_update)   # проверка обновлений при старте
        self._restart_after_exit = False

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        # Верхняя панель (шапка)
        header = ctk.CTkFrame(self, fg_color=self.bg_panel, corner_radius=0, height=64)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        # Лого: квадратный «блок» с зелёным текстом, стилизация под Minecraft
        logo = ctk.CTkLabel(
            header, text="MY LAUNCHER",
            font=self._font(20, "bold"),
            text_color=self.accent_green,
        )
        logo.pack(side="left", padx=20)

        dir_label = ctk.CTkLabel(
            header,
            text=f"Папка игры: {self.mc_dir}",
            font=self._font(11),
            text_color=self.text_dim,
        )
        dir_label.pack(side="right", padx=20)

        # Основная область: слева настройки, справа лог
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=12)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color=self.bg_panel, corner_radius=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        right = ctk.CTkFrame(body, fg_color=self.bg_panel, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        # ---------------------------------------------------------- левая часть
        title = ctk.CTkLabel(
            left, text="НАСТРОЙКИ ЗАПУСКА",
            font=self._font(14, "bold"),
            text_color=self.text_main,
        )
        title.pack(anchor="w", padx=20, pady=(16, 4))

        # Тип версии
        lbl_type = ctk.CTkLabel(
            left, text="Тип версии",
            font=self._font(12), text_color=self.text_dim,
        )
        lbl_type.pack(anchor="w", padx=20, pady=(8, 2))

        self.type_segment = ctk.CTkSegmentedButton(
            left,
            values=self.VERSION_TYPES,
            command=self._on_type_changed,
            selected_color=self.accent_purple,
            selected_hover_color="#8b70e0",
            fg_color=self.bg_entry,
            font=self._font(12),
        )
        self.type_segment.set("Релизы")
        self.type_segment.pack(fill="x", padx=20)

        # Список версий (+ кнопка обновления списка — новые версии Minecraft подхватываются сами)
        ver_row = ctk.CTkFrame(left, fg_color="transparent")
        ver_row.pack(fill="x", padx=20, pady=(12, 2))
        lbl_ver = ctk.CTkLabel(
            ver_row, text="Версия",
            font=self._font(12), text_color=self.text_dim,
        )
        lbl_ver.pack(side="left")
        self.refresh_versions_btn = ctk.CTkButton(
            ver_row, text="ОБНОВИТЬ СПИСОК", width=130, height=22,
            fg_color=self.bg_entry, hover_color="#33334a",
            border_width=1, border_color=self.accent_purple,
            font=self._font(11),
            command=self._load_versions_async,
        )
        self.refresh_versions_btn.pack(side="right")

        self.version_box = ctk.CTkComboBox(
            left,
            values=["Загрузка..."],
            fg_color=self.bg_entry,
            button_color=self.accent_purple,
            button_hover_color="#8b70e0",
            dropdown_fg_color=self.bg_panel,
            dropdown_hover_color=self.accent_purple,
            font=self._font(13),
        )
        self.version_box.pack(fill="x", padx=20)

        # Ник
        lbl_nick = ctk.CTkLabel(
            left, text="Ник в игре (оффлайн-режим)",
            font=self._font(12), text_color=self.text_dim,
        )
        lbl_nick.pack(anchor="w", padx=20, pady=(12, 2))

        self.nick_entry = ctk.CTkEntry(
            left,
            placeholder_text="Steve",
            fg_color=self.bg_entry,
            font=self._font(13),
        )
        self.nick_entry.pack(fill="x", padx=20)

        # Путь к Java
        lbl_java = ctk.CTkLabel(
            left, text="Путь к Java (пусто = системная)",
            font=self._font(12), text_color=self.text_dim,
        )
        lbl_java.pack(anchor="w", padx=20, pady=(12, 2))

        java_row = ctk.CTkFrame(left, fg_color="transparent")
        java_row.pack(fill="x", padx=20)
        java_row.grid_columnconfigure(0, weight=1)

        self.java_entry = ctk.CTkEntry(
            java_row,
            placeholder_text="java",
            fg_color=self.bg_entry,
            font=self._font(13),
        )
        self.java_entry.grid(row=0, column=0, sticky="ew")

        self.java_btn = ctk.CTkButton(
            java_row, text="Найти", width=80,
            fg_color=self.bg_entry, hover_color=self.bg_entry,
            border_width=1, border_color=self.accent_purple,
            font=self._font(12),
            command=self._find_java,
        )
        self.java_btn.grid(row=0, column=1, padx=(8, 0))

        # Статус установленной версии
        self.installed_label = ctk.CTkLabel(
            left, text="", font=self._font(11), text_color=self.text_dim,
        )
        self.installed_label.pack(anchor="w", padx=20, pady=(10, 0))

        # Кнопка ИГРАТЬ — крупная, зелёная
        self.play_btn = ctk.CTkButton(
            left,
            text="ИГРАТЬ",
            font=self._font(22, "bold"),
            fg_color=self.accent_green,
            hover_color=self.accent_green_hover,
            text_color="#ffffff",
            height=58,
            corner_radius=10,
            command=self._on_play,
        )
        self.play_btn.pack(fill="x", padx=20, pady=(20, 8))

        self.stop_btn = ctk.CTkButton(
            left,
            text="ЗАВЕРШИТЬ ИГРУ",
            font=self._font(13, "bold"),
            fg_color="#a33a3a",
            hover_color="#c24747",
            height=36,
            corner_radius=8,
            command=self._on_stop_game,
        )
        self.stop_btn.pack(fill="x", padx=20, pady=(0, 8))

        # Кнопка открытия каталога модов
        self.mods_btn = ctk.CTkButton(
            left,
            text="КАТАЛОГ МОДОВ (MODRINTH)",
            font=self._font(13, "bold"),
            fg_color=self.accent_purple,
            hover_color="#8b70e0",
            height=40,
            corner_radius=8,
            command=self._on_mods,
        )
        self.mods_btn.pack(fill="x", padx=20, pady=(0, 8))

        # Кнопка проверки обновлений
        self.update_btn = ctk.CTkButton(
            left,
            text="ПРОВЕРИТЬ ОБНОВЛЕНИЯ",
            font=self._font(12, "bold"),
            fg_color=self.bg_entry,
            hover_color="#33334a",
            border_width=1,
            border_color=self.accent_purple,
            height=32,
            corner_radius=8,
            command=self._on_check_updates,
        )
        self.update_btn.pack(fill="x", padx=20, pady=(0, 16))

        # ---------------------------------------------------------- правая часть
        log_top = ctk.CTkFrame(right, fg_color="transparent")
        log_top.pack(fill="x", padx=20, pady=(16, 4))

        log_title = ctk.CTkLabel(
            log_top, text="КОНСОЛЬ",
            font=self._font(14, "bold"),
            text_color=self.text_main,
        )
        log_title.pack(side="left")

        self.log_filter_box = ctk.CTkComboBox(
            log_top,
            values=["Все", "Только ошибки", "Ошибки и предупреждения", "Моды и обновления"],
            width=190, height=24,
            fg_color=self.bg_entry,
            button_color=self.accent_purple,
            button_hover_color="#8b70e0",
            dropdown_fg_color=self.bg_panel,
            dropdown_hover_color=self.accent_purple,
            font=self._font(11),
            command=self._on_log_filter,
        )
        self.log_filter_box.set("Все")
        self.log_filter_box.pack(side="right")

        self.log_box = ctk.CTkTextbox(
            right,
            fg_color="#0f0f17",
            text_color="#b9c8b9",
            font=ctk.CTkFont(family=self._font_mono, size=12),
        )
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(0, 6))
        self.log_box.configure(state="disabled")
        # цветные уровни сообщений внутри лога
        tk_text = getattr(self.log_box, "_textbox", None)
        if tk_text is not None:
            for lvl, color in self.LOG_LEVEL_COLORS.items():
                tk_text.tag_config(lvl, foreground=color)

        log_btns = ctk.CTkFrame(right, fg_color="transparent")
        log_btns.pack(fill="x", padx=20, pady=(0, 6))
        ctk.CTkButton(
            log_btns, text="СОХРАНИТЬ ЛОГ", width=140, height=26,
            fg_color=self.bg_entry, hover_color="#33334a",
            border_width=1, border_color=self.accent_purple,
            font=self._font(11), command=self._save_log,
        ).pack(side="left")
        ctk.CTkButton(
            log_btns, text="ОЧИСТИТЬ", width=110, height=26,
            fg_color=self.bg_entry, hover_color="#33334a",
            font=self._font(11), command=self._clear_log,
        ).pack(side="right")

        # Прогресс установки
        self.status_label = ctk.CTkLabel(
            right, text="Готов к запуску",
            font=self._font(12), text_color=self.text_dim, anchor="w",
        )
        self.status_label.pack(fill="x", padx=20)

        self.progress = ctk.CTkProgressBar(
            right, fg_color=self.bg_entry,
            progress_color=self.accent_green, height=10,
        )
        self.progress.set(0)
        self.progress.pack(fill="x", padx=20, pady=(4, 16))

        self._log(f"{APP_NAME} {APP_VERSION} запущен")
        if self._migrated:
            self._log(f"[ОБНОВЛЕНИЕ] Служебные файлы перенесены в папку {DATA_DIR_NAME}: {self._migrated} шт.")
        self._log(f"Папка игры: {self.mc_dir}")

    # ------------------------------------------------------------ утилиты
    def _font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        return ctk.CTkFont(family=self._font_family, size=size, weight=weight)

    @staticmethod
    def _detect_log_level(text: str) -> str:
        """Определяет цвет строки лога по метке или содержимому."""
        t = text.lstrip()
        if t.startswith("[ОШИБКА]") or "/ERROR]" in t or "Exception in thread" in t:
            return "error"
        if t.startswith("[ВНИМАНИЕ]") or "/WARN]" in t or t.startswith("WARNING"):
            return "warn"
        if t.startswith("[МОД]"):
            return "mod"
        if t.startswith("[ЗАПУСК]") or t.startswith("[УСТАНОВКА]"):
            return "run"
        if t.startswith("[ИГРА]"):
            return "game"
        if t.startswith(("[ОБНОВЛЕНИЕ]", "[JAVA]", "[ВЕРСИИ]")):
            return "update"
        return "plain"

    def _prepare_log_file(self) -> str:
        """Файл лога в папке launcher_data; крупный файл ротируется в .old."""
        path = os.path.join(_data_dir(), "launcher.log")
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 5 * 1024 * 1024:
                os.replace(path, path + ".old")
        except OSError:
            pass
        return path

    def _log(self, text: str):
        """Потокобезопасно: строка уходит в очередь, файл и историю."""
        ts = time.strftime("%H:%M:%S")
        level = self._detect_log_level(text)
        line = f"[{ts}] {text}"
        self._log_entries.append((level, line))
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self._log_queue.put((level, line))

    def _level_matches_filter(self, level: str) -> bool:
        if self._log_filter == "Только ошибки":
            return level == "error"
        if self._log_filter == "Ошибки и предупреждения":
            return level in ("error", "warn")
        if self._log_filter == "Моды и обновления":
            return level in ("mod", "update")
        return True

    def _insert_log_line(self, level: str, line: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n", level)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _poll_log(self):
        """Выгружает очередь лога в консоль (выполняется в главном потоке)."""
        try:
            while True:
                level, line = self._log_queue.get_nowait()
                if self._level_matches_filter(level):
                    self._insert_log_line(level, line)
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    # ------------------------------------------------------------ тихий лог игры
    def _pump_game_output(self, stream):
        """Читает вывод игры в «тихом режиме» (1.4.2):
        - не дублирует время: у строк Minecraft уже есть своё [ЧЧ:ММ:СС];
        - схлопывает повторы: одинаковые подряд строки пишутся один раз с (×N);
        - скрывает безвредный offline-шум (401, Realms, PDH) — в конце
          игры пишется только их количество.
        Выполняется в рабочем потоке запуска."""
        prev_key = None
        prev_text = None
        repeats = 0
        noise = 0
        in_trace = False  # внутри стек-трейса скрытого исключения

        def flush():
            nonlocal prev_key, prev_text, repeats
            if prev_text is None:
                return
            if repeats:
                self._log(f"{prev_text} (×{repeats + 1})")
            else:
                self._log(prev_text)
            prev_key, prev_text, repeats = None, None, 0

        for raw in stream:
            line = raw.rstrip()
            if not line.strip():
                continue
            m = re.match(r"^\[\d{2}:\d{2}:\d{2}\]\s*(.*)$", line)
            text = m.group(1) if m else line
            if GAME_NOISE_RE.search(text):
                noise += 1
                in_trace = True   # давим и стек-трейс под ним
                continue
            if in_trace and re.match(r"^\s*(at\s|Caused by:|\.\.\.\s*\d+\s*more)", text):
                noise += 1
                continue
            in_trace = False
            key = re.sub(r"\d+", "#", text)
            if key == prev_key:
                repeats += 1
                continue
            flush()
            prev_key, prev_text = key, text
        flush()
        if noise:
            self._log(f"[ИГРА] Пропущено служебных сообщений offline-режима: {noise}")

    def _on_log_filter(self, value: str):
        """Перестроить консоль, оставив только выбранный тип сообщений."""
        self._log_filter = value
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        for level, line in self._log_entries:
            if self._level_matches_filter(level):
                self.log_box.insert("end", line + "\n", level)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _save_log(self):
        path = os.path.join(_data_dir(), "launcher_log.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                for _level, line in self._log_entries:
                    f.write(line + "\n")
            self._log(f"Лог сохранён: {path}")
        except OSError as e:
            self._log(f"[ОШИБКА] Не удалось сохранить лог: {e}")

    def _clear_log(self):
        self._log_entries = []
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _set_status(self, text: str):
        self.after(0, lambda: self.status_label.configure(text=text))

    def _set_progress(self, value: float):
        self.after(0, lambda: self.progress.set(value))

    # ------------------------------------------------------------ версии
    def _on_type_changed(self, value: str):
        self.version_type = value
        self._load_versions_async()

    def _load_versions_async(self):
        threading.Thread(target=self._load_versions, daemon=True).start()

    def _load_versions(self):
        if self._loading_versions:
            return
        self._loading_versions = True
        self.after(0, lambda: self.refresh_versions_btn.configure(state="disabled"))
        self.after(0, lambda: self.version_box.configure(values=["Загрузка..."]))
        try:
            versions = self._fetch_versions(self.version_type)
        except Exception as e:
            self._log(f"[ОШИБКА] Не удалось получить список версий: {e}")
            versions = []

        self.versions = versions
        if versions:
            self._log(f"[ВЕРСИИ] Загружено {len(versions)} версий ({self.version_type})")
            if self.version_type == "Релизы":
                self._check_new_release(versions[0])
            self.after(0, lambda: self.version_box.configure(values=versions))
            self.after(0, lambda: self.version_box.set(versions[0]))
        else:
            self.after(0, lambda: self.version_box.configure(values=["Нет данных"]))
            self.after(0, lambda: self.version_box.set("Нет данных"))

        self._update_installed_label()
        self._loading_versions = False
        self.after(0, lambda: self.refresh_versions_btn.configure(state="normal"))

    def _check_new_release(self, latest: str):
        """Сообщает, если с прошлого запуска вышла новая версия Minecraft.
        Список версий берётся напрямую с серверов Mojang, поэтому свежие
        версии появляются в лаунчере автоматически."""
        marker = os.path.join(_data_dir(), "last_mc_version.txt")
        try:
            prev = ""
            if os.path.isfile(marker):
                with open(marker, "r", encoding="utf-8") as f:
                    prev = f.read().strip()
            if prev and prev != latest:
                self._log(
                    f"[ВЕРСИИ] Вышла новая версия Minecraft {latest} "
                    f"(прошлый запуск был на {prev}) — она уже в списке"
                )
            with open(marker, "w", encoding="utf-8") as f:
                f.write(latest)
        except OSError:
            pass

    def _fetch_versions(self, vtype: str):
        if vtype == "Релизы":
            return [v["id"] for v in mll.utils.get_version_list() if v.get("type") == "release"]
        if vtype == "Снапшоты":
            return [v["id"] for v in mll.utils.get_version_list() if v.get("type") == "snapshot"]
        if vtype == "Fabric":
            # Fabric: стабильные версии Minecraft в порядке новизны (от манифеста Mojang)
            return sort_versions_like_manifest(mll.fabric.get_stable_minecraft_versions())
        if vtype == "Forge":
            return sort_forge_versions(mll.forge.list_forge_versions())
        return []

    def _update_installed_label(self):
        try:
            installed = [v["id"] for v in mll.utils.get_installed_versions(self.mc_dir)]
        except Exception:
            installed = []
        text = f"Установлено версий: {len(installed)}"
        self.after(0, lambda: self.installed_label.configure(text=text))

    # ------------------------------------------------------------ Java
    def _find_java(self):
        found = shutil.which("java")
        if found:
            self.java_entry.delete(0, "end")
            self.java_entry.insert(0, found)
            major = java_major_version(found)
            self._log(f"[JAVA] Найдено: {found} (Java {major})" if major else f"[JAVA] Найдено: {found}")
        else:
            self._log("[JAVA] Java не найдена в PATH — не проблема: при запуске игры лаунчер "
                      "сам скачает официальный рантайм Mojang, либо установите Java с adoptium.net.")

    def _get_java_path(self) -> str:
        return self.java_entry.get().strip() or None

    def _ensure_java(self, version: str) -> str:
        """Возвращает путь к подходящей Java для выбранной версии Minecraft.
        Если системной Java нет или она слишком старая — скачивает
        официальный рантайм Mojang в .minecraft/runtime."""
        custom = self.java_entry.get().strip()
        if custom:
            if os.path.isfile(custom) or shutil.which(custom):
                self._log(f"[JAVA] Использую указанную Java: {custom}")
                return custom
            self._log(f"[ВНИМАНИЕ] Указанный путь к Java не найден: {custom} — ищу автоматически")

        # какая Java нужна выбранной версии Minecraft (порядок манифеста)
        probe = version.split("-")[0]          # Forge: «1.20.1-47.2.0» -> «1.20.1»
        runtime_name = None
        req_major = None
        try:
            info = mll.runtime.get_version_runtime_information(probe, self.mc_dir)
            if info:
                runtime_name = info.get("name")
                req_major = int(info.get("javaMajorVersion", 0)) or None
        except Exception:
            pass
        if req_major is None:
            req_major = 8                     # самые старые версии

        sys_java = shutil.which("java")
        if sys_java:
            major = java_major_version(sys_java)
            if major is None:
                self._log(f"[JAVA] Использую системную Java (версию определить не удалось): {sys_java}")
                return sys_java
            if major >= req_major:
                self._log(f"[JAVA] Системная Java {major} подходит (нужна {req_major}+): {sys_java}")
                return sys_java
            self._log(f"[ВНИМАНИЕ] Системная Java {major} слишком старая для {version} (нужна {req_major})")

        # официальная Java от Mojang — скачиваем один раз в .minecraft
        runtime_name = runtime_name or "java-runtime-delta"
        try:
            installed = mll.runtime.get_installed_jvm_runtimes(self.mc_dir)
            if runtime_name not in installed:
                self._log(f"[JAVA] Скачиваю официальный рантайм Mojang: {runtime_name} (Java {req_major})...")
                self._log("[JAVA] Это один раз — файлы сохранятся в .minecraft/runtime")
                cb = InstallProgress(self._set_status, self._set_progress).build()
                mll.runtime.install_jvm_runtime(runtime_name, self.mc_dir, callback=cb)
            jvm = mll.runtime.get_executable_path(runtime_name, self.mc_dir)
            if jvm:
                self._log(f"[JAVA] Рантайм Mojang готов: {jvm}")
                return jvm
        except Exception as e:
            self._log(f"[JAVA] Не удалось скачать рантайм Mojang: {e}")

        raise RuntimeError(
            f"Java не найдена (для {version} нужна Java {req_major}+). "
            "Установите Java с adoptium.net и нажмите «Найти», "
            "или укажите путь к java.exe в поле «Путь к Java»."
        )

    # ------------------------------------------------------------ установка и запуск
    def _on_play(self):
        if self.installing:
            return
        version = self.version_box.get()
        if version in ("Загрузка...", "Нет данных", ""):
            self._log("[ОШИБКА] Сначала выберите версию")
            return

        nick = self.nick_entry.get().strip() or "Steve"
        threading.Thread(
            target=self._install_and_launch, args=(version, nick), daemon=True
        ).start()

    def _install_and_launch(self, version: str, nick: str):
        self.installing = True
        self.after(0, lambda: self.play_btn.configure(state="disabled", text="ЗАГРУЗКА..."))

        try:
            callback = InstallProgress(self._set_status, self._set_progress).build()

            # --- Java: системная или официальный рантайм Mojang ---
            java = self._ensure_java(version)

            # --- установка нужного типа версии ---
            if self.version_type == "Fabric":
                if not mll.fabric.is_minecraft_version_supported(version):
                    raise RuntimeError(f"Fabric не поддерживает версию {version}")
                self._log(f"[УСТАНОВКА] Fabric {version}")
                mll.fabric.install_fabric(version, self.mc_dir, callback=callback, java=java)
            elif self.version_type == "Forge":
                if not mll.forge.is_forge_version_valid(version):
                    raise RuntimeError(f"Версия Forge {version} не найдена")
                if not mll.forge.supports_automatic_install(version):
                    raise RuntimeError(
                        f"Forge {version} слишком старый: требуется ручная установка установщиком Forge"
                    )
                self._log(f"[УСТАНОВКА] Forge {version}")
                mll.forge.install_forge_version(version, self.mc_dir, callback=callback, java=java)
            else:
                # install_minecraft_version также проверяет и чинит существующую установку
                self._log(f"[УСТАНОВКА] Minecraft {version}")
                mll.install.install_minecraft_version(version, self.mc_dir, callback=callback)
                ensure_legacy_icons(self.mc_dir)

            # --- предупреждение про древние версии ---
            year = get_version_release_year(version)
            if year is not None and year < 2013:
                self._log(f"[ВНИМАНИЕ] {version} выпущена в {year} году. Древние версии:")
                self._log("[ВНИМАНИЕ] - звук может не работать (сервер ресурсов Mojang отключён)")
                self._log("[ВНИМАНИЕ] - требуется Java 8, на новых Java они нестабильны")
                self._log("[ВНИМАНИЕ] - могут завершаться сразу после старта")
                self._log("[ВНИМАНИЕ] Рекомендуется современная версия (1.20.1 или новее)")
            else:
                old_prefixes = ("b1.", "a1.", "1.0", "1.1", "1.2.", "1.3.", "1.4.", "1.5.")
                if version.startswith(old_prefixes):
                    self._log("[ВНИМАНИЕ] Похоже, это очень старая версия — см. примечания в README")

            self._set_progress(1.0)
            self._set_status("Установка завершена, запускаю игру...")

            # --- запуск ---
            options = {
                "username": nick,
                "uuid": get_offline_uuid(nick),
                "token": "",
                "launcherName": APP_NAME,
                "launcherVersion": APP_VERSION,
                "executablePath": java,
            }

            command = mll.command.get_minecraft_command(version, self.mc_dir, options)
            self._log(f"[ЗАПУСК] Minecraft {version}, игрок: {nick}")
            self._log(f"[ЗАПУСК] Java: {os.path.basename(command[0])} ({len(command)} параметров)")

            self._game_stopped_by_user = False
            self.game_proc = subprocess.Popen(
                command,
                cwd=self.mc_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self._set_status(f"Игра запущена (процесс {self.game_proc.pid})")
            self.after(0, lambda: self.stop_btn.configure(state="normal"))

            # читаем лог игры в консоль лаунчера (тихий режим 1.4.2)
            assert self.game_proc.stdout is not None
            self._pump_game_output(self.game_proc.stdout)

            code = self.game_proc.wait()
            if self._game_stopped_by_user:
                self._log("[ИГРА] Игра остановлена кнопкой СТОП")
            else:
                self._log(f"[ИГРА] Процесс завершён с кодом {code}")
            if code == 0 and year is not None and year < 2013:
                self._log("[ИГРА] Древние версии часто завершаются сразу после старта.")
                self._log("[ИГРА] Попробуйте выбрать версию 1.20.1 или новее во вкладке «Релизы».")

        except Exception as e:
            self._log(f"[ОШИБКА] {e}")
            if "WinError 2" in str(e) or "The system cannot find the file" in str(e):
                self._log("[ВНИМАНИЕ] Файл не найден — обычно это java.exe. "
                          "Установите Java 21+ (adoptium.net) или укажите путь в поле «Путь к Java».")
            self._set_status(f"Ошибка: {e}")
            self._set_progress(0)
        finally:
            self.installing = False
            self.after(0, lambda: self.play_btn.configure(state="normal", text="ИГРАТЬ"))
            self._update_installed_label()

    def _on_stop_game(self):
        if self.game_proc and self.game_proc.poll() is None:
            self._log("[ИГРА] Останавливаю игру...")
            self._game_stopped_by_user = True
            self.game_proc.terminate()
        else:
            self._log("[ИГРА] Игра не запущена")

    # ------------------------------------------------------------ моды
    def _on_mods(self):
        version = self.version_box.get()
        ctx = get_mod_context(self.version_type, version)
        ModCatalog(self, ctx, self.mc_dir, self._log)

    # ------------------------------------------------------------ обновления
    def _on_check_updates(self):
        self.update_btn.configure(state="disabled")
        threading.Thread(target=self._check_updates_worker, daemon=True).start()

    def _auto_check_update(self):
        threading.Thread(target=self._check_updates_worker, args=(False,), daemon=True).start()

    def _check_updates_worker(self, manual=True):
        try:
            upd = check_update_source()
        except Exception as e:
            upd = None
            self._log(f"[ОБНОВЛЕНИЕ] Ошибка проверки: {e}")

        if upd is None:
            if manual:
                self._log("[ОБНОВЛЕНИЕ] У вас последняя версия")
            self.after(0, lambda: self.update_btn.configure(state="normal"))
            return

        self._log(f"[ОБНОВЛЕНИЕ] Доступна версия {upd['version']} (у вас {APP_VERSION})")
        self._set_status(f"Доступно обновление до {upd['version']}")
        self.after(0, lambda: self.update_btn.configure(state="normal"))
        self.after(0, lambda: UpdateDialog(self, upd))

    def _install_update(self, upd: dict):
        """Скачивает (если надо) и устанавливает обновление в фоне."""
        def worker():
            try:
                if "files" in upd:
                    # обновление отдельными файлами из манифеста (GitHub)
                    apply_update_files(upd["files"], self._log)
                else:
                    zip_path = upd.get("zip_path")
                    if zip_path is None:
                        zip_path = download_update_zip(upd["zip_url"], self._log)
                    self._log("[ОБНОВЛЕНИЕ] Устанавливаю...")
                    apply_update_zip(zip_path, self._log)
                self._log(f"[ОБНОВЛЕНИЕ] Готово! Нажмите «ПЕРЕЗАПУСТИТЬ», чтобы применить версию {upd['version']}")
                self._set_status(f"Обновление до {upd['version']} установлено — перезапустите лаунчер")
                self._restart_after_exit = True
                self.after(0, self._show_restart_button)
            except Exception as e:
                self._log(f"[ОБНОВЛЕНИЕ] Ошибка: {e}")
                self._set_status(f"Ошибка обновления: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def _show_restart_button(self):
        self.update_btn.configure(
            text="ПЕРЕЗАПУСТИТЬ ЛАУНЧЕР",
            fg_color=self.accent_green,
            hover_color=self.accent_green_hover,
            border_width=0,
            state="normal",
            command=self._do_restart,
        )

    def _do_restart(self):
        self._restart_after_exit = True
        self.destroy()


class UpdateDialog(ctk.CTkToplevel):

    def __init__(self, master, upd: dict):
        super().__init__(master)
        self.master_app = master
        self.upd = upd

        self.title(f"{APP_NAME} — обновление")
        self.geometry("420x210")
        self.resizable(False, False)
        self.configure(fg_color=master.bg_main)
        self.transient(master)
        self.lift()
        self.focus_force()

        fam = master._font_family
        self._font = lambda size, weight="normal": ctk.CTkFont(family=fam, size=size, weight=weight)

        msg = (f"Доступна новая версия {upd['version']}\n"
               f"У вас сейчас: {APP_VERSION}\n\n"
               "Будут обновлены только файлы программы.\nСкачанные версии игры и моды останутся на месте.")
        label = ctk.CTkLabel(
            self, text=msg, font=self._font(13),
            text_color=master.text_main, justify="left",
        )
        label.pack(padx=20, pady=(20, 10), anchor="w")

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(6, 16))

        btn = ctk.CTkButton(
            row, text="УСТАНОВИТЬ",
            font=self._font(14, "bold"),
            fg_color=master.accent_green, hover_color=master.accent_green_hover,
            height=44, corner_radius=8,
            command=self._apply,
        )
        btn.pack(side="left", expand=True, fill="x", padx=(0, 6))

        close = ctk.CTkButton(
            row, text="ПОЗЖЕ", width=110,
            font=self._font(13, "bold"),
            fg_color=master.bg_entry, hover_color="#33334a",
            height=44, corner_radius=8,
            command=self.destroy,
        )
        close.pack(side="right")

    def _apply(self):
        self.master_app._install_update(self.upd)
        self.destroy()


# ----------------------------------------------------------------------------
# Каталог модов Modrinth: живой поиск, пагинация, автозагрузка зависимостей
# ----------------------------------------------------------------------------
class ModCatalog(ctk.CTkToplevel):

    def __init__(self, master, ctx, mc_dir, log_fn):
        super().__init__(master)

        self.ctx = ctx              # (версия MC, загрузчик) или None
        self.mc_dir = mc_dir
        # потокобезопасный лог: из рабочего потока — через after в главный
        self.log = lambda msg: self.after(0, lambda: log_fn(msg))
        self.searching = False
        self.downloading = False
        self.query = ""
        self.offset = 0
        self.total_hits = 0
        self.slugs_vars = {}        # slug -> BooleanVar (галочка выбора)
        self.no_version_filter = False

        self.title(f"{APP_NAME} — каталог модов")
        self.geometry("780x680")
        self.minsize(660, 540)
        self.configure(fg_color=master.bg_main)
        self.transient(master)
        self.lift()
        self.focus_force()

        fam = master._font_family
        self._font = lambda size, weight="normal": ctk.CTkFont(family=fam, size=size, weight=weight)

        # цвета из главного окна (для строк результатов)
        self.bg_entry_ref = master.bg_entry
        self.accent_purple_ref = master.accent_purple
        self.text_main_ref = master.text_main
        self.text_dim_ref = master.text_dim

        # Шапка с контекстом
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(14, 6))

        if ctx:
            mc_version, loader = ctx
            loader_name = "Forge" if loader == "forge" else "Fabric"
            info = (f"Каталог Modrinth: Minecraft {mc_version} ({loader_name}). "
                    "Отметьте моды — обязательные зависимости скачаются автоматически.")
        else:
            info = ("Каталог доступен для версий Fabric и Forge. "
                    "Выберите такой тип версии на главном экране и попробуйте снова.")

        self.info_label = ctk.CTkLabel(
            top, text=info, font=self._font(12),
            text_color=master.text_main if ctx else "#d8a33a",
            anchor="w", justify="left", wraplength=720,
        )
        self.info_label.pack(fill="x")

        # Строка поиска
        search_row = ctk.CTkFrame(self, fg_color="transparent")
        search_row.pack(fill="x", padx=16, pady=(4, 4))
        search_row.grid_columnconfigure(0, weight=1)

        self.search_entry = ctk.CTkEntry(
            search_row,
            placeholder_text="Поиск по каталогу Modrinth (пусто = самые популярные)",
            fg_color=master.bg_entry, font=self._font(13),
        )
        self.search_entry.grid(row=0, column=0, sticky="ew")
        self.search_entry.bind("<Return>", lambda _e: self._on_search())

        self.search_btn = ctk.CTkButton(
            search_row, text="НАЙТИ", width=110,
            fg_color=master.accent_purple, hover_color="#8b70e0",
            font=self._font(13, "bold"), command=self._on_search,
        )
        self.search_btn.grid(row=0, column=1, padx=(8, 0))

        # Результаты
        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color=master.bg_panel, corner_radius=12
        )
        self.scroll.pack(fill="both", expand=True, padx=16, pady=8)

        self.more_btn = ctk.CTkButton(
            self, text="", height=32,
            fg_color=master.bg_entry, hover_color="#33334a",
            border_width=1, border_color=master.accent_purple,
            font=self._font(12, "bold"),
            state="disabled", command=self._load_more,
        )
        self.more_btn.pack(fill="x", padx=16, pady=(0, 4))

        # Низ: статус и кнопки
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=16, pady=(2, 14))

        self.status_label = ctk.CTkLabel(
            bottom, text="", font=self._font(11), text_color=master.text_dim,
            anchor="w", justify="left", wraplength=720,
        )
        self.status_label.pack(fill="x", pady=(0, 6))

        row = ctk.CTkFrame(bottom, fg_color="transparent")
        row.pack(fill="x")

        self.download_btn = ctk.CTkButton(
            row, text="СКАЧАТЬ ВЫБРАННЫЕ",
            font=self._font(14, "bold"),
            fg_color=master.accent_green, hover_color=master.accent_green_hover,
            height=44, corner_radius=8,
            state="normal" if ctx else "disabled",
            command=self._download_selected,
        )
        self.download_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))

        close_btn = ctk.CTkButton(
            row, text="ЗАКРЫТЬ", width=120,
            font=self._font(13, "bold"),
            fg_color=master.bg_entry, hover_color="#33334a",
            height=44, corner_radius=8,
            command=self.destroy,
        )
        close_btn.pack(side="right")

        if ctx:
            self.after(300, self._on_search)   # сразу показываем популярные моды

    # -------------------------------------------------------- поиск
    def _on_search(self):
        if self.searching or not self.ctx:
            return
        self.query = self.search_entry.get().strip()
        self.offset = 0
        self._clear_results()
        self.searching = True
        self.search_btn.configure(state="disabled", text="ИЩУ...")
        self.status_label.configure(text="Ищу моды...")
        threading.Thread(target=self._search_worker, daemon=True).start()

    def _load_more(self):
        if self.searching or not self.ctx:
            return
        self.searching = True
        self.more_btn.configure(state="disabled", text="ЗАГРУЖАЮ...")
        threading.Thread(target=self._search_worker, daemon=True).start()

    def _search_worker(self):
        try:
            hits, total = search_mods(
                self.query, self.ctx[0], self.ctx[1], self.offset, MODS_PAGE_SIZE
            )
            fallback = False
            if not hits and self.offset == 0 and not self.no_version_filter:
                # модов именно под эту версию MC нет — покажем весь каталог загрузчика
                hits, total = search_mods(
                    self.query, self.ctx[0], self.ctx[1], 0, MODS_PAGE_SIZE,
                    use_version_filter=False,
                )
                fallback = True
            self.after(0, lambda: self._search_done(hits, total, fallback))
        except Exception:
            self.after(0, self._search_error)

    def _search_done(self, hits, total, fallback):
        self.searching = False
        self.search_btn.configure(state="normal", text="НАЙТИ")
        if fallback:
            self.no_version_filter = True
            self.info_label.configure(
                text=self.info_label.cget("text")
                + "\nМодов именно для этой версии MC не нашлось — показан весь каталог "
                  "загрузчика. Перед установкой проверяйте совместимость.",
                text_color="#d8a33a",
            )
        if self.offset == 0:
            self.total_hits = total
        for hit in hits:
            self._add_result_row(hit)
        self.offset += len(hits)
        if hits and self.offset < self.total_hits:
            self.more_btn.configure(
                state="normal",
                text=f"ПОКАЗАТЬ ЕЩЁ (найдено {self.total_hits}, показано {self.offset})",
            )
        elif hits:
            self.more_btn.configure(state="disabled", text="ВСЁ ПОКАЗАНО")
        else:
            self.more_btn.configure(state="disabled", text="НИЧЕГО НЕ НАЙДЕНО")
        if hits:
            self.status_label.configure(
                text=f"Найдено модов: {self.total_hits}. "
                     "Отметьте нужные и нажмите «СКАЧАТЬ ВЫБРАННЫЕ».")
        else:
            self.status_label.configure(text="Ничего не найдено. Измените запрос или версию.")

    def _search_error(self):
        self.searching = False
        self.search_btn.configure(state="normal", text="НАЙТИ")
        self.more_btn.configure(state="disabled", text="ОШИБКА ПОИСКА")
        self.status_label.configure(
            text="Ошибка поиска. Проверьте интернет и попробуйте снова.")
        self.log("[МОД] Ошибка поиска по каталогу")

    def _clear_results(self):
        for w in self.scroll.winfo_children():
            w.destroy()
        self.slugs_vars = {}
        self.more_btn.configure(state="disabled", text="")

    def _add_result_row(self, hit):
        slug = hit["slug"]
        if not slug or slug in self.slugs_vars:
            return
        row = ctk.CTkFrame(self.scroll, fg_color=self.bg_entry_ref, corner_radius=8)
        row.pack(fill="x", padx=8, pady=4)

        var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            row, text="", variable=var, width=26,
            fg_color=self.accent_purple_ref, hover_color="#8b70e0",
            checkmark_color="#ffffff",
        ).pack(side="left", padx=(10, 6), pady=10)

        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=8)

        dl_text = f"{hit['downloads']:,}".replace(",", " ")
        title_lbl = ctk.CTkLabel(
            info,
            text=f"{hit['title']}  ·  {dl_text} скачиваний",
            font=self._font(13, "bold"),
            text_color=self.text_main_ref, anchor="w",
        )
        title_lbl.pack(fill="x")
        desc_lbl = ctk.CTkLabel(
            info, text=hit["description"] or "Без описания",
            font=self._font(11), text_color=self.text_dim_ref,
            anchor="w", justify="left", wraplength=580,
        )
        desc_lbl.pack(fill="x")

        def _toggle(_event=None, v=var):
            v.set(not v.get())

        for w in (info, title_lbl, desc_lbl):
            w.bind("<Button-1>", _toggle)

        self.slugs_vars[slug] = var

    # -------------------------------------------------------- скачивание
    def _download_selected(self):
        if self.downloading or not self.ctx:
            return
        chosen = [slug for slug, var in self.slugs_vars.items() if var.get()]
        if not chosen:
            self.status_label.configure(text="Ничего не выбрано")
            return
        self.downloading = True
        self.download_btn.configure(state="disabled", text="СКАЧИВАЮ...")
        threading.Thread(target=self._download_worker, args=(chosen,), daemon=True).start()

    def _download_worker(self, chosen):
        mc_version, loader = self.ctx
        mods_dir = os.path.join(self.mc_dir, "mods")
        os.makedirs(mods_dir, exist_ok=True)

        self._status(f"Строю список зависимостей для {len(chosen)} мод(ов)...")
        try:
            plan = collect_mods_with_deps(chosen, mc_version, loader, self.log)
        except Exception as e:
            self.log(f"[МОД] Ошибка построения списка: {e}")
            plan = []

        ok = skip = fail = replaced = 0
        for i, item in enumerate(plan, 1):
            kind = "зависимость" if item["is_dep"] else "мод"
            self._status(f"Скачиваю {i}/{len(plan)}: {item['filename']} ({kind})")
            dest = os.path.join(mods_dir, item["filename"])
            # убираем другие версии этого же мода — они конфликтуют при запуске
            try:
                replaced += remove_old_mod_files(
                    mods_dir, item["slug"], item["title"], item["filename"], self.log
                )
            except Exception as e:
                self.log(f"[МОД] Не удалось проверить старые версии: {e}")
            if os.path.isfile(dest):
                skip += 1
                self.log(f"[МОД] {item['filename']}: уже скачан, пропускаю")
                continue
            try:
                download_mod_file(item["url"], dest)
                ok += 1
                self.log(f"[МОД] {item['filename']}: скачан в mods")
            except Exception as e:
                fail += 1
                self.log(f"[МОД] {item['filename']}: ошибка — {e}")

        def finish():
            extra = f", старых версий модов удалено {replaced}" if replaced else ""
            self.status_label.configure(
                text=f"Готово: скачано {ok}, уже были {skip}, ошибок {fail}{extra}. "
                     "Перезапустите Minecraft, чтобы моды появились.")
            self.download_btn.configure(state="normal", text="СКАЧАТЬ ВЫБРАННЫЕ")
            self.downloading = False

        self.after(0, finish)

    def _status(self, text: str):
        self.after(0, lambda: self.status_label.configure(text=text))


def main():
    app = LauncherApp()
    app.mainloop()
    # перезапуск после установки обновления
    if getattr(app, "_restart_after_exit", False):
        me = os.path.abspath(__file__)
        if getattr(sys, "frozen", False):
            subprocess.Popen([sys.executable])
        else:
            subprocess.Popen([sys.executable, me])


if __name__ == "__main__":
    main()
