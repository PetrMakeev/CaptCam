import os
import time
import psutil
import sys
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException
from PIL import Image
import logging
from logging.handlers import RotatingFileHandler
from rich.console import Console
from rich.live import Live
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
from ruamel.yaml import YAML  # <-- ИЗМЕНЕНО: ruamel.yaml вместо yaml
import cv2
import glob
import threading
import queue
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ----------------------------------------------------------------------
# Подавление мусорных логов
# ----------------------------------------------------------------------
import urllib3
from selenium.webdriver.remote.remote_connection import LOGGER as SELENIUM_LOGGER

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("selenium").setLevel(logging.ERROR)
SELENIUM_LOGGER.setLevel(logging.ERROR)

# ----------------------------------------------------------------------
# Блокировка дублирующего запуска (Windows)
# ----------------------------------------------------------------------
if sys.platform.startswith('win'):
    import win32event
    import win32api
    from winerror import ERROR_ALREADY_EXISTS

    mutex = win32event.CreateMutex(None, False, "Global\\CaptureApp_SingleInstance_Mutex")
    if win32api.GetLastError() == ERROR_ALREADY_EXISTS:
        Console().print("[bold red]Ошибка: Приложение уже запущено![/]")
        sys.exit(1)

# ----------------------------------------------------------------------
# Утилиты
# ----------------------------------------------------------------------
def cleanup_processes():
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            name = proc.info['name'].lower()
            if name in ['chromedriver.exe', 'chrome.exe']:
                proc.kill()
                logging.info(f"Убит: {name} (PID: {proc.info['pid']})")
        except Exception as e:
            logging.warning(f"Не удалось убить процесс: {e}")
            pass

def is_image_black(img):
    try:
        w, h = img.size
        for x in range(0, w, 10):
            for y in range(0, h, 10):
                if img.getpixel((x, y))[:3] != (0, 0, 0):
                    return False
        return True
    except:
        return False

# ----------------------------------------------------------------------
# Глобальный console
# ----------------------------------------------------------------------
console = Console()

# Логирование
handler = RotatingFileHandler(
    'capture.log',
    maxBytes=5*1024*1024,
    backupCount=5,
    delay=True,
    encoding='utf-8'
)
logging.basicConfig(
    handlers=[handler],
    level=logging.INFO,
    format='%(asctime)s - %(message)s'
)

