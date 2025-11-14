import os
import sys
import time
import psutil
import queue
import threading
import logging
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QProgressBar, QTextEdit,
    QFrame, QStackedWidget, QMessageBox, QStyleFactory,
    QCheckBox, QSizePolicy, QSpacerItem
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QFont, QPalette, QColor

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from PIL import Image
import cv2
import glob

from ruamel.yaml import YAML
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ----------------------------------------------------------------------
# Логи (безопасная ротация по суткам)
# ----------------------------------------------------------------------
import urllib3
from selenium.webdriver.remote.remote_connection import LOGGER as SELENIUM_LOGGER

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("selenium").setLevel(logging.ERROR)
SELENIUM_LOGGER.setLevel(logging.ERROR)

LOG_DIR = "."
LOG_BASE = "capture"
LOG_EXT = ".log"
MAX_LOG_DAYS = 5

def get_current_log_path():
    return os.path.join(LOG_DIR, f"{LOG_BASE}{LOG_EXT}")

def get_dated_log_path(date_str):
    return os.path.join(LOG_DIR, f"{LOG_BASE}_{date_str}{LOG_EXT}")

def create_new_handler():
    handler = RotatingFileHandler(
        get_current_log_path(),
        maxBytes=5*1024*1024,
        backupCount=1,
        delay=True,
        encoding='utf-8'
    )
    handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    return handler

def replace_log_handler():
    root = logging.getLogger()
    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)
    root.addHandler(create_new_handler())
    root.setLevel(logging.INFO)

# Инициализация
replace_log_handler()
logging.info("=== GUI ПРИЛОЖЕНИЕ ЗАПУЩЕНО ===")

def rotate_log_if_needed():
    current_log = get_current_log_path()
    if not os.path.exists(current_log):
        return

    # Используем дату ПРОШЛЫХ суток
    yesterday = datetime.now() - timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y%m%d")
    dated_log = get_dated_log_path(yesterday_str)

    if os.path.exists(dated_log):
        return  # уже ротирован

    try:
        root = logging.getLogger()
        for h in root.handlers[:]:
            h.close()
            root.removeHandler(h)

        os.rename(current_log, dated_log)
        logging.info(f"Лог переименован: {current_log} → {dated_log}")

        replace_log_handler()

    except Exception as e:
        try:
            replace_log_handler()
        except:
            pass
        logging.warning(f"Не удалось ротировать лог: {e}")

    # Удаляем старые логи (>5 дней)
    cutoff = datetime.now() - timedelta(days=MAX_LOG_DAYS)
    for file in Path(LOG_DIR).glob(f"{LOG_BASE}_*{LOG_EXT}"):
        try:
            file_date_str = file.stem.split("_")[-1]
            file_date = datetime.strptime(file_date_str, "%Y%m%d")
            if file_date < cutoff:
                file.unlink()
                logging.info(f"Удалён старый лог: {file.name}")
        except Exception as e:
            logging.warning(f"Ошибка при удалении старого лога {file.name}: {e}")

# ----------------------------------------------------------------------
# Блокировка дублирующего запуска (Windows)
# ----------------------------------------------------------------------
if sys.platform.startswith('win'):
    import win32event
    import win32api
    from winerror import ERROR_ALREADY_EXISTS
    mutex = win32event.CreateMutex(None, False, "Global\\CaptureApp_SingleInstance_Mutex")
    if win32api.GetLastError() == ERROR_ALREADY_EXISTS:
        QMessageBox.critical(None, "Ошибка", "Приложение уже запущено!")
        sys.exit(1)

# ----------------------------------------------------------------------
# Утилиты
# ----------------------------------------------------------------------
def cleanup_processes():
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'].lower() in ['chromedriver.exe', 'chrome.exe']:
                proc.kill()
                logging.info(f"Убит: {proc.info['name']} (PID: {proc.info['pid']})")
        except Exception as e:
            logging.warning(f"Не удалось убить процесс: {e}")

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
    
def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)    

