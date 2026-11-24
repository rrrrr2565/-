# -*- coding: utf-8 -*-
"""
MyLauncher — простой десктопный лаунчер Minecraft
Стиль: тёмный Minecraft (тёмный фон, зелёная кнопка ИГРАТЬ)
Функции:
  - список версий: релизы, снапшоты, Fabric, Forge
  - скачивание и установка выбранной версии
  - запуск игры (оффлайн-вход по нику)
  - 10 популярных модов с загрузкой с Modrinth
"""

import os
import sys
import json
import queue
import shutil
import threading
import subprocess

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
APP_VERSION = "1.3"

MODRINTH_API = "https://api.modrinth.com/v2"
MODRINTH_HEADERS = {"User-Agent": f"{APP_NAME}/{APP_VERSION} (Minecraft launcher)"}

# --- Обновление по воздуху ---
# Файлы программы, которые обновляет самообновление (папка .minecraft не трогается)
UPDATABLE_FILES = ["launcher.py", "README.md", "requirements.txt", "run.bat", "build_exe.bat"]
LOCAL_UPDATE_ZIP = "MyLauncher-update.zip"   # если этот файл лежит рядом с launcher.py — обновится из него
# GitHub-репозиторий обновлений: лаунчер сам проверяет манифест и качает новые файлы
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/rrrrr2565/-/main/version.json"
# URL можно также вписать в файл update_url.txt рядом с launcher.py — тогда код менять не нужно

# 10 популярных модов (slug на Modrinth + подпись)
POPULAR_MODS = [
    ("fabric-api", "Fabric API — обязательная библиотека для Fabric-модов"),
    ("sodium", "Sodium — сильное ускорение графики"),
    ("iris", "Iris — поддержка шейдеров"),
    ("jei", "JEI — просмотр рецептов предметов"),
    ("lithium", "Lithium — оптимизация физики и логики"),
    ("modmenu", "Mod Menu — меню установленных модов"),
    ("appleskin", "AppleSkin — показывает сытость от еды"),
    ("ferrite-core", "Ferrite Core — экономит память RAM"),
    ("cloth-config", "Cloth Config — настройки для многих модов"),
    ("create", "Create — механизмы, конвейеры, автоматы"),
]

# Папка игры: по умолчанию стандартный .minecraft,
# можно положить файл launcher_dir.txt рядом с launcher.py со своим путём.
DEFAULT_MC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".minecraft")


def get_minecraft_dir() -> str:
    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launcher_dir.txt")
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


def fetch_mod_file(slug: str, mc_version: str, loader: str):
    """Возвращает (имя файла, url) последней совместимой версии мода или None."""
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
    for f in versions[0].get("files", []):
        if f.get("primary"):
            return f["filename"], f["url"]
    files = versions[0].get("files", [])
    if files:
        return files[0]["filename"], files[0]["url"]
    return None


def download_mod_file(url: str, dest_path: str):
    """Скачивает файл мода с потоком (для больших jar)."""
    with requests.get(url, headers=MODRINTH_HEADERS, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)


def get_mod_context(version_type: str, version: str):
    """Определяет (версия MC, загрузчик) для модов или None, если моды не поставить."""
    if version_type == "Forge" and "-" in version:
        return version.split("-")[0], "forge"
    if version_type == "Fabric":
        return version, "fabric"
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
    return os.path.dirname(os.path.abspath(__file__))


