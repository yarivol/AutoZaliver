"""AutoZaliver — Python 3.11+.

Установка:
  python -m pip install customtkinter watchdog google-api-python-client google-auth-oauthlib instagrapi tiktokautouploader
  phantomwright_driver install chromium
Для TikTok также нужны Node.js и npm в PATH (см. документацию пакета).
Запуск: python autozaliver.py
TXT: UTF-8, первая строка — заголовок YouTube, остальные — описание.
"""

import contextlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

import customtkinter as ctk
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# All settings are loaded only from config.json.
CONFIG = {}
_external_config = Path(__file__).resolve().parent / "config.json"
if not _external_config.exists():
    raise FileNotFoundError("�� ������ config.json ����� � autozaliver.py")
CONFIG.update(json.loads(_external_config.read_text(encoding="utf-8-sig")))
_required_config = {"to_upload", "done", "state_file", "youtube_token", "youtube_client_secrets", "youtube_privacy", "youtube_made_for_kids", "ig_username", "ig_password", "ig_proxy", "ig_session", "tiktok_profile", "enabled_platforms", "default_title", "default_description", "file_ready_seconds", "platform_pause_seconds", "cycle_pause_seconds", "network_timeout_seconds", "oauth_timeout_seconds"}
_missing_config = sorted(_required_config - CONFIG.keys())
if _missing_config:
    raise ValueError("� config.json ����������� ���������: " + ", ".join(_missing_config))


def local_path(key):
    path = Path(CONFIG[key]).expanduser()
    return path if path.is_absolute() else Path(__file__).resolve().parent / path


def safe_text(text):
    text = str(text)
    for key in ("ig_proxy", "ig_password"):
        secret = CONFIG[key]
        if secret:
            text = text.replace(secret, "<скрыто>")
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1<скрыто>@", text)


class Stopped(Exception):
    pass


class GuiStream:
    """Перенаправляет print сторонних библиотек в очередь GUI."""

    encoding = "utf-8"

    def __init__(self, emit):
        self.emit = emit

    def write(self, text):
        for line in text.replace("\r", "\n").splitlines():
            if line.strip():
                self.emit(line)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


class GuiLogHandler(logging.Handler):
    def __init__(self, emit):
        super().__init__()
        self.emit_message = emit

    def emit(self, record):
        self.emit_message(self.format(record))

class FolderEvents(FileSystemEventHandler):
    def __init__(self, wake):
        self.wake = wake

    def on_any_event(self, event):
        if event.event_type in {"created", "modified", "moved", "deleted"}:
            self.wake.set()


