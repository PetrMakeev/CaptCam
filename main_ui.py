import os
import sys

import queue
import threading
import logging
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QProgressBar, QTextEdit,
    QFrame, QStackedWidget, QMessageBox,
    QCheckBox
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QFont, QPixmap

from ruamel.yaml import YAML
from watchdog.observers import Observer

from main_classes import (
    FrameCapture, VideoEncoder, ConfigManager, CaptureAppGUI,
    BrowserDriver, ConfigWatcher, cleanup_processes
)

from main_function import get_current_log_path, validate_config, resource_path


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
        self.last_frame_path = None
        self.show_preview = False

        # Состояния конвертации и удаления
        self.video_total_frames = 0
        self.video_processed_frames = 0
        self.is_encoding_now = False
        self.deleting_in_progress = False
        self.total_to_delete = 0
        self.deleted_count = 0
        self.current_video_date = datetime.now().strftime("%Y%m%d")

        self.init_ui()
        self.start_background()
        self.setup_timers()

        QTimer.singleShot(500, self.update_status_display)
        QTimer.singleShot(500, self.update_video_status_display)

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

    # ============================================================
    # Страница статуса + превью
    # ============================================================
    def build_status_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        grid = QGridLayout()
        layout.addLayout(grid)

        font = QFont("Consolas", 10)
        row = 0
        BTN_WIDTH = 120

        # Кнопки Настройки / Лог
        btn_container = QHBoxLayout()
        btn_container.addStretch()
        btn_settings = QPushButton("Настройки")
        btn_settings.setFixedWidth(BTN_WIDTH)
        btn_settings.clicked.connect(self.show_settings_page)
        btn_log = QPushButton("Лог")
        btn_log.setFixedWidth(BTN_WIDTH)
        btn_log.clicked.connect(self.show_log_page)
        btn_container.addWidget(btn_settings)
        btn_container.addWidget(btn_log)
        grid.addLayout(btn_container, row, 0, 1, 2)
        row += 1

        # Параметры конфига
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

        self.video_plan_label = QLabel("")
        self.video_plan_label.setFont(font)
        grid.addWidget(self.video_plan_label, row, 0, 1, 2)
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

        # Постоянные строки статуса конвертации
        self.video_frames_label = QLabel("Конвертация кадров: всего — —, обработано — —")
        self.video_frames_label.setFont(QFont("Consolas", 10))
        grid.addWidget(self.video_frames_label, row, 0, 1, 2)
        row += 1

        self.video_filename_label = QLabel("Будет создан видеофайл — video-XXXXXXXX.mp4")
        self.video_filename_label.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        grid.addWidget(self.video_filename_label, row, 0, 1, 2)
        row += 1

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(separator)

        # Чекбокс превью
        preview_ctrl = QHBoxLayout()
        preview_ctrl.addStretch()
        self.preview_checkbox = QCheckBox("Показывать последний кадр")
        self.preview_checkbox.setChecked(False)
        self.preview_checkbox.stateChanged.connect(self.toggle_preview)
        preview_ctrl.addWidget(self.preview_checkbox)
        preview_ctrl.addStretch()
        layout.addLayout(preview_ctrl)

        # Превью
        self.preview_label = QLabel()
        self.preview_label.setMinimumHeight(220)
        self.preview_label.setStyleSheet("background-color: #f0f0f0; border: 1px solid #cccccc;")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setText("Последний кадр появится здесь")
        layout.addWidget(self.preview_label)
        self.preview_label.hide()

        #layout.addStretch()
        return page

    # ============================================================
    # Страница настроек
    # ============================================================
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

    # ============================================================
    # Страница лога
    # ============================================================
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

    # ============================================================
    # Вспомогательные функции
    # ============================================================
    def _get_video_path(self, date_str=None):
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")
        return os.path.join("capture", date_str, f"video-{date_str}.mp4")

    def update_video_status_display(self):
        today = datetime.now().strftime("%Y%m%d")
        video_name = f"video-{today}.mp4"
        video_full_path = self._get_video_path(today)

        # Сброс при смене суток
        if today != self.current_video_date:
            self.current_video_date = today
            self.video_total_frames = 0
            self.video_processed_frames = 0
            self.is_encoding_now = False
            self.deleting_in_progress = False
            self.total_to_delete = 0
            self.deleted_count = 0

        # Приоритет состояний:
        if self.deleting_in_progress:
            text = f"Удаляем кадры: {self.total_to_delete} всего / {self.deleted_count} удалено"
            style = "color: #0066cc; font-weight: bold;"  # синий
        elif self.is_encoding_now:
            text = f"Генерируется видеофайл — {video_name}"
            style = "color: orange; font-weight: bold;"
        elif os.path.exists(video_full_path):
            text = f"Создан видеофайл — {video_name}"
            style = "color: green; font-weight: bold;"
        else:
            text = f"Будет создан видеофайл — {video_name}"
            style = "color: black;"

        self.video_filename_label.setText(text)
        self.video_filename_label.setStyleSheet(style)

        # Строка с количеством кадров
        if self.is_encoding_now:
            self.video_frames_label.setText(
                f"Конвертация кадров: всего — {self.video_total_frames}, обработано — {self.video_processed_frames}"
            )
        else:
            count = self.frame_capture.count_existing_frames() if hasattr(self, 'frame_capture') else 0
            total_str = str(count) if count > 0 else "—"
            self.video_frames_label.setText(
                f"Конвертация кадров: всего — {total_str}, обработано — —"
            )

    # ============================================================
    # Остальные методы
    # ============================================================
    def toggle_preview(self, state):
        show = (state == Qt.CheckState.Checked.value)
        self.show_preview = show
        if show:
            self.preview_label.show()
            QTimer.singleShot(0, lambda: self.setFixedSize(560, 606))
        else:
            self.preview_label.hide()
            QTimer.singleShot(0, lambda: self.setFixedSize(560, 380))
        self.update_preview()

    def update_preview(self):
        if not self.show_preview or not self.last_frame_path or not os.path.exists(self.last_frame_path):
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Последний кадр появится здесь" if self.show_preview else "")
            return
        pixmap = QPixmap(self.last_frame_path)
        if pixmap.isNull():
            self.preview_label.setText("Ошибка загрузки изображения")
            return
        scaled = pixmap.scaled(
            self.preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        self.preview_label.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(50, self.update_preview)

    def show_status_page(self):
        self.stacked.setCurrentIndex(0)
        self.update_status_display()
        self.update_video_status_display()
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
        if self.auto_update_cb.isChecked():
            self.log_timer.start(500)

    def toggle_log_auto_update(self, state):
        if state == Qt.CheckState.Checked.value:
            self.log_timer.start(500)
        else:
            self.log_timer.stop()

    def update_log_display(self):
        log_path = get_current_log_path()
        if not os.path.exists(log_path):
            self.log_text.setPlainText("# Файл capture.log не найден")
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
            self.log_text.setPlainText(f"# Ошибка чтения capture.log: {e}")

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
        self.video_plan_label.setText(f"Планируемое время запуска конвертации после: {cfg['time_video']}")
        self.update_video_status_display()

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

        self.video_status_timer = QTimer()
        self.video_status_timer.timeout.connect(self.update_video_status_display)
        self.video_status_timer.start(5000)

    def process_queue(self):
        try:
            while True:
                msg = self.gui_queue.get_nowait()
                typ = msg[0]

                if typ == 'status':
                    total = msg[1]
                    info = msg[2]
                    self.captured_count_label.setText(f"Сохранено кадров за текущие сутки: {total}")

                    if isinstance(info, str) and info.startswith("stop:"):
                        self.last_frame_status_label.setText(f"Захват остановлен до {info[5:]}")
                        self.last_frame_status_label.setStyleSheet("color: red;")
                        self.last_frame_path = None
                        self.update_preview()
                    else:
                        if info and os.path.exists(info):
                            self.last_frame_path = info
                            self.last_frame_status_label.setText(f"Последний кадр: {os.path.basename(info)}")
                            self.last_frame_status_label.setStyleSheet("color: black;")
                            self.update_preview()
                        else:
                            self.last_frame_status_label.setText("Последний кадр: Нет")
                            self.last_frame_status_label.setStyleSheet("color: black;")
                            self.last_frame_path = None
                        self.update_preview()

                    self.update_video_status_display()

                elif typ == 'video_prepare':
                    self.is_encoding_now = True
                    self.deleting_in_progress = False
                    self.update_video_status_display()

                elif typ == 'video_start':
                    self.video_total_frames = msg[1]
                    self.video_processed_frames = 0
                    self.is_encoding_now = True
                    self.deleting_in_progress = False
                    self.video_pb.setMaximum(self.video_total_frames)
                    self.video_pb.setValue(0)
                    self.video_pb.setTextVisible(True)
                    self.update_video_status_display()

                elif typ == 'video_progress':
                    self.video_processed_frames = msg[1]
                    self.video_pb.setValue(self.video_processed_frames)
                    self.update_video_status_display()

                elif typ == 'video_done':
                    self.is_encoding_now = False
                    self.video_pb.setMaximum(1)
                    self.video_pb.setValue(0)
                    self.video_pb.setTextVisible(False)

                    if self.config_manager['delete_frames_after_video']:
                        self.deleting_in_progress = True
                        self.total_to_delete = self.video_total_frames
                        self.deleted_count = 0
                    else:
                        self.deleting_in_progress = False

                    self.update_video_status_display()

                elif typ == 'delete_done':
                    deleted = msg[1]
                    self.deleted_count = deleted
                    self.deleting_in_progress = False
                    self.update_video_status_display()

                elif typ == 'capture_progress':
                    self.capture_pb.setValue(int(msg[1]))
                    self.time_status_label.setText(f"Время: {msg[2]} | Осталось: {msg[3]}")
                    if msg[1] == 0 and msg[2] == "--:--":
                        self.capture_pb.setTextVisible(False)
                    else:
                        self.capture_pb.setTextVisible(True)

                elif typ == 'config_update':
                    self.update_status_display()
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