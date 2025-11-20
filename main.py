import sys

from PyQt6.QtWidgets import (
    QApplication,  QMessageBox, QStyleFactory
)
from PyQt6.QtGui import QPalette, QColor

from watchdog.observers import Observer

from main_ui import CaptureGUI

from main_classes import cleanup_processes


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