class Pipeline:
    def __init__(self, events):
        self.events = events
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.state = {"active": None, "next_allowed": 0}
        self.verification_requested = threading.Event()
        self.verification_code = None

    def log(self, message):
        self.events.put(("log", f"[{time.strftime('%H:%M:%S')}] {safe_text(message)}"))

    def status(self, message):
        self.events.put(("status", message))

    def check_stop(self):
        if self.stop.is_set():
            raise Stopped()

    def wait(self, seconds):
        # Эквивалент time.sleep(seconds), но с немедленной отменой паузы.
        if self.stop.wait(max(0, seconds)):
            raise Stopped()

    def save_state(self):
        target = local_path("state_file")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def files(self):
        return sorted((p for p in local_path("to_upload").iterdir()
                       if p.is_file() and p.suffix.lower() == ".mp4"), key=lambda p: p.name)

    def ready(self, video):
        self.status("🟡 Жду завершения записи файла")
        while True:
            before = video.stat()
            self.wait(CONFIG["file_ready_seconds"])
            after = video.stat()
            if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) and after.st_size:
                with video.open("rb"):
                    return
            self.log(f"{video.name}: файл еще записывается или пуст; повторное ожидание")

    def metadata(self, video):
        txt = video.with_suffix(".txt")
        # Единый заголовок для всех площадок — имя видео без расширения.
        # Первая строка TXT больше не используется как заголовок YouTube.
        title = video.stem
        if not txt.exists():
            self.log(f"{txt.name} отсутствует: использую тексты CONFIG")
            return title, CONFIG["default_description"]
        lines = txt.read_text(encoding="utf-8-sig").splitlines()
        description = "\n".join(lines[1:]) if len(lines) > 1 else CONFIG["default_description"]
        return title, description

    def youtube(self, video, title, description):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        scopes = ["https://www.googleapis.com/auth/youtube.upload"]
        token = local_path("youtube_token")
        credentials = Credentials.from_authorized_user_file(str(token), scopes) if token.exists() else None
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                self.log("YouTube: выполните первичный вход в открывшемся браузере")
                flow = InstalledAppFlow.from_client_secrets_file(str(local_path("youtube_client_secrets")), scopes)
                credentials = flow.run_local_server(port=0, timeout_seconds=CONFIG["oauth_timeout_seconds"])
            token.parent.mkdir(parents=True, exist_ok=True)
            token.write_text(credentials.to_json(), encoding="utf-8")
        # Используем штатный transport google-api-client. Ручной
        # AuthorizedHttp на некоторых версиях httplib2 теряет Location при
        # resumable redirect и вызывает RedirectMissingLocation.
        service = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        try:
            request = service.videos().insert(part="snippet,status", body={
                "snippet": {"title": title, "description": description, "categoryId": "22"},
                "status": {"privacyStatus": CONFIG["youtube_privacy"],
                           "selfDeclaredMadeForKids": CONFIG["youtube_made_for_kids"]},
            }, media_body=media)
            response = None
            while response is None:
                self.check_stop()
                progress, response = request.next_chunk(num_retries=5)
                if progress:
                    self.log(f"YouTube: {progress.progress():.0%}")
            self.log(f"YouTube: загружено, ID {response['id']}")
        finally:
            media.stream().close()
            service.close()

    def instagram(self, video, title, description):
        from instagrapi import Client
        from instagrapi.exceptions import TwoFactorRequired

        if CONFIG["ig_username"].startswith("YOUR_"):
            raise ValueError("Заполните ig_username и ig_password в CONFIG")
        client = Client()
        proxy_value = CONFIG["ig_proxy"].strip()
        if proxy_value:
            proxy = urlsplit(proxy_value)
            if proxy.scheme != "http" or not proxy.username or not proxy.password or not proxy.port:
                raise ValueError("Укажите IG-прокси в формате http://login:pass@IPv4:port или оставьте ig_proxy пустым")
            ipaddress.IPv4Address(proxy.hostname)
            client.set_proxy(proxy_value)
            self.log("Instagram: используется прокси")
        else:
            self.log("Instagram: подключение напрямую, без прокси")
        session = local_path("ig_session")
        if session.exists():
            client.load_settings(str(session))
        if proxy_value:
            client.set_proxy(proxy_value)
        try:
            client.login(CONFIG["ig_username"], CONFIG["ig_password"])
        except TwoFactorRequired:
            self.verification_code = None
            self.verification_requested.clear()
            self.events.put(("verification", None))
            self.log("Instagram: введите код двухфакторной проверки в окне программы")
            if self.verification_requested.wait(180):
                if not self.verification_code:
                    raise ValueError("Код Instagram не введен")
                client.login(CONFIG["ig_username"], CONFIG["ig_password"],
                             verification_code=self.verification_code)
            else:
                raise TimeoutError("Ожидание кода Instagram истекло")
        session.parent.mkdir(parents=True, exist_ok=True)
        client.dump_settings(str(session))
        result = client.clip_upload(str(video), caption=description)
        self.log(f"Instagram: загружено, ID {result.pk}")
        client.dump_settings(str(session))

    def tiktok(self, video, title, description):
        # Обход несовместимости tiktokautouploader с новым интерфейсом TikTok.
        # В старой версии библиотеки используется page.locator(
        # "button:has-text('Cancel')").is_visible(), но теперь TikTok
        # рисует две кнопки Cancel. Playwright в strict mode выбрасывает
        # исключение вместо выбора элемента. Подменяем только этот запрос,
        # оставляя остальные локаторы библиотеки без изменений.
        self._patch_tiktok_cancel_locator()
        from tiktokautouploader import upload_tiktok

        result = upload_tiktok(video=str(video), description=description,
                               accountname=CONFIG["tiktok_profile"])
        if result is False:
            raise RuntimeError("upload_tiktok вернул False")
        self.log("TikTok: upload_tiktok завершился без исключения; проверьте публикацию в профиле")

    @staticmethod
    def _patch_tiktok_cancel_locator():
        """Make the affected TikTok Cancel locator strict-mode safe.

        The package uses Playwright's synchronous API. The patch is idempotent
        and is intentionally limited to the exact selector from the failing
        third-party function, so application code and other selectors retain
        their normal strict-mode behavior.
        """
        # tiktokautouploader импортирует Page из phantomwright, а не из
        # обычного playwright. Поддерживаем оба варианта для разных версий.
        page_classes = []
        for module_name in ("phantomwright.sync_api", "playwright.sync_api"):
            try:
                module = __import__(module_name, fromlist=["Page"])
                page_classes.append(module.Page)
            except (ImportError, AttributeError):
                continue
        for page_class in page_classes:
            if getattr(page_class.locator, "_autozaliver_cancel_patch", False):
                continue
            original_locator = page_class.locator

            def patched_locator(self, selector, *args, _original=original_locator, **kwargs):
                locator = _original(self, selector, *args, **kwargs)
                if isinstance(selector, str) and selector.strip() in {
                    "button:has-text('Cancel')",
                    'button:has-text("Cancel")',
                }:
                    return locator.first
                return locator

            patched_locator._autozaliver_cancel_patch = True
            page_class.locator = patched_locator

    def process(self, video):
        active = self.state["active"]
        if active is None:
            self.ready(video)
            title, description = self.metadata(video)
            # Instagram и TikTok получают название в начале подписи;
            # YouTube получает его отдельным полем title.
            description = f"{title}\n{description}".strip()
            active = {"file": str(video), "title": title, "description": description,
                      "attempts": {}, "pause_until": 0, "archive": None}
            self.state["active"] = active
            self.save_state()
        all_platforms = [("YouTube", self.youtube), ("Instagram", self.instagram), ("TikTok", self.tiktok)]
        platforms = [(name, upload) for name, upload in all_platforms
                     if name in CONFIG["enabled_platforms"]]
        if not platforms:
            raise ValueError("enabled_platforms не содержит ни одной площадки")
        for index, (name, upload) in enumerate(platforms):
            if name in active["attempts"]:
                continue
            self.wait(active["pause_until"] - time.time())
            self.check_stop()
            # Сохраняем попытку ДО сетевого вызова: после аварии не дублируем публикацию.
            active["attempts"][name] = "исход неизвестен"
            self.save_state()
            self.status(f"🟡 Загрузка в {name}")
            self.log(f"{video.name}: начало загрузки в {name}")
            try:
                upload(video, active["title"], active["description"])
            except Exception as exc:
                active["attempts"][name] = "ошибка"
                self.log(f"Ошибка {name}: {type(exc).__name__}: {exc}")
            else:
                active["attempts"][name] = "вызов завершен"
            active["pause_until"] = time.time() + CONFIG["platform_pause_seconds"] if index < 2 else 0
            self.save_state()
            if index < 2:
                self.status("⏳ Пауза 20 секунд между платформами")

        # Один каталог на цикл предотвращает перезапись одноименных файлов в done.
        if not active["archive"]:
            active["archive"] = str(local_path("done") / f"{video.stem}_{uuid4().hex[:12]}")
            self.save_state()
        archive = Path(active["archive"])
        archive.mkdir(parents=True, exist_ok=True)
        for source in (video.with_suffix(".txt"), video):
            destination = archive / source.name
            if source.exists():
                if destination.exists():
                    raise FileExistsError(f"Архив уже содержит {destination}")
                shutil.move(str(source), str(destination))
            elif source == video and not destination.exists():
                raise FileNotFoundError(str(video))
        self.log(f"Цикл завершен: {active['attempts']}. Файлы: {archive}")
        self.state["active"] = None
        self.state["next_allowed"] = time.time() + CONFIG["cycle_pause_seconds"]
        self.save_state()
        self.status("💤 Сон 2 часа")
        self.log("Антиспам-пауза 7200 секунд")
        self.wait(CONFIG["cycle_pause_seconds"])

    def run(self):
        observer = Observer()
        try:
            for key in ("to_upload", "done"):
                local_path(key).mkdir(parents=True, exist_ok=True)
            state_file = local_path("state_file")
            if state_file.exists():
                self.state = json.loads(state_file.read_text(encoding="utf-8"))
            observer.schedule(FolderEvents(self.wake), str(local_path("to_upload")), recursive=False)
            observer.start()
            self.log("Мониторинг запущен. Учитываются также файлы, добавленные до запуска.")
            while True:
                self.check_stop()
                remaining = self.state["next_allowed"] - time.time()
                if remaining > 0:
                    self.status(f"💤 Антиспам-пауза: осталось {remaining / 60:.1f} мин")
                    self.wait(remaining)
                self.wake.clear()
                active = self.state["active"]
                files = self.files()
                if active:
                    self.process(Path(active["file"]))
                elif files:
                    self.process(files[0])
                else:
                    self.status("🟢 Жду файлы")
                    self.wake.wait(1)
        except Stopped:
            self.log("Работа остановлена; прогресс и антиспам-таймер сохранены")
        except Exception as exc:
            self.log(f"Конвейер остановлен: {type(exc).__name__}: {exc}")
        finally:
            if observer.is_alive():
                observer.stop()
                observer.join()
            self.events.put(("finished", None))


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AutoZaliver · Shorts / Reels / TikTok")
        self.geometry("960x640")
        self.minsize(720, 460)
        self.events = queue.Queue()
        self.pipeline = Pipeline(self.events)
        self.worker = None
        self.closing = False
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)
        ctk.CTkLabel(self, text="AutoZaliver", font=ctk.CTkFont(size=28, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=24, pady=(20, 10))
        controls = ctk.CTkFrame(self)
        controls.grid(row=1, column=0, sticky="ew", padx=24)
        self.start_button = ctk.CTkButton(controls, text="Старт", command=self.start)
        self.start_button.pack(side="left", padx=12, pady=12)
        self.stop_button = ctk.CTkButton(controls, text="Стоп", command=self.stop,
                                         fg_color="#a53535", state="disabled")
        self.stop_button.pack(side="left", padx=4, pady=12)
        self.counter = ctk.CTkLabel(controls, text="В очереди: 0")
        self.counter.pack(side="right", padx=20)
        self.status_label = ctk.CTkLabel(self, text="⏹ Остановлено", anchor="w")
        self.status_label.grid(row=2, column=0, sticky="ew", padx=24, pady=12)
        self.code_frame = ctk.CTkFrame(self)
        self.code_entry = ctk.CTkEntry(self.code_frame, placeholder_text="Код Instagram")
        self.code_entry.pack(side="left", padx=8, pady=8)
        ctk.CTkButton(self.code_frame, text="Отправить код", command=self.submit_code).pack(side="left", padx=8, pady=8)
        self.console = ctk.CTkTextbox(self, state="disabled", wrap="word")
        self.console.grid(row=4, column=0, sticky="nsew", padx=24, pady=(0, 24))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.drain)
        self.after(500, self.count_files)
        self.pipeline.log("Заполните CONFIG и положите видео и TXT в to_upload. Затем нажмите Старт.")

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.pipeline.stop.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.worker = threading.Thread(target=self.pipeline.run, name="upload-pipeline", daemon=False)
        self.worker.start()

    def stop(self):
        self.pipeline.stop.set()
        self.pipeline.wake.set()
        self.stop_button.configure(state="disabled")
        self.status_label.configure(text="⏳ Остановка после текущего вызова загрузчика")

    def close(self):
        self.closing = True
        if self.worker and self.worker.is_alive():
            self.stop()
        else:
            self.destroy()

    def drain(self):
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.console.configure(state="normal")
                self.console.insert("end", value + "\n")
                if int(self.console.index("end-1c").split(".")[0]) > 5000:
                    self.console.delete("1.0", "1000.0")
                self.console.see("end")
                self.console.configure(state="disabled")
            elif kind == "status" and not self.pipeline.stop.is_set():
                self.status_label.configure(text=value)
            elif kind == "count":
                self.counter.configure(text=f"В очереди: {value}")
            elif kind == "verification":
                self.code_frame.grid(row=3, column=0, sticky="w", padx=24, pady=(0, 8))
                self.code_entry.focus_set()
            elif kind == "finished":
                self.status_label.configure(text="⏹ Остановлено")
                self.stop_button.configure(state="disabled")
        if self.worker and not self.worker.is_alive():
            self.start_button.configure(state="normal")
        if self.closing and (not self.worker or not self.worker.is_alive()):
            self.destroy()
            return
        self.after(100, self.drain)

    def submit_code(self):
        code = self.code_entry.get().strip()
        if code:
            self.pipeline.verification_code = code
            self.pipeline.verification_requested.set()
            self.code_entry.delete(0, "end")
            self.code_frame.grid_remove()

    def count_files(self):
        # Чтение каталога также выполняется вне GUI, включая сетевые пути.
        def count():
            try:
                amount = len(self.pipeline.files())
            except OSError:
                amount = 0
            self.events.put(("count", amount))
        # Результат обрабатывается в drain, никаких вызовов Tk из потока.
        if not hasattr(self, "counter_worker") or not self.counter_worker.is_alive():
            self.counter_worker = threading.Thread(target=count, daemon=True)
            self.counter_worker.start()
        self.after(1000, self.count_files)


def main():
    # TikTok хранит профиль относительно рабочего каталога.
    os.chdir(Path(__file__).resolve().parent)
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = App()
    handler = GuiLogHandler(app.pipeline.log)
    logging.getLogger().addHandler(handler)
    stream = GuiStream(app.pipeline.log)
    try:
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            app.mainloop()
    finally:
        logging.getLogger().removeHandler(handler)


if __name__ == "__main__":
    main()