logging.info("=== ПРИЛОЖЕНИЕ ЗАПУЩЕНО ===")
logging.info(f"Время запуска: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
logging.info("Очистка старых процессов Chrome/Driver...")
cleanup_processes()

# ----------------------------------------------------------------------
# Очереди
# ----------------------------------------------------------------------
config_queue = queue.Queue()
live_queue = queue.Queue()

# ----------------------------------------------------------------------
# Конфиг — с ruamel.yaml для контроля кавычек
# ----------------------------------------------------------------------
class ConfigManager:
    DEFAULT_CONFIG = {
        'adress_url': 'http://maps.ufanet.ru/orenburg#1759214666SGR59',
        'time_begin': '07:00',
        'time_end': '20:00',
        'time_period_interval': 15,
        'time_video': '20:01',
        'video_fps': 60,
        'delete_frames_after_video': False,
        'console_rich': True
    }

    def __init__(self, filename='config.yaml'):
        self.filename = filename
        self.config = {}
        self.yaml = YAML()  # <-- ИЗМЕНЕНО: ruamel.yaml instance
        self.yaml.preserve_quotes = False  # <-- КЛЮЧЕВОЕ: убирает лишние кавычки
        self._load()

    def _load(self):
        if not os.path.exists(self.filename):
            with open(self.filename, 'w', encoding='utf-8') as f:
                self.yaml.dump(self.DEFAULT_CONFIG, f)  # <-- dump вместо safe_dump
            logging.info(f"Создан шаблон {self.filename}")
            self.config = self.DEFAULT_CONFIG.copy()
            return

        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                loaded = self.yaml.load(f) or {}  # <-- load вместо safe_load
            for k, v in self.DEFAULT_CONFIG.items():
                if k not in loaded:
                    loaded[k] = v

            end_h, end_m = map(int, str(loaded['time_end']).split(':'))
            create_h, create_m = map(int, str(loaded['time_video']).split(':'))
            end_total = end_h * 60 + end_m
            create_total = create_h * 60 + create_m

            if create_total <= end_total:
                new_create_total = end_total + 1
                new_h = new_create_total // 60
                new_m = new_create_total % 60
                new_time = f"{new_h:02d}:{new_m:02d}"
                loaded['time_video'] = new_time
                logging.warning(f"time_video <= time_end → исправлено на {new_time}")

            for t in ['time_begin', 'time_end', 'time_video']:
                datetime.strptime(str(loaded[t]), '%H:%M')
            if loaded['time_period_interval'] <= 0 or loaded['video_fps'] <= 0:
                raise ValueError("Интервал/FPS <= 0")

            logging.info(f"Конфиг загружен: {loaded}")
            self.config = loaded

            with open(self.filename, 'w', encoding='utf-8') as f:
                self.yaml.dump(self.config, f)  # <-- сохраняет БЕЗ кавычек

        except Exception as e:
            logging.error(f"Ошибка config: {e}")
            self.config = self.DEFAULT_CONFIG.copy()
            with open(self.filename, 'w', encoding='utf-8') as f:
                self.yaml.dump(self.config, f)

    def update(self, new_config):
        self.config.update(new_config)
        with open(self.filename, 'w', encoding='utf-8') as f:
            self.yaml.dump(self.config, f)
        logging.info(f"Конфиг обновлён: {new_config}")

    def __getitem__(self, key):
        return self.config[key]

    def __setitem__(self, key, value):
        self.config[key] = value

    def get(self, key, default=None):
        return self.config.get(key, default)


# ----------------------------------------------------------------------
# Драйвер браузера — reload_via_url() + refresh()
# ----------------------------------------------------------------------
class BrowserDriver:
    def __init__(self, config):
        self.config = config
        self.driver = None
        self.div_element = None
        self.iframe_element = None
        self._setup_driver()
        self._init_page()

    def _setup_driver(self):
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chromedriver_path = os.path.join(sys._MEIPASS, "chromedriver.exe") if getattr(sys, 'frozen', False) else "chromedriver.exe"
        service = Service(executable_path=chromedriver_path)
        self.driver = webdriver.Chrome(service=service, options=chrome_options)

    def _init_page(self):
        try:
            self.driver.get(self.config['adress_url'])
            self.div_element = WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.ID, "ModalBodyPlayer"))
            )
            self.iframe_element = WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, "iframe"))
            )
        except Exception as e:
            logging.error(f"Не загрузилась страница: {e}")
            sys.exit(1)

    def reload_via_url(self):
        try:
            logging.info("Перезагрузка: driver.get(url) + refresh()")
            self.driver.get(self.config['adress_url'])
            self.driver.refresh()
            time.sleep(1)

            self.div_element = WebDriverWait(self.driver, 25).until(
                EC.presence_of_element_located((By.ID, "ModalBodyPlayer"))
            )
            self.iframe_element = WebDriverWait(self.driver, 25).until(
                EC.presence_of_element_located((By.TAG_NAME, "iframe"))
            )

            src = self.iframe_element.get_attribute("src") or ""
            if not src or "about:blank" in src:
                logging.warning("iframe src пустой после refresh")
                return False

            time.sleep(1)
            logging.info("Страница перезагружена: get() + refresh()")
            return True
        except Exception as e:
            logging.error(f"Ошибка reload_via_url: {e}")
            return False

    def restart(self):
        try: self.driver.quit()
        except: pass
        cleanup_processes()
        time.sleep(2)
        self._setup_driver()
        self._init_page()
        logging.info("Драйвер полностью перезапущен (профилактика)")

    def get_iframe_size(self):
        try:
            return self.driver.execute_script("return arguments[0].getBoundingClientRect()", self.iframe_element)
        except Exception as e:
            logging.warning(f"Ошибка get_iframe_size: {e}")
            return None

    def capture_frame(self, file_path):
        try:
            self.driver.switch_to.frame(self.iframe_element)
            try:
                video = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, "video")))
                video.screenshot(file_path)
            except:
                self.driver.switch_to.default_content()
                self.iframe_element.screenshot(file_path)
            else:
                self.driver.switch_to.default_content()
            return True
        except Exception as e:
            logging.warning(f"Ошибка захвата кадра: {e}")
            return False


