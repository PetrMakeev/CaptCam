import os
import sys

import queue
import threading
import logging

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QProgressBar, QTextEdit,
    QFrame, QStackedWidget, QMessageBox, QStyleFactory,
    QCheckBox
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QFont, QPalette, QColor

from ruamel.yaml import YAML
from watchdog.observers import Observer

from main_classes import (FrameCapture, VideoEncoder, 
                          ConfigManager, CaptureAppGUI, 
                          BrowserDriver, ConfigWatcher,
                          cleanup_processes)

from main_function import get_current_log_path, validate_config, resource_path

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

        self.video_result_label = QLabel("")
        self.video_result_label.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self.video_result_label.setWordWrap(True)
        self.video_result_label.setStyleSheet("color: #0066cc;")
        grid.addWidget(self.video_result_label, row, 0, 1, 2)

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

    def update_captured_count(self):
        total = self.frame_capture.count_existing_frames()
        self.captured_count_label.setText(f"Сохранено кадров за текущие сутки: {total}")

    def reset_video_status(self):
        self.video_result_label.setText("")
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

                elif typ == 'video_prepare':
                    self.video_result_label.setText("Подготовка к конвертации")
                    self.video_pb.setMaximum(100)
                    self.video_pb.setValue(2)
                    self.video_pb.setTextVisible(True)

                elif typ == 'video_start':
                    total = msg[1]
                    self.video_result_label.setText(f"Идет конвертация — 0/{total}")
                    self.video_pb.setMaximum(total)
                    self.video_pb.setValue(0)

                elif typ == 'video_progress':
                    current, total = msg[1], msg[2]
                    self.video_result_label.setText(f"Идет конвертация — {current}/{total}")
                    self.video_pb.setValue(current)

                elif typ == 'video_done':
                    summary = msg[1]
                    self.video_result_label.setText(summary)
                    self.video_pb.setValue(self.video_pb.maximum())

                elif typ == 'delete_done':
                    deleted = msg[1]
                    if deleted > 0:
                        self.video_result_label.setText(f"Удалено кадров: {deleted}")
                    else:
                        self.video_result_label.setText("")
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
