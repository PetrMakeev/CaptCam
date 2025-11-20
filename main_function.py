from os import path as os_path, rename as os_rename
import logging
import sys
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
import psutil

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
    return os_path.join(LOG_DIR, f"{LOG_BASE}{LOG_EXT}")

def get_dated_log_path(date_str):
    return os_path.join(LOG_DIR, f"{LOG_BASE}_{date_str}{LOG_EXT}")

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

replace_log_handler()
logging.info("=== GUI ПРИЛОЖЕНИЕ ЗАПУЩЕНО ===")

def rotate_log_if_needed():
    current_log = get_current_log_path()
    if not os_path.exists(current_log):
        return

    yesterday = datetime.now() - timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y%m%d")
    dated_log = get_dated_log_path(yesterday_str)

    if os_path.exists(dated_log):
        return

    try:
        root = logging.getLogger()
        for h in root.handlers[:]:
            h.close()
            root.removeHandler(h)

        os_rename(current_log, dated_log)
        logging.info(f"Лог переименован: {current_log} → {dated_log}")
        replace_log_handler()
    except Exception as e:
        try:
            replace_log_handler()
        except:
            pass
        logging.warning(f"Не удалось ротировать лог: {e}")

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
# Утилиты
# ----------------------------------------------------------------------
   
def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os_path.abspath(".")
    return os_path.join(base_path, relative_path)    


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