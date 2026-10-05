from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout

from core.models import CameraConfig, FrameData, CameraStatus
from ui.camera_widget import CameraWidget
from WidgetCameraControl import WidgetCameraControl
import qtawesome as qta

class DetachedCameraWindow(QDialog):
    """
    분리된 카메라 관제 및 PTZ 제어 창입니다.
    MainWindow로부터 FrameData와 상태를 전달받아 화면을 표시하며,
    하단 PTZ 제어 패널을 통해 ONVIF 실시간 카메라 제어를 수행합니다.
    """
    def __init__(self, config: CameraConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"카메라: {config.name}")
        # [추가] 창의 프레임(제목 표시줄, 닫기 버튼 등)을 제거하여 완전한 전체화면 효과 부여
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)

        self.config = config
        self.camera_id = config.camera_id

        self.camera_control = WidgetCameraControl(config, self)
        self.camera_control.camera_view.set_high_resolution_view(True)
        # self.camera_widget = CameraWidget(config, self)
        # self.camera_widget.new_window_btn.setIcon(qta.icon('mdi6.close-box-outline', color='#f3f4f6'))
        # 20260601 swjang 새창에서 영상 화면에 더블 클릭했을때 창닫기 수행
        # 닫기 버튼 연결 (CameraWidget의 새 창 버튼 아이콘 변경 및 창 닫기)
        self.camera_control.camera_view.new_window_btn.setToolTip("창 닫기")
        self.camera_control.camera_view.new_window_btn.setIcon(qta.icon('mdi6.close-box-outline', color='#f3f4f6'))
        self.camera_control.camera_view.new_window_btn.clicked.connect(lambda: self.close())
        self.camera_control.camera_view.double_clicked.connect(lambda: self.close())

        # 레이아웃 설정
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.camera_control)
        self.setLayout(layout)

    @property
    def camera_widget(self):
        """기존 MainWindow 코드 호환성을 위한 camera_view 반환 속성"""
        return self.camera_control.camera_view

    def update_frame(self, frame_data: FrameData):
        if frame_data.camera_id == self.camera_id:
            self.camera_control.update_frame(frame_data)

            display_frame = frame_data.high_res_frame
            if display_frame is None:
                display_frame = frame_data.frame

            if display_frame is not None:
                width, height = frame_data.high_res_frame_size
                self.camera_control.update_resolution(self.camera_id, width, height)

    def update_status(self, status: CameraStatus):
        """MainWindow로부터 상태 정보를 받아 CameraWidget을 업데이트합니다."""
        self.camera_control.update_status(self.camera_id, status)

    def keyPressEvent(self, event):
        """Esc 키를 누르면 창 닫기"""
        if event.key() == Qt.Key_Escape:
            self.close()
        super().keyPressEvent(event)