# ----------------------------------------------------------------------
# Обработчик захвата — reload_via_url() при ошибках
# ----------------------------------------------------------------------
class FrameCapture:
    def __init__(self, config, driver):
        self.config = config
        self.driver = driver
        self.last_file = None

    def count_existing_frames(self):
        date_str = datetime.now().strftime("%Y%m%d")
        folder = os.path.join("capture", date_str)
        if not os.path.exists(folder):
            return 0
        return len([f for f in os.listdir(folder) if f.startswith("capt-") and f.endswith(".png")])

    def capture(self):
        now = datetime.now()
        date_str = now.strftime("%Y%m%d")
        time_str = now.strftime("%H-%M-%S")
        filename = f"capt-{date_str}_{time_str}.png"
        folder = os.path.join("capture", date_str)
        os.makedirs(folder, exist_ok=True)
        file_path = os.path.join(folder, filename)

        try:
            size = self.driver.get_iframe_size()
            if not size or size['width'] < 1 or size['height'] < 1:
                logging.warning("iframe размер некорректный → перезагрузка get+refresh")
                if self.driver.reload_via_url():
                    time.sleep(1)
                return False

            if not self.driver.capture_frame(file_path):
                logging.warning("capture_frame не удался → перезагрузка get+refresh")
                if self.driver.reload_via_url():
                    time.sleep(1)
                return False

            if is_image_black(Image.open(file_path)):
                os.remove(file_path)
                logging.warning("Чёрный кадр → перезагрузка get+refresh")
                if self.driver.reload_via_url():
                    time.sleep(1)
                return False

            with Image.open(file_path) as img:
                w, h = img.size
                if w < 132:
                    os.remove(file_path)
                    logging.warning(f"Узкий кадр (w={w}) → перезагрузка get+refresh")
                    if self.driver.reload_via_url():
                        time.sleep(1)
                    return False
                img.crop((66, 0, w-66, h)).save(file_path, quality=95)

            if os.path.getsize(file_path) / 1024 < 100:
                os.remove(file_path)
                logging.warning("Обманка (<100 КБ) → перезагрузка get+refresh")
                if self.driver.reload_via_url():
                    time.sleep(1)
                return False

            self.last_file = file_path
            return True

        except Exception as e:
            try: os.remove(file_path)
            except: pass
            logging.error("Перезагрузка get+refresh из-за исключения")
            if self.driver.reload_via_url():
                time.sleep(1)
            return False


