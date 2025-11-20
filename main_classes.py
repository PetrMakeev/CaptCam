import os
import time
from datetime import datetime, timedelta
import logging
import queue
import cv2
import glob
import sys
from PIL import Image
from logging.handlers import RotatingFileHandler
from ruamel.yaml import YAML


from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from watchdog.events import FileSystemEventHandler

from main_function import rotate_log_if_needed, cleanup_processes, is_image_black




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
        total = self.frame_capture.count_existing_frames()
        # ИЗМЕНЕНО: передаём полный путь, а не только basename
        last_file_full = self.frame_capture.last_file if self.frame_capture.last_file else None
        self.gui_queue.put(('status', total, last_file_full))

    def _send_stopped(self):
        total = self.frame_capture.count_existing_frames()
        next_str = self._next_start_time()
        # Передаём строку с префиксом "stop:", чтобы GUI понимала, что это не файл
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
# Видеокодер (обновлённый)
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

        # Подготовка — сразу видно, что процесс пошёл
        self.gui_queue.put(('video_prepare',))

        h, w, _ = cv2.imread(frames[0]).shape
        writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*'mp4v'), self.config['video_fps'], (w, h))

        total = len(frames)

        # Реальный старт конвертации
        self.gui_queue.put(('video_start', total))

        for i, f in enumerate(frames):
            writer.write(cv2.imread(f))
            self.gui_queue.put(('video_progress', i + 1, total))

        writer.release()

        summary = f"Конвертация завершена: {total} кадров → {os.path.basename(video_path)}"
        self.gui_queue.put(('video_done', summary))
        logging.info(summary)

        time.sleep(5)  # пауза 5 секунд перед удалением

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