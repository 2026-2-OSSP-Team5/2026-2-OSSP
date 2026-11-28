"""Tray access and window activation for a desktop app running in the background."""

import hashlib
import logging
from pathlib import Path

from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

LOGGER = logging.getLogger("slideguard.desktop")


def _server_name(database_path: Path) -> str:
    identity = str(database_path.resolve()).casefold().encode("utf-8")
    return "slideguard-" + hashlib.sha256(identity).hexdigest()[:24]


def activate_running_instance(database_path: Path) -> bool:
    socket = QLocalSocket()
    socket.connectToServer(_server_name(database_path))
    if not socket.waitForConnected(1000):
        return False
    socket.disconnectFromServer()
    return True


def listen_for_activation(app: QApplication, database_path: Path, show_window) -> QLocalServer:
    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)

    def activate():
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            socket.disconnected.connect(socket.deleteLater)
            socket.disconnectFromServer()
        show_window()

    server.newConnection.connect(activate)
    if not server.listen(_server_name(database_path)):
        LOGGER.warning("ACTIVATION_LISTENER_FAILED | %s", server.errorString())
    return server


def create_tray(app: QApplication, window, controller) -> QSystemTrayIcon | None:
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    icon = app.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogInfoView)
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("SlideGuard · 추적 폴더의 PPTX를 열면 방패가 표시됩니다")
    menu = QMenu(window)
    menu.addAction("작업 폴더·이력 열기", window._raise_window)
    menu.addAction("설정", lambda: controller.show_settings(parent=window))
    menu.addSeparator()
    menu.addAction("SlideGuard 종료", controller.request_exit)
    tray.setContextMenu(menu)

    def activated(reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            window._raise_window()

    tray.activated.connect(activated)
    app.aboutToQuit.connect(tray.hide)
    tray.show()
    return tray