def _read_update_manifest_url() -> str:
    url_file = os.path.join(_launcher_dir(), "update_url.txt")
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
          {"version", "zip_url"} для скачивания по манифесту."""
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

    # 2) локальный zip рядом с launcher.py
    local = os.path.join(_launcher_dir(), LOCAL_UPDATE_ZIP)
    if os.path.isfile(local):
        ver = read_zip_launcher_version(local)
        if ver and _version_tuple(ver) > _version_tuple(APP_VERSION):
            return {"version": ver, "zip_path": local}

    return None


def download_update_zip(zip_url: str, log_fn=print) -> str:
    """Скачивает zip обновления рядом с launcher.py и возвращает путь."""
    dest = os.path.join(_launcher_dir(), LOCAL_UPDATE_ZIP)
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
    me = os.path.abspath(__file__)
    if "launcher.py" in files and os.path.isfile(me):
        shutil.copy2(me, os.path.join(_launcher_dir(), "launcher.py.bak"))
        log_fn("[ОБНОВЛЕНИЕ] Бэкап старого launcher.py сохранён")

    updated = 0
    for fname, url in files.items():
        if fname not in UPDATABLE_FILES:
            log_fn(f"[ОБНОВЛЕНИЕ] Пропущен посторонний файл {fname}")
            continue
        dest = os.path.join(_launcher_dir(), fname)
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

        me = os.path.abspath(__file__)
        if os.path.isfile(me):
            shutil.copy2(me, os.path.join(_launcher_dir(), "launcher.py.bak"))
            log_fn("[ОБНОВЛЕНИЕ] Бэкап старого launcher.py сохранён")

        updated = 0
        for fname in UPDATABLE_FILES:
            member = prefix + fname
            if member in names:
                data = z.read(member)
                with open(os.path.join(_launcher_dir(), fname), "wb") as fh:
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

    def __init__(self):
        super().__init__()

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
        self._log_queue = queue.Queue()  # потокобезопасная очередь лога

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

        # Список версий
        lbl_ver = ctk.CTkLabel(
            left, text="Версия",
            font=self._font(12), text_color=self.text_dim,
        )
        lbl_ver.pack(anchor="w", padx=20, pady=(12, 2))

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

        # Кнопка открытия окна популярных модов
        self.mods_btn = ctk.CTkButton(
            left,
            text="ПОПУЛЯРНЫЕ МОДЫ (10)",
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
        log_title = ctk.CTkLabel(
            right, text="КОНСОЛЬ",
            font=self._font(14, "bold"),
            text_color=self.text_main,
        )
        log_title.pack(anchor="w", padx=20, pady=(16, 4))

        self.log_box = ctk.CTkTextbox(
            right,
            fg_color="#0f0f17",
            text_color="#b9c8b9",
            font=ctk.CTkFont(family=self._font_mono, size=12),
        )
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        self.log_box.configure(state="disabled")

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
        self._log(f"Папка игры: {self.mc_dir}")

    # ------------------------------------------------------------ утилиты
    def _font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        return ctk.CTkFont(family=self._font_family, size=size, weight=weight)

    def _log(self, text: str):
        """Потокобезопасно: кладёт строку в очередь, выводит главный поток."""
        self._log_queue.put(text)

    def _poll_log(self):
        """Выгружает очередь лога в консоль (выполняется в главном потоке)."""
        try:
            while True:
                text = self._log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", text + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

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
        self.after(0, lambda: self.version_box.configure(values=["Загрузка..."]))
        try:
            versions = self._fetch_versions(self.version_type)
        except Exception as e:
            self._log(f"[ОШИБКА] Не удалось получить список версий: {e}")
            versions = []

        self.versions = versions
        if versions:
            self.after(0, lambda: self.version_box.configure(values=versions))
            self.after(0, lambda: self.version_box.set(versions[0]))
        else:
            self.after(0, lambda: self.version_box.configure(values=["Нет данных"]))
            self.after(0, lambda: self.version_box.set("Нет данных"))

        self._update_installed_label()

    def _fetch_versions(self, vtype: str):
        if vtype == "Релизы":
            return [v["id"] for v in mll.utils.get_version_list() if v.get("type") == "release"]
        if vtype == "Снапшоты":
            return [v["id"] for v in mll.utils.get_version_list() if v.get("type") == "snapshot"]
        if vtype == "Fabric":
            # Fabric: берём стабильные версии Minecraft (последний загрузчик ставится сам)
            return list(mll.fabric.get_stable_minecraft_versions())
        if vtype == "Forge":
            return mll.forge.list_forge_versions()
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
            self._log(f"[JAVA] Найдено: {found}")
        else:
            self._log("[JAVA] Java не найдена в PATH. Установите Java 17+ и укажите путь вручную.")

    def _get_java_path(self) -> str:
        return self.java_entry.get().strip() or None

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

            # --- установка нужного типа версии ---
            if self.version_type == "Fabric":
                if not mll.fabric.is_minecraft_version_supported(version):
                    raise RuntimeError(f"Fabric не поддерживает версию {version}")
                self._log(f"[УСТАНОВКА] Fabric {version}")
                mll.fabric.install_fabric(version, self.mc_dir, callback=callback)
            elif self.version_type == "Forge":
                if not mll.forge.is_forge_version_valid(version):
                    raise RuntimeError(f"Версия Forge {version} не найдена")
                if not mll.forge.supports_automatic_install(version):
                    raise RuntimeError(
                        f"Forge {version} слишком старый: требуется ручная установка установщиком Forge"
                    )
                self._log(f"[УСТАНОВКА] Forge {version}")
                mll.forge.install_forge_version(version, self.mc_dir, callback=callback)
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
            }
            java = self._get_java_path()
            if java:
                options["executablePath"] = java

            command = mll.command.get_minecraft_command(version, self.mc_dir, options)
            self._log(f"[ЗАПУСК] Minecraft {version}, игрок: {nick}")
            self._log("[ЗАПУСК] java " + " ".join(command[1:6]) + " ...")

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

            # читаем лог игры в консоль лаунчера
            assert self.game_proc.stdout is not None
            for line in self.game_proc.stdout:
                self._log(line.rstrip())

            code = self.game_proc.wait()
            self._log(f"[ИГРА] Процесс завершён с кодом {code}")
            if code == 0 and year is not None and year < 2013:
                self._log("[ИГРА] Древние версии часто завершаются сразу после старта.")
                self._log("[ИГРА] Попробуйте выбрать версию 1.20.1 или новее во вкладке «Релизы».")

        except Exception as e:
            self._log(f"[ОШИБКА] {e}")
            self._set_status(f"Ошибка: {e}")
            self._set_progress(0)
        finally:
            self.installing = False
            self.after(0, lambda: self.play_btn.configure(state="normal", text="ИГРАТЬ"))
            self._update_installed_label()

    def _on_stop_game(self):
        if self.game_proc and self.game_proc.poll() is None:
            self._log("[ИГРА] Принудительное завершение процесса игры")
            self.game_proc.terminate()
        else:
            self._log("[ИГРА] Игра не запущена")

    # ------------------------------------------------------------ моды
    def _on_mods(self):
        version = self.version_box.get()
        ctx = get_mod_context(self.version_type, version)
        ModsWindow(self, ctx, self.mc_dir, self._log)

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
# Окно «Популярные моды» — загрузка с Modrinth
# ----------------------------------------------------------------------------
class ModsWindow(ctk.CTkToplevel):

    def __init__(self, master, ctx, mc_dir, log_fn):
        super().__init__(master)

        self.ctx = ctx              # (версия MC, загрузчик) или None
        self.mc_dir = mc_dir
        # потокобезопасный лог: из потока качания — через after в главный поток
        self.log = lambda msg: self.after(0, lambda: log_fn(msg))
        self.downloading = False

        self.title(f"{APP_NAME} — популярные моды")
        self.geometry("640x560")
        self.minsize(560, 480)
        self.configure(fg_color=master.bg_main)
        self.transient(master)
        self.lift()
        self.focus_force()

        fam = master._font_family
        self._font = lambda size, weight="normal": ctk.CTkFont(family=fam, size=size, weight=weight)

        # Шапка с контекстом
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(14, 6))

        if ctx:
            mc_version, loader = ctx
            loader_name = "Forge" if loader == "forge" else "Fabric"
            info = f"Моды для Minecraft {mc_version} ({loader_name}) — папка mods"
        else:
            info = "Моды доступны для версий Fabric и Forge. Выберите такой тип версии и попробуйте снова."

        self.info_label = ctk.CTkLabel(
            top, text=info, font=self._font(12),
            text_color=master.text_main if ctx else "#d8a33a",
            anchor="w", justify="left", wraplength=600,
        )
        self.info_label.pack(fill="x")

        # Список модов
        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color=master.bg_panel, corner_radius=12
        )
        self.scroll.pack(fill="both", expand=True, padx=16, pady=8)

        self.check_vars = []
        for slug, desc in POPULAR_MODS:
            row = ctk.CTkFrame(self.scroll, fg_color=master.bg_entry, corner_radius=8)
            row.pack(fill="x", padx=8, pady=4)

            var = ctk.BooleanVar(value=bool(ctx))
            box = ctk.CTkCheckBox(
                row, text=desc, variable=var, width=520,
                font=self._font(12),
                fg_color=master.accent_purple, hover_color="#8b70e0",
                checkmark_color="#ffffff",
            )
            box.pack(anchor="w", padx=10, pady=8)
            self.check_vars.append((slug, var))

        # Низ: кнопки и статус
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=16, pady=(6, 14))

        self.status_label = ctk.CTkLabel(
            bottom, text="", font=self._font(11), text_color=master.text_dim, anchor="w",
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

    # -------------------------------------------------------- скачивание
    def _download_selected(self):
        if self.downloading or not self.ctx:
            return
        selected = [(slug, var.get()) for slug, var in self.check_vars]
        chosen = [slug for slug, on in selected if on]
        if not chosen:
            self.status_label.configure(text="Ничего не выбрано")
            return
        self.downloading = True
        self.download_btn.configure(state="disabled", text="СКАЧИВАЮ...")
        threading.Thread(
            target=self._download_worker, args=(chosen, self.ctx), daemon=True
        ).start()

    def _download_worker(self, slugs, ctx):
        mc_version, loader = ctx
        mods_dir = os.path.join(self.mc_dir, "mods")
        os.makedirs(mods_dir, exist_ok=True)

        ok, skip, fail = 0, 0, 0
        total = len(slugs)
        for i, slug in enumerate(slugs, 1):
            self.after(0, lambda i=i, t=total, s=slug: self.status_label.configure(
                text=f"Мод {s}: запрос версии ({i}/{t})..."
            ))
            try:
                info = fetch_mod_file(slug, mc_version, loader)
                if info is None:
                    skip += 1
                    self.log(f"[МОД] {slug}: нет версии для MC {mc_version} ({loader})")
                    continue
                filename, url = info
                self.after(0, lambda f=filename: self.status_label.configure(
                    text=f"Скачиваю {f}..."
                ))
                dest = os.path.join(mods_dir, filename)
                if os.path.isfile(dest):
                    self.log(f"[МОД] {filename}: уже скачан, пропускаю")
                    ok += 1
                    continue
                download_mod_file(url, dest)
                ok += 1
                self.log(f"[МОД] {filename}: скачан в mods")
            except Exception as e:
                fail += 1
                self.log(f"[МОД] {slug}: ошибка — {e}")

        def finish():
            self.status_label.configure(
                text=f"Готово: скачано {ok}, пропущено {skip}, ошибок {fail}. "
                     f"Моды появятся в игре после перезапуска Minecraft."
            )
            self.download_btn.configure(state="normal", text="СКАЧАТЬ ВЫБРАННЫЕ")
            self.downloading = False

        self.after(0, finish)


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