# ----------------------------------------------------------------------
# Видеокодер — С НОМЕРАМИ И ПРОВЕРКОЙ
# ----------------------------------------------------------------------
class VideoEncoder:
    def __init__(self, config, ui):
        self.config = config
        self.ui = ui
        self.last_summary = ""

    def _get_unique_video_path(self, folder, date_str):
        base_name = f"video-{date_str}.mp4"
        video_path = os.path.join(folder, base_name)

        if not self.config['delete_frames_after_video']:
            return video_path

        pattern = os.path.join(folder, f"video-{date_str}*.mp4")
        existing = glob.glob(pattern)
        if not existing:
            return video_path

        max_num = 0
        for f in existing:
            name = os.path.basename(f)
            if name == base_name:
                continue
            try:
                num = int(name.split('_')[-1].split('.')[0])
                max_num = max(max_num, num)
            except:
                pass
        new_name = f"video-{date_str}_{max_num + 1}.mp4"
        return os.path.join(folder, new_name)

    def encode(self, date_str):
        folder = os.path.join("capture", date_str)
        video_path = self._get_unique_video_path(folder, date_str)
        frames = sorted(glob.glob(os.path.join(folder, "capt-*.png")))
        if not frames:
            logging.info(f"Нет кадров для даты {date_str} — видео не создаётся")
            return

        start_time = datetime.now()
        h, w, _ = cv2.imread(frames[0]).shape
        writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*'mp4v'), self.config['video_fps'], (w, h))

        console.clear()

        with Progress(
            TextColumn("[bold blue]Создание видео: {task.fields[filename]}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("[bold cyan]{task.completed}/{task.total} кадр(ов)[/]"),
            TimeRemainingColumn(),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("", total=len(frames), filename=os.path.basename(video_path))
            for f in frames:
                writer.write(cv2.imread(f))
                progress.update(task, advance=1)

        writer.release()
        end_time = datetime.now()
        size_mb = os.path.getsize(video_path) / 1024 / 1024
        frames_count = len(frames)
        deleted = False

        if size_mb > 1 and self.config['delete_frames_after_video']:
            with Progress(
                TextColumn("[bold red]Удаление кадров..."),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TextColumn("[bold cyan]{task.completed}/{task.total} файл(ов)[/]"),
                console=console,
                transient=True
            ) as progress:
                task = progress.add_task("", total=frames_count)
                for f in frames:
                    try: os.remove(f)
                    except: pass
                    progress.update(task, advance=1)
            deleted = True

        self.last_summary = (
            f"[bold yellow]Конвертация: {end_time.strftime('%H:%M:%S')} | "
            f"Кадров: {frames_count} | "
            f"Файлы: {'удалены' if deleted else 'оставлены'} | "
            f"Видео: {os.path.basename(video_path)}[/]"
        )

        logging.info("=== КОНВЕРТАЦИЯ ЗАВЕРШЕНА ===")
        logging.info(f"Дата: {date_str}")
        logging.info(f"Видео: {video_path} ({size_mb:.1f} МБ)")
        logging.info(f"Кадров обработано: {frames_count}")
        logging.info(f"Файлы: {'удалены' if deleted else 'оставлены'}")
        logging.info(f"Время завершения: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info("=" * 40)

        time.sleep(0.5)
        if self.ui.live and self.ui.live.is_started:
            off_text = self.ui._generate_rich_off_status(self.ui.next_start)
            full_text = off_text + "\n" + self.last_summary
            self.ui.live.update(full_text)


# ----------------------------------------------------------------------
# Консольный вывод — БЕЗ camera_reload_interval
# ----------------------------------------------------------------------
class ConsoleUI:
    def __init__(self, config, frame_capture):
        self.config = config
        self.frame_capture = frame_capture
        self.console = Console()
        self.live = None
        self.live_queue = queue.Queue()
        self.next_start = None

    def _generate_rich_status(self, start_time):
        total_frames = self.frame_capture.count_existing_frames()
        return (
            f"[bold cyan]Текущие настройки:[/]\n"
            f"adress_url: {self.config['adress_url']}\n"
            f"time_begin: {self.config['time_begin']}\n"
            f"time_end: {self.config['time_end']}\n"
            f"time_period_interval: {self.config['time_period_interval']}\n"
            f"time_video: {self.config['time_video']}\n"
            f"video_fps: {self.config['video_fps']}\n"
            f"delete_frames_after_video: {self.config['delete_frames_after_video']}\n"
            f"{'#' * 50}\n"
            f"Запуск захват кадров в {start_time}\n"
            f"Захвачено кадров (всего за сутки): {total_frames}\n"
            f"Последний: {self.frame_capture.last_file or 'Нет'}"
        )

    def _generate_rich_off_status(self, next_start):
        self.next_start = next_start
        return (
            f"[bold cyan]Текущие настройки:[/]\n"
            f"adress_url: {self.config['adress_url']}\n"
            f"time_begin: {self.config['time_begin']}\n"
            f"time_end: {self.config['time_end']}\n"
            f"time_period_interval: {self.config['time_period_interval']}\n"
            f"time_video: {self.config['time_video']}\n"
            f"video_fps: {self.config['video_fps']}\n"
            f"delete_frames_after_video: {self.config['delete_frames_after_video']}\n"
            f"{'#' * 50}\n"
            f"[bold red]Захват остановлен\nСледующий: {next_start}[/]"
        )

    def start_live(self, start_time=None, next_start=None):
        if self.live:
            self.live.stop()
        self.console.clear()
        self.live = Live(console=self.console, refresh_per_second=1)
        self.live.start()
        if start_time:
            self.live.update(self._generate_rich_status(start_time))
        else:
            self.live.update(self._generate_rich_off_status(next_start))
        self.live_queue.put(self.live)

    def update_status(self, start_time):
        if self.live and self.live.is_started:
            self.live.update(self._generate_rich_status(start_time))

    def update_off_status(self, next_start):
        if self.live and self.live.is_started:
            base = self._generate_rich_off_status(next_start)
            if hasattr(self.encoder, 'last_summary') and self.encoder.last_summary:
                base += "\n" + self.encoder.last_summary
            self.live.update(base)


# ----------------------------------------------------------------------
# Watchdog
# ----------------------------------------------------------------------
class ConfigWatcher(FileSystemEventHandler):
    def __init__(self, config_manager, config_queue, live_queue):
        self.config_manager = config_manager
        self.config_queue = config_queue
        self.live_queue = live_queue
        self.last_modified = 0

    def on_modified(self, event):
        if not event.src_path.endswith('config.yaml'):
            return
        now = time.time()
        if now - self.last_modified < 1.5:
            return
        self.last_modified = now

        logging.info("Изменение config.yaml")
        new_cfg = self.config_manager._load()
        if new_cfg:
            self.config_manager.config = new_cfg
            self.config_queue.put(new_cfg.copy())

            try:
                live = self.live_queue.get_nowait()
                if live and live.is_started:
                    if 'Запуск захват кадров' in str(live._content):
                        live.update_status(live.start_time)
                    else:
                        live.update_off_status(live.next_start)
            except queue.Empty:
                pass


# ----------------------------------------------------------------------
# Основной контроллер — БЕЗ camera_reload_interval
# ----------------------------------------------------------------------
class CaptureApp:
    def __init__(self):
        self.config_manager = ConfigManager()
        self.config_queue = queue.Queue()
        self.live_queue = queue.Queue()

        self.driver = BrowserDriver(self.config_manager)
        self.frame_capture = FrameCapture(self.config_manager, self.driver)
        self.ui = ConsoleUI(self.config_manager, self.frame_capture)
        self.encoder = VideoEncoder(self.config_manager, self.ui)
        self.ui.encoder = self.encoder

        self.start_time = None
        self.current_state = None
        self.last_video_date = None

        self._start_watchdog()
        self._init_state()

    def _start_watchdog(self):
        handler = ConfigWatcher(self.config_manager, self.config_queue, self.live_queue)
        observer = Observer()
        observer.schedule(handler, path='.', recursive=False)
        threading.Thread(target=lambda: (observer.start(), [time.sleep(1) for _ in iter(int, 1)], observer.stop(), observer.join()), daemon=True).start()

    def _init_state(self):
        now = datetime.now()
        cur_total = now.hour * 60 + now.minute
        st_total = self._get_minutes(self.config_manager['time_begin'])
        en_total = self._get_minutes(self.config_manager['time_end'])
        self.current_state = "work" if st_total <= cur_total < en_total else "off"

        if self.current_state == "work":
            self.start_time = now.strftime('%Y-%m-%d %H:%M:%S')
            logging.info(f"Старт: {self.start_time}")
            self.ui.start_live(start_time=self.start_time)
        else:
            ns = self._next_start_time()
            logging.info(f"Ожидание: {ns}")
            self.ui.start_live(next_start=ns)

    def _get_minutes(self, time_str):
        h, m = map(int, time_str.split(':'))
        return h * 60 + m

    def _next_start_time(self):
        now = datetime.now()
        next_start = now.replace(
            hour=self._get_minutes(self.config_manager['time_begin']) // 60,
            minute=self._get_minutes(self.config_manager['time_begin']) % 60,
            second=0, microsecond=0
        )
        if now >= next_start:
            next_start += timedelta(days=1)
        return next_start.strftime('%Y-%m-%d %H:%M:%S')

    def _update_config(self):
        try:
            updated = self.config_queue.get_nowait()
            self.config_manager.config = updated
            logging.info(f"Применены: time_end={updated['time_end']}, time_period_interval={updated['time_period_interval']}")

            if self.current_state == "work":
                self.ui.update_status(self.start_time)
            else:
                self.ui.update_off_status(self._next_start_time())
        except queue.Empty:
            pass

    def run(self):
        try:
            while True:
                self._update_config()

                now = datetime.now()
                cur_total = now.hour * 60 + now.minute
                st_total = self._get_minutes(self.config_manager['time_begin'])
                en_total = self._get_minutes(self.config_manager['time_end'])
                new_state = "work" if st_total <= cur_total < en_total else "off"

                if self.current_state != new_state:
                    self.current_state = new_state
                    if self.current_state == "work":
                        self.start_time = now.strftime('%Y-%m-%d %H:%M:%S')
                        logging.info(f"Старт: {self.start_time}")
                        self.ui.start_live(start_time=self.start_time)
                    else:
                        ns = self._next_start_time()
                        logging.info(f"Стоп. Следующий: {ns}")
                        self.ui.start_live(next_start=ns)

                if self.current_state == "work":
                    if self.frame_capture.capture():
                        self.ui.update_status(self.start_time)

                if self.current_state == "off":
                    if cur_total >= self._get_minutes(self.config_manager['time_video']):
                        today = now.strftime("%Y%m%d")
                        if self.last_video_date != today:
                            logging.info(f"Запуск конвертации для даты: {today}")
                            self.encoder.encode(today)
                            self.last_video_date = today

                time.sleep(self.config_manager['time_period_interval'])

        except KeyboardInterrupt:
            console.print("\n[bold red]Остановлено пользователем[/]")
            logging.info("Приложение остановлено пользователем")
        finally:
            logging.getLogger().setLevel(logging.CRITICAL)
            logging.getLogger("urllib3").setLevel(logging.CRITICAL)
            logging.getLogger("selenium").setLevel(logging.CRITICAL)

            if hasattr(self.ui, 'live') and self.ui.live:
                try: self.ui.live.stop()
                except: pass
            try:
                self.driver.driver.quit()
            except: pass
            cleanup_processes()


# ----------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------
if __name__ == "__main__":
    app = CaptureApp()
    app.run()