# ----------------------------------------------------------------------
# Валидация конфигурации
# ----------------------------------------------------------------------
def validate_config(data):
    errors = []
    required = ['time_begin', 'time_end', 'time_period_interval', 'time_video', 'video_fps', 'delete_frames_after_video']
    for key in required:
        if key not in data:
            errors.append(f"Отсутствует: {key}")

    if errors:
        return errors, None

    def parse_time(t, name):
        try:
            h, m = map(int, str(t).split(':'))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            return h * 60 + m
        except:
            errors.append(f"Неверный формат {name}: ожидается HH:MM")
            return None

    begin_min = parse_time(data['time_begin'], 'time_begin')
    end_min = parse_time(data['time_end'], 'time_end')
    video_min = parse_time(data['time_video'], 'time_video')

    if None not in (begin_min, end_min, video_min):
        if end_min <= begin_min:
            errors.append("time_end должен быть позже time_begin")
        if video_min <= end_min:
            errors.append("time_video должен быть позже time_end")

    try:
        interval = int(data['time_period_interval'])
        if interval <= 0:
            errors.append("time_period_interval должен быть > 0")
    except:
        errors.append("time_period_interval — целое число")
        interval = 0

    try:
        fps = int(data['video_fps'])
        if fps <= 0:
            errors.append("video_fps должен быть > 0")
    except:
        errors.append("video_fps — целое число")
        fps = 0

    def parse_bool(val, name):
        if isinstance(val, bool):
            return val
        val_str = str(val).lower()
        if val_str in ('true', 'false'):
            return val_str == 'true'
        errors.append(f"{name} должен быть true/false")
        return None

    delete_frames = parse_bool(data['delete_frames_after_video'], 'delete_frames_after_video')

    parsed = {
        'interval': interval,
        'fps': fps,
        'delete_frames': delete_frames
    }

    return errors, parsed

# ----------------------------------------------------------------------
# Конфиг
# ----------------------------------------------------------------------
class ConfigManager:
    DEFAULT_CONFIG = {
        'adress_url': 'http://maps.ufanet.ru/orenburg#1759214666SGR59',
        'time_begin': '07:00', 'time_end': '20:00',
        'time_period_interval': 15, 'time_video': '20:01',
        'video_fps': 60, 'delete_frames_after_video': False
    }

    def __init__(self, filename='config.yaml'):
        self.filename = filename
        self.yaml = YAML()
        self.yaml.preserve_quotes = False
        self.config = {}
        self._load()

    def _load(self):
        if not os.path.exists(self.filename):
            self._save_default()
            return

        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                loaded = self.yaml.load(f) or {}
            loaded = {**self.DEFAULT_CONFIG, **loaded}

            begin_min = self._to_minutes(loaded['time_begin'])
            end_min = self._to_minutes(loaded['time_end'])
            if end_min <= begin_min:
                loaded['time_end'] = self._from_minutes(begin_min + 1)
                logging.warning(f"time_end исправлен на {loaded['time_end']}")

            video_min = self._to_minutes(loaded['time_video'])
            if video_min <= end_min:
                loaded['time_video'] = self._from_minutes(end_min + 1)
                logging.warning(f"time_video исправлен на {loaded['time_video']}")

            self.config = loaded
            self._save()
        except Exception as e:
            logging.error(f"Ошибка загрузки config: {e}")
            self._save_default()

    def _save_default(self):
        self.config = self.DEFAULT_CONFIG.copy()
        self._save()

    def _save(self):
        with open(self.filename, 'w', encoding='utf-8') as f:
            self.yaml.dump(self.config, f)

    def _to_minutes(self, t):
        h, m = map(int, str(t).split(':'))
        return h * 60 + m

    def _from_minutes(self, m):
        return f"{m // 60:02d}:{m % 60:02d}"

    def update(self, new_config, gui_queue=None):
        old_video_time = self.config.get('time_video')
        self.config.update(new_config)
        self._save()
        logging.info(f"Конфиг обновлён: {new_config}")
        if gui_queue and old_video_time != new_config.get('time_video'):
            gui_queue.put(('config_update',))

    def __getitem__(self, key): return self.config[key]
    def __setitem__(self, key, value): self.config[key] = value

# ----------------------------------------------------------------------
# Драйвер
# ----------------------------------------------------------------------
class BrowserDriver:
    def __init__(self, config):
        self.config = config
        self.driver = None
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
            WebDriverWait(self.driver, 20).until(EC.presence_of_element_located((By.ID, "ModalBodyPlayer")))
            self.iframe_element = WebDriverWait(self.driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
        except Exception as e:
            logging.error(f"Не загрузилась страница: {e}")
            sys.exit(1)

    def reload_via_url(self):
        try:
            logging.info("Перезагрузка страницы")
            self.driver.get(self.config['adress_url'])
            self.driver.refresh()
            time.sleep(1)
            WebDriverWait(self.driver, 25).until(EC.presence_of_element_located((By.ID, "ModalBodyPlayer")))
            self.iframe_element = WebDriverWait(self.driver, 25).until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
            if not self.iframe_element.get_attribute("src") or "about:blank" in self.iframe_element.get_attribute("src"):
                logging.warning("iframe src пустой")
                return False
            return True
        except Exception as e:
            logging.error(f"Ошибка перезагрузки: {e}")
            return False

    def restart(self):
        try: self.driver.quit()
        except: pass
        cleanup_processes()
        time.sleep(2)
        self._setup_driver()
        self._init_page()
        logging.info("Драйвер перезапущен")

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
# Захват кадров
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
                logging.warning("iframe размер некорректный → перезагрузка")
                if self.driver.reload_via_url():
                    time.sleep(1)
                return False

            if not self.driver.capture_frame(file_path):
                logging.warning("capture_frame не удался → перезагрузка")
                if self.driver.reload_via_url():
                    time.sleep(1)
                return False

            if is_image_black(Image.open(file_path)):
                os.remove(file_path)
                logging.warning("Чёрный кадр → перезагрузка")
                if self.driver.reload_via_url():
                    time.sleep(1)
                return False

            with Image.open(file_path) as img:
                w, h = img.size
                if w < 132:
                    os.remove(file_path)
                    logging.warning(f"Узкий кадр (w={w}) → перезагрузка")
                    if self.driver.reload_via_url():
                        time.sleep(1)
                    return False
                img.crop((66, 0, w-66, h)).save(file_path, quality=95)

            if os.path.getsize(file_path) / 1024 < 100:
                os.remove(file_path)
                logging.warning("Обманка (<100 КБ) → перезагрузка")
                if self.driver.reload_via_url():
                    time.sleep(1)
                return False

            self.last_file = file_path
            return True

        except Exception as e:
            try: os.remove(file_path)
            except: pass
            logging.error("Перезагрузка из-за исключения")
            if self.driver.reload_via_url():
                time.sleep(1)
            return False

# ----------------------------------------------------------------------
# Видеокодер
# ----------------------------------------------------------------------
class VideoEncoder:
    def __init__(self, config, gui_queue):
        self.config = config
        self.gui_queue = gui_queue

    def _get_video_path(self, date_str):
        folder = os.path.join("capture", date_str)
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"video-{date_str}.mp4")

    def encode(self, date_str):
        video_path = self._get_video_path(date_str)
        frames = sorted(glob.glob(os.path.join("capture", date_str, "capt-*.png")))
        if not frames:
            logging.info(f"Нет кадров для {date_str}")
            return

        self.gui_queue.put(('video_start',))

        h, w, _ = cv2.imread(frames[0]).shape
        writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*'mp4v'), self.config['video_fps'], (w, h))

        total = len(frames)
        for i, f in enumerate(frames):
            writer.write(cv2.imread(f))
            self.gui_queue.put(('video_progress', i + 1, total))

        writer.release()
        summary = f"Конвертация завершена: {total} кадров → {os.path.basename(video_path)}"
        self.gui_queue.put(('video_done', summary))
        logging.info(summary)

        if self.config['delete_frames_after_video']:
            deleted = 0
            for f in frames:
                try:
                    os.remove(f)
                    deleted += 1
                except Exception as e:
                    logging.warning(f"Не удалось удалить {f}: {e}")
            logging.info(f"Удалено {deleted} кадров")
            self.gui_queue.put(('delete_done', deleted))
        else:
            self.gui_queue.put(('delete_done', 0))

# ----------------------------------------------------------------------
# Watchdog
# ----------------------------------------------------------------------
class ConfigWatcher(FileSystemEventHandler):
    def __init__(self, config_manager, config_queue):
        self.config_manager = config_manager
        self.config_queue = config_queue
        self.last_modified = 0

    def on_modified(self, event):
        if not event.src_path.endswith('config.yaml'):
            return
        now = time.time()
        if now - self.last_modified < 1.5:
            return
        self.last_modified = now
        logging.info("Изменение config.yaml")
        self.config_manager._load()
        self.config_queue.put(self.config_manager.config.copy())

# ----------------------------------------------------------------------
# GUI (PyQt6)
# ----------------------------------------------------------------------
class CaptureGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Захват кадров с камеры ...")
        self.setFixedSize(560, 380)

        icon_path = resource_path(os.path.join("resource", "eye.ico"))
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        else:
            logging.warning(f"Иконка не найдена: {icon_path}")

        self.config_manager = ConfigManager()
        self.gui_queue = queue.Queue()
        self.config_queue = queue.Queue()

        self.status_labels = {}
        self.init_ui()
        self.start_background()
        self.setup_timers()

        QTimer.singleShot(500, self.update_status_display)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.stacked = QStackedWidget()
        layout.addWidget(self.stacked)

        self.status_page = self.build_status_page()
        self.settings_page = self.build_settings_page()
        self.log_page = self.build_log_page()

        self.stacked.addWidget(self.status_page)
        self.stacked.addWidget(self.settings_page)
        self.stacked.addWidget(self.log_page)

        self.show_status_page()

    def build_status_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        grid = QGridLayout()
        layout.addLayout(grid)
        layout.addStretch()

        font = QFont("Consolas", 10)

        row = 0

        BTN_WIDTH = 120

        btn_container = QHBoxLayout()
        btn_container.setSpacing(5)
        btn_container.addStretch()

        btn_settings = QPushButton("Настройки")
        btn_settings.setFixedWidth(BTN_WIDTH)
        btn_settings.clicked.connect(self.show_settings_page)
        btn_container.addWidget(btn_settings)

        btn_log = QPushButton("Лог")
        btn_log.setFixedWidth(BTN_WIDTH)
        btn_log.clicked.connect(self.show_log_page)
        btn_container.addWidget(btn_log)

        grid.addLayout(btn_container, row, 0, 1, 2)
        row += 1

        labels = {
            'adress_url': 'Адрес камеры',
            'time_begin': 'Время начала захвата',
            'time_end': 'Время окончания захвата',
            'time_period_interval': 'Интервал между кадрами, сек',
        }
        for key, text in labels.items():
            lbl_text = QLabel(text)
            lbl_text.setFont(font)
            lbl_text.setAlignment(Qt.AlignmentFlag.AlignRight)
            grid.addWidget(lbl_text, row, 0)

            lbl_val = QLabel("")
            lbl_val.setFont(font)
            self.status_labels[key] = lbl_val
            grid.addWidget(lbl_val, row, 1)
            row += 1

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        grid.addWidget(line, row, 0, 1, 2)
        row += 1

        self.captured_count_label = QLabel("Сохранено кадров за текущие сутки: 0")
        self.captured_count_label.setFont(font)
        grid.addWidget(self.captured_count_label, row, 0, 1, 2)
        row += 1

        self.last_frame_status_label = QLabel("Последний кадр: Нет")
        self.last_frame_status_label.setFont(font)
        grid.addWidget(self.last_frame_status_label, row, 0, 1, 2)
        row += 1

        self.capture_pb = QProgressBar()
        self.capture_pb.setMaximum(100)
        self.capture_pb.setTextVisible(False)
        grid.addWidget(self.capture_pb, row, 0, 1, 2)
        row += 1

        self.time_status_label = QLabel("Время: --:-- | Осталось: --:--")
        self.time_status_label.setFont(font)
        grid.addWidget(self.time_status_label, row, 0, 1, 2)
        row += 1

        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        grid.addWidget(line2, row, 0, 1, 2)
        row += 1

        self.video_status_label = QLabel("")
        self.video_status_label.setFont(font)
        grid.addWidget(self.video_status_label, row, 0, 1, 2)
        row += 1

        self.fps_label = QLabel("")
        self.fps_label.setFont(font)
        grid.addWidget(self.fps_label, row, 0, 1, 2)
        row += 1

        self.video_pb = QProgressBar()
        self.video_pb.setMaximum(1)
        self.video_pb.setValue(0)
        self.video_pb.setTextVisible(False)
        grid.addWidget(self.video_pb, row, 0, 1, 2)
        row += 1

        self.delete_status_label = QLabel("")
        self.delete_status_label.setFont(font)
        self.delete_status_label.setStyleSheet("color: blue;")
        grid.addWidget(self.delete_status_label, row, 0, 1, 2)

        return page

    def build_settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        self.config_text = QTextEdit()
        self.config_text.setFont(QFont("Consolas", 10))
        layout.addWidget(self.config_text)

        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Сохранить")
        save_btn.clicked.connect(self.save_config)
        cancel_btn = QPushButton("Отмена")
        cancel_btn.clicked.connect(self.show_status_page)

        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        return page

    def build_log_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        self.log_text = QTextEdit()
        self.log_text.setFont(QFont("Consolas", 10))
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)

        bottom_layout = QHBoxLayout()

        self.auto_update_cb = QCheckBox("Автообновление")
        self.auto_update_cb.setChecked(True)
        self.auto_update_cb.stateChanged.connect(self.toggle_log_auto_update)
        bottom_layout.addWidget(self.auto_update_cb)

        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.show_status_page)
        bottom_layout.addWidget(close_btn)

        layout.addLayout(bottom_layout)

        self.log_timer = QTimer()
        self.log_timer.timeout.connect(self.update_log_display)

        return page

    def show_status_page(self):
        self.stacked.setCurrentIndex(0)
        self.update_status_display()
        try:
            with open('config.yaml', 'r', encoding='utf-8') as f:
                self.config_text.setPlainText(f.read())
        except Exception as e:
            self.config_text.setPlainText(f"# Ошибка чтения config.yaml: {e}")

    def show_settings_page(self):
        self.stacked.setCurrentIndex(1)
        try:
            with open('config.yaml', 'r', encoding='utf-8') as f:
                self.config_text.setPlainText(f.read())
        except Exception as e:
            self.config_text.setPlainText(f"# Ошибка чтения: {e}")

    def show_log_page(self):
        self.stacked.setCurrentIndex(2)
        self.update_log_display()

    def toggle_log_auto_update(self, state):
        if state == Qt.CheckState.Checked.value:
            self.log_timer.start(500)
        else:
            self.log_timer.stop()

    def update_log_display(self):
        log_path = get_current_log_path()
        if not os.path.exists(log_path):
            self.log_text.setPlainText("# Файл console.log не найден")
            return

        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.log_text.setPlainText(content)
            cursor = self.log_text.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.log_text.setTextCursor(cursor)
            self.log_text.ensureCursorVisible()
        except Exception as e:
            self.log_text.setPlainText(f"# Ошибка чтения console.log: {e}")

    def save_config(self):
        try:
            text = self.config_text.toPlainText()
            config_data = YAML().load(text)
            errors, parsed = validate_config(config_data)

            if errors:
                raise ValueError("\n".join(errors))

            config_data.update({
                'time_period_interval': parsed['interval'],
                'video_fps': parsed['fps'],
                'delete_frames_after_video': parsed['delete_frames']
            })

            self.config_manager.update(config_data, self.gui_queue)
            self.config_queue.put(config_data.copy())
            self.show_status_page()

        except Exception as e:
            logging.warning(f"Ошибка сохранения: {e}")
            self.config_manager._load()
            QMessageBox.critical(self, "Ошибка", f"Настройки не сохранены:\n\n{e}\n\nВосстановлена рабочая версия.")
            self.show_settings_page()

    def update_status_display(self):
        cfg = self.config_manager.config
        for key, lbl in self.status_labels.items():
            lbl.setText(str(cfg[key]))
        delete = "Да" if cfg['delete_frames_after_video'] else "Нет"
        self.fps_label.setText(f"Частота кадров в видео: {cfg['video_fps']} | Удаление кадров после конвертации: {delete}")
        self.update_video_time_label()

    def update_video_time_label(self):
        self.video_status_label.setText(f"Планируемое время запуска конвертации после: {self.config_manager['time_video']}")

    def update_captured_count(self):
        total = self.frame_capture.count_existing_frames()
        self.captured_count_label.setText(f"Сохранено кадров за текущие сутки: {total}")

    def reset_video_status(self):
        self.update_video_time_label()
        self.delete_status_label.setText("")
        self.video_pb.setValue(0)
        self.video_pb.setMinimum(0)
        self.video_pb.setMaximum(1)
        self.video_pb.setTextVisible(False)
        self.update_captured_count()

    def schedule_reset(self):
        QTimer.singleShot(10000, self.reset_video_status)

    def start_background(self):
        self.driver = BrowserDriver(self.config_manager)
        self.frame_capture = FrameCapture(self.config_manager, self.driver)
        self.encoder = VideoEncoder(self.config_manager, self.gui_queue)
        self.app = CaptureAppGUI(self.config_manager, self.driver, self.frame_capture, self.encoder, self.gui_queue, self.config_queue)
        threading.Thread(target=self.app.run, daemon=True).start()
        self.start_watchdog()

    def start_watchdog(self):
        observer = Observer()
        observer.schedule(ConfigWatcher(self.config_manager, self.config_queue), path='.', recursive=False)
        threading.Thread(target=observer.start, daemon=True).start()

    def setup_timers(self):
        self.queue_timer = QTimer()
        self.queue_timer.timeout.connect(self.process_queue)
        self.queue_timer.start(100)

    def process_queue(self):
        try:
            while True:
                msg = self.gui_queue.get_nowait()
                typ = msg[0]

                if typ == 'status':
                    self.captured_count_label.setText(f"Сохранено кадров за текущие сутки: {msg[1]}")
                    info = msg[2]
                    if info.startswith("stop:"):
                        self.last_frame_status_label.setText(f"Захват остановлен до {info[5:]}")
                        self.last_frame_status_label.setStyleSheet("color: red;")
                    else:
                        self.last_frame_status_label.setText(f"Последний кадр: {info or 'Нет'}")
                        self.last_frame_status_label.setStyleSheet("color: black")

                elif typ == 'video_start':
                    self.video_status_label.setText("Идет конвертация — 0/0")
                    self.video_pb.setMaximum(100)
                    self.video_pb.setValue(0)
                    self.video_pb.setTextVisible(True)

                elif typ == 'video_progress':
                    self.video_pb.setMaximum(msg[2])
                    self.video_pb.setValue(msg[1])
                    self.video_status_label.setText(f"Идет конвертация — {msg[1]}/{msg[2]}")

                elif typ == 'video_done':
                    self.video_status_label.setText(msg[1])
                    self.video_pb.setValue(self.video_pb.maximum())
                    self.video_pb.setTextVisible(True)

                elif typ == 'delete_done':
                    deleted = msg[1]
                    if deleted > 0:
                        self.delete_status_label.setText(f"Удалено кадров: {deleted}")
                    self.update_captured_count()
                    self.schedule_reset()

                elif typ == 'capture_progress':
                    self.capture_pb.setValue(int(msg[1]))
                    self.time_status_label.setText(f"Время: {msg[2]} | Осталось: {msg[3]}")
                    if msg[1] == 0 and msg[2] == "--:--":
                        self.capture_pb.setTextVisible(False)
                    else:
                        self.capture_pb.setTextVisible(True)

                elif typ == 'config_update':
                    self.update_video_time_label()
                    self.app.reset_video_trigger()

        except queue.Empty:
            pass

    def closeEvent(self, event):
        reply = QMessageBox.question(self, 'Выход', 'Остановить захват?', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            cleanup_processes()
            event.accept()
        else:
            event.ignore()

# ----------------------------------------------------------------------
# Основной контроллер
# ----------------------------------------------------------------------
class CaptureAppGUI:
    def __init__(self, config_manager, driver, frame_capture, encoder, gui_queue, config_queue):
        self.config_manager = config_manager
        self.driver = driver
        self.frame_capture = frame_capture
        self.encoder = encoder
        self.gui_queue = gui_queue
        self.config_queue = config_queue
        self.current_state = None
        self.last_video_date = None
        self.last_video_triggered = False
        self.last_log_date = None

    def reset_video_trigger(self):
        self.last_video_date = None
        self.last_video_triggered = False
        logging.info("Блокировка запуска конвертации сброшена")

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
        return next_start.strftime('%d.%m.%Y %H:%M')

    def _update_status(self):
        total = self.frame_capture.count_existing_frames()  # ИСПРАВЛЕНО: убрано "ures"
        last_file = os.path.basename(self.frame_capture.last_file) if self.frame_capture.last_file else 'Нет'
        self.gui_queue.put(('status', total, last_file))

    def _send_stopped(self):
        total = self.frame_capture.count_existing_frames()
        next_str = self._next_start_time()
        self.gui_queue.put(('status', total, f"stop:{next_str}"))

    def run(self):
        self._init_state()
        while True:
            try:
                updated = self.config_queue.get_nowait()
                self.config_manager.config = updated
                logging.info("Конфиг обновлён из GUI")
            except queue.Empty:
                pass

            now = datetime.now()
            today_str = now.strftime("%Y%m%d")
            cur_total = now.hour * 60 + now.minute
            st_total = self._get_minutes(self.config_manager['time_begin'])
            en_total = self._get_minutes(self.config_manager['time_end'])
            new_state = "work" if st_total <= cur_total < en_total else "off"

            # Ротация лога при смене даты
            if self.last_log_date != today_str:
                rotate_log_if_needed()
                self.last_log_date = today_str

            if self.current_state != new_state:
                self.current_state = new_state
                if new_state == "work":
                    logging.info(f"Старт захвата: {now.strftime('%Y-%m-%d %H:%M:%S')}")
                    self._update_status()
                else:
                    logging.info(f"Остановка. Следующий: {self._next_start_time()}")
                    self._send_stopped()
                    self.gui_queue.put(('capture_progress', 0, "--:--", "--:--"))

            if self.current_state == "work":
                if self.frame_capture.capture():
                    self._update_status()

                begin_min = st_total
                end_min = en_total
                current_min = cur_total

                if end_min > begin_min:
                    progress = (current_min - begin_min) / (end_min - begin_min) * 100
                    progress = max(0, min(100, progress))
                else:
                    progress = 0

                current_time = now.strftime("%H:%M")
                remaining_min = max(0, end_min - current_min)
                remaining_str = f"{remaining_min // 60:02d}:{remaining_min % 60:02d}"

                self.gui_queue.put(('capture_progress', progress, current_time, remaining_str))

            if self.current_state == "off":
                video_total = self._get_minutes(self.config_manager['time_video'])

                if self.last_video_date != today_str:
                    self.last_video_date = today_str
                    self.last_video_triggered = False
                    logging.info(f"Сброс триггера конвертации для новой даты: {today_str}")

                if cur_total >= video_total and not self.last_video_triggered:
                    if self.frame_capture.count_existing_frames() == 0:
                        logging.info(f"Нет кадров за {today_str} — конвертация пропущена")
                        self.last_video_triggered = True
                        continue

                    logging.info(f"Запуск конвертации за {today_str} в {self.config_manager['time_video']}")
                    self.gui_queue.put(('video_start',))
                    self.encoder.encode(today_str)
                    self.last_video_triggered = True

            time.sleep(self.config_manager['time_period_interval'])

    def _init_state(self):
        now = datetime.now()
        today_str = now.strftime("%Y%m%d")
        self.last_video_date = today_str
        self.last_video_triggered = False
        self.last_log_date = today_str

        cur_total = now.hour * 60 + now.minute
        st_total = self._get_minutes(self.config_manager['time_begin'])
        en_total = self._get_minutes(self.config_manager['time_end'])
        self.current_state = "work" if st_total <= cur_total < en_total else "off"

        self._update_status()

        if self.current_state == "work":
            begin_min = st_total
            end_min = en_total
            current_min = cur_total
            progress = 0
            if end_min > begin_min:
                progress = (current_min - begin_min) / (end_min - begin_min) * 100
                progress = max(0, min(100, progress))
            current_time = now.strftime("%H:%M")
            remaining_min = max(0, end_min - current_min)
            remaining_str = f"{remaining_min // 60:02d}:{remaining_min % 60:02d}"
            self.gui_queue.put(('capture_progress', progress, current_time, remaining_str))
        else:
            self._send_stopped()
            self.gui_queue.put(('capture_progress', 0, "--:--", "--:--"))

# ----------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------
if __name__ == "__main__":
    cleanup_processes()
    app = QApplication(sys.argv)
    app.setStyle(QStyleFactory.create('Fusion'))

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Highlight, QColor(0, 120, 215))
    app.setPalette(palette)

    window = CaptureGUI()
    window.show()
    sys.exit(app.exec())