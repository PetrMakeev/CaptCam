import os
import time
from datetime import datetime, timedelta
import logging
import queue
import cv2
import glob
import sys
import io
import numpy as np
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
        last_file_full = self.frame_capture.last_file if self.frame_capture.last_file else None
        self.gui_queue.put(('status', total, last_file_full))

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

            time.sleep(max(0.01, float(self.config_manager['time_period_interval'])))

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
# Драйвер, FrameCapture, VideoEncoder — без изменений, кроме FrameCapture
# ----------------------------------------------------------------------
class BrowserDriver:
    def __init__(self, config):
        self.config = config
        self.driver = None
        self.iframe_element = None
        self._setup_driver()
        self._init_page()
    
    @property
    def switch_to(self):
        return self.driver.switch_to        

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
# ОПТИМАЛЬНЫЙ ЗАХВАТ: JPG напрямую, без временных файлов и предупреждений
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
        return len([f for f in os.listdir(folder) if f.startswith("capt-") and f.endswith(".jpg")])

    def capture(self):
        now = datetime.now()
        date_str = now.strftime("%Y%m%d")
        time_str = now.strftime("%H-%M-%S")
        filename = f"capt-{date_str}_{time_str}.jpg"
        folder = os.path.join("capture", date_str)
        os.makedirs(folder, exist_ok=True)
        file_path = os.path.join(folder, filename)

        try:
            # Проверка размера iframe
            size = self.driver.get_iframe_size()
            if not size or size['width'] < 132:
                logging.warning("iframe слишком узкий или пустой → перезагрузка")
                self.driver.reload_via_url()
                time.sleep(0.2)
                return False

            # === КЛЮЧЕВОЙ ТРЮК: используем screenshot_as_png + сохраняем через PIL как JPG ===
            self.driver.switch_to.frame(self.driver.iframe_element)
            try:
                video = WebDriverWait(self.driver, 4).until(
                    EC.presence_of_element_located((By.TAG_NAME, "video"))
                )
                png_data = video.screenshot_as_png
            except:
                self.driver.switch_to.default_content()
                png_data = self.driver.iframe_element.screenshot_as_png
            finally:
                self.driver.switch_to.default_content()

            # Открываем PNG из памяти
            img = Image.open(io.BytesIO(png_data))

            # Конвертируем в RGB (убираем альфу)
            if img.mode != "RGB":
                img = img.convert("RGB")

            w, h = img.size
            if w < 132:
                logging.warning(f"Узкий кадр w={w} → перезагрузка")
                self.driver.reload_via_url()
                time.sleep(0.2)
                return False

            if is_image_black(img):
                logging.warning("Чёрный кадр → перезагрузка")
                self.driver.reload_via_url()
                time.sleep(0.2)
                return False

            # Кроп боковых панелей
            cropped = img.crop((66, 0, w-66, h))

            # Качество из конфига
            quality = int(self.config.config.get('image_quality', 92))
            quality = max(75, min(100, quality))

            # Сохраняем как JPG
            cropped.save(file_path, "JPEG", quality=quality, optimize=True, progressive=True)

            # Проверка размера
            if os.path.getsize(file_path) < 70 * 1024:
                os.remove(file_path)
                logging.warning("JPG слишком маленький → перезагрузка")
                self.driver.reload_via_url()
                time.sleep(0.2)
                return False

            self.last_file = file_path
            return True

        except Exception as e:
            logging.error(f"Ошибка захвата кадра: {e}")
            try: os.remove(file_path)
            except: pass
            self.driver.reload_via_url()
            time.sleep(0.2)
            return False


# ----------------------------------------------------------------------
# Видеокодер — теперь ищет .jpg
# ----------------------------------------------------------------------
class VideoEncoder:
    def __init__(self, config, gui_queue, frame_capture):
        self.config = config
        self.gui_queue = gui_queue
        self.frame_capture = frame_capture  # нужен для доступа к папке

    def _get_video_path(self, date_str):
        folder = os.path.join("capture", date_str)
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"video-{date_str}.mp4")

    def encode(self, date_str):
        """Создаёт видео из всех JPG-кадров за указанную дату"""
        folder = os.path.join("capture", date_str)
        pattern = os.path.join(folder, "capt-*.jpg")
        frames = sorted(glob.glob(pattern))

        if not frames:
            logging.info(f"Нет JPG-кадров для конвертации за {date_str}")
            self.gui_queue.put(('video_done', "Нет кадров для видео"))
            return

        # Читаем первый кадр для размеров
        first_frame = cv2.imread(frames[0])
        if first_frame is None:
            logging.error("Не удалось прочитать первый кадр")
            return
        h, w = first_frame.shape[:2]

        video_path = self._get_video_path(date_str)
        writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*'mp4v'),
                                 self.config['video_fps'], (w, h))

        total = len(frames)
        self.gui_queue.put(('video_prepare',))
        self.gui_queue.put(('video_start', total))

        for i, jpg_path in enumerate(frames):
            frame = cv2.imread(jpg_path)
            if frame is not None:
                writer.write(frame)
            # Обновляем прогресс реже — чтобы GUI не тормозил
            if (i + 1) % 10 == 0 or i == total - 1:
                self.gui_queue.put(('video_progress', i + 1, total))

        writer.release()

        summary = f"Видео создано: video-{date_str}.mp4 ({total} кадров)"
        self.gui_queue.put(('video_done', summary))
        logging.info(summary)

        # Удаление кадров после конвертации
        if self.config.get('delete_frames_after_video', False):
            deleted = 0
            for p in frames:
                try:
                    os.remove(p)
                    deleted += 1
                except:
                    pass
            logging.info(f"Удалено {deleted}/{total} JPG-кадров")
            self.gui_queue.put(('delete_done', deleted))


# ----------------------------------------------------------------------
# Конфиг — добавлено image_quality
# ----------------------------------------------------------------------
class ConfigManager:
    DEFAULT_CONFIG = {
        'adress_url': 'http://maps.ufanet.ru/orenburg#1759214666SGR59',
        'time_begin': '06:00',
        'time_end': '19:00',
        'time_period_interval': 0.5,
        'time_video': '19:05',
        'video_fps': 60,
        'delete_frames_after_video': True,
        'image_quality': 92
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
            self.config = {**self.DEFAULT_CONFIG, **loaded}

            # Валидация image_quality
            q = self.config.get('image_quality', 92)
            self.config['image_quality'] = max(75, min(100, int(q)))

            # Исправление времени, если нужно
            begin_min = self._to_minutes(self.config['time_begin'])
            end_min = self._to_minutes(self.config['time_end'])
            if end_min <= begin_min:
                self.config['time_end'] = self._from_minutes(begin_min + 60)
            video_min = self._to_minutes(self.config['time_video'])
            if video_min <= end_min:
                self.config['time_video'] = self._from_minutes(end_min + 5)

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
        self.config.update(new_config)
        self._save()
        logging.info(f"Конфиг обновлён: {new_config}")

    # ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←
    # ВАЖНЫЙ МЕТОД:
    def get(self, key, default=None):
        return self.config.get(key, default)
    # ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←

    def __getitem__(self, key):
        return self.config[key]
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