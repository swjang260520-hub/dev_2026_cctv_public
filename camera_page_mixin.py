"""
ui/camera_page_mixin.py

카메라 관리 영역 — MainWindow 에서 분리한 Mixin.
(2026 최종본 2026_cctv_svn 기준으로 이식)

[설계 의도]
- WidgetMain.py 에서 카메라 관련 45개 메서드(약 720줄)를 이 파일로 이동.
- Mixin 은 자체 __init__ 을 갖지 않는다. 필요한 모든 상태
  (self.camera_manager, self.camera_widgets, self.camera_workers ...)는
  MainWindow.__init__ 에서 생성되며, Mixin 은 self 를 통해 접근한다.
- 따라서 동작은 100% 동일하고, WidgetMain.py 의 크기만 줄어든다.

[상속 방법]
    class MainWindow(QMainWindow, Ui_MainWindow, CameraPageMixin):
        ...

[신규 기능: 카메라 그룹]
- Col.GROUP 컬럼 추가 (그룹명 표시)
- 그룹 필터 콤보 (테이블 + 그리드 레이아웃 동시 필터링)
- 그룹 단위 일괄 시작/정지
- 그룹명 변경(일괄 갱신)

'.ui' 파일은 수정하지 않는다. _setup_camera_table() 이 런타임에 컬럼을
정의하고, 그룹 필터 콤보는 코드로 레이아웃에 삽입한다.
"""

import math
from enum import IntEnum
from datetime import datetime
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Slot, QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QMessageBox, QPushButton, QTableWidgetItem, QWidget,
)
import qtawesome as qta
from loguru import logger

from core.models import CameraConfig, CameraStatus, FrameData, UNGROUPED
from core.workers import CameraWorker
from ui.camera_widget import CameraWidget
from WidgetDetachedCamera import DetachedCameraWindow
from WidgetCameraDialog import CameraDialog


# ====================================================================== #
# 테이블 컬럼 인덱스 정의 (매직 넘버 제거)
# ---------------------------------------------------------------------- #
# [수정] GROUP 컬럼이 추가되어 이후 인덱스가 1씩 밀렸다.
#        Col 을 통해서만 접근하므로 나머지 코드는 자동으로 따라온다.
# ====================================================================== #
class Col(IntEnum):
    CHECK   = 0
    MOVE    = 1
    STATUS  = 2
    GROUP   = 3   # [신규]
    NAME    = 4
    DISPLAY = 5
    DETECT  = 6
    MANAGE  = 7
    ADDRESS = 8

    @classmethod
    def count(cls) -> int:
        return len(cls)


#: 그룹 필터 콤보에서 "전체 그룹"을 나타내는 data 값
FILTER_ALL = None


class CameraPageMixin:
    """
    카메라 테이블 · 그리드 · 워커 수명주기 · 그룹 관리.

    이 클래스는 단독으로 인스턴스화되지 않는다.
    MainWindow 와 함께 상속되어 self 를 공유하는 용도다.
    """

    # ================================================================== #
    # 1. 초기화 (__init__ 에서 호출)
    # ================================================================== #

    def _setup_camera_table(self):
        """
        카메라 테이블 컬럼 구성.

        [수정] 그룹 컬럼 추가로 컬럼 수 8 → 9.
               '.ui' 파일은 수정 불필요 (여기서 런타임에 설정하므로).
        """
        self.camera_table.setColumnCount(Col.count())
        self.camera_table.setHorizontalHeaderLabels(
            ["✓", "이동", "상태", "그룹", "카메라명", "영상", "탐지", "관리", "주소"]
        )
        self.camera_table.setAlternatingRowColors(True)
        self.camera_table.setSelectionBehavior(QAbstractItemView.SelectRows)

        header = self.camera_table.horizontalHeader()
        header.setSectionsClickable(True)

        if check_header := self.camera_table.horizontalHeaderItem(Col.CHECK):
            check_header.setToolTip("전체 선택 / 전체 해제")
        if group_header := self.camera_table.horizontalHeaderItem(Col.GROUP):
            group_header.setToolTip("카메라 그룹")

        # 헤더 폰트 굵게 설정
        font = header.font()
        font.setBold(True)
        header.setFont(font)

        # 모든 컬럼을 사용자가 조절할 수 있도록 Interactive 모드로 설정
        header.setSectionResizeMode(QHeaderView.Interactive)

        # 초기 컬럼 너비 설정 (사용자가 조절 가능)
        self.camera_table.setColumnWidth(Col.CHECK, 25)
        self.camera_table.setColumnWidth(Col.MOVE, 55)
        self.camera_table.setColumnWidth(Col.STATUS, 35)
        self.camera_table.setColumnWidth(Col.GROUP, 80)   # [신규]
        self.camera_table.setColumnWidth(Col.NAME, 120)
        self.camera_table.setColumnWidth(Col.DISPLAY, 35)
        self.camera_table.setColumnWidth(Col.DETECT, 35)
        self.camera_table.setColumnWidth(Col.MANAGE, 90)
        self.camera_table.setColumnWidth(Col.ADDRESS, 250)

    # ------------------------------------------------------------------ #
    # [신규] 그룹 필터 UI
    # ------------------------------------------------------------------ #

    def _setup_group_filter(self):
        """
        그룹 필터 콤보박스와 그룹 일괄 제어 버튼을 코드로 배치한다.

        '.ui' 를 건드리지 않기 위해 gridLayout_control 에 동적으로 삽입한다.

        ------------------------------------------------------------------
        [레이아웃 주의 — 컬럼을 늘리면 안 된다]
        '.ui' 의 groupBox_control 은 3컬럼 그리드이고, 1행의 grid_combo 가
        colspan=2 로 병합되어 있다.

            row 0: [영상 재생 :] [전체 시작] [전체 정지]
            row 1: [뷰 레이아웃:] [ ── grid_combo (colspan 2) ── ]

        병합 셀의 폭은 "포함된 컬럼 폭의 합 + spacing" 과 정확히 일치해야 하므로
        col1 == col2 라는 제약이 생긴다. 여기에 4번째 컬럼을 추가하면
        (그룹 라벨/콤보/버튼/버튼) 이 제약이 풀리고, Qt 가 병합 셀의 남는 폭을
        마지막 컬럼으로 몰아주면서 col0 가 급격히 줄어든다.
        결과적으로 우측 정렬된 라벨들이 오른쪽 끝에 붙어 '상단 행 전체가
        왼쪽으로 밀리는' 현상이 발생한다. (실측: col0 115px → 65px)

        → 그래서 그룹 행은 기존 3컬럼 구조를 그대로 유지한다.
          콤보 + 버튼 2개를 QHBoxLayout 으로 묶어 colspan=2 로 배치하고,
          버튼은 텍스트 대신 아이콘(28x28)으로 두어 가로 폭을 아낀다.
          (아이콘만으로는 의미 전달이 약하므로 툴팁을 반드시 붙인다)
        ------------------------------------------------------------------
        """
        # --- 그룹 필터 콤보 ---
        self.group_filter = QComboBox(self)
        self.group_filter.setMinimumWidth(110)
        self.group_filter.setToolTip("표시할 카메라 그룹을 선택합니다")
        self.group_filter.currentIndexChanged.connect(self._on_group_filter_changed)

        # --- 그룹 일괄 제어 버튼 (아이콘 전용) ---
        self.group_start_btn = self._create_group_action_button(
            "mdi6.play", "그룹 전체 시작 (현재 선택된 그룹의 카메라를 모두 시작)",
            self._on_start_group,
        )
        self.group_stop_btn = self._create_group_action_button(
            "mdi6.stop", "그룹 전체 정지 (현재 선택된 그룹의 카메라를 모두 정지)",
            self._on_stop_group,
        )

        # 기존 컨트롤 레이아웃에 한 줄 추가 (없으면 조용히 건너뛴다)
        layout = getattr(self, "gridLayout_control", None)
        if layout is None:
            logger.warning("gridLayout_control 을 찾을 수 없어 그룹 UI 를 배치하지 못했습니다.")
            self._refresh_group_filter()
            return

        try:
            row = layout.rowCount()

            label = QLabel("그룹")
            # 위 행들의 라벨과 정렬을 통일한다
            # ('.ui' 의 label_2, label_3 과 동일한 AlignRight|AlignVCenter)
            label.setAlignment(Qt.AlignRight | Qt.AlignTrailing | Qt.AlignVCenter)
            layout.addWidget(label, row, 0)

            # 콤보와 버튼을 하나의 가로 레이아웃으로 묶는다.
            # 컬럼 수를 늘리지 않도록 나머지 컬럼 전체를 병합(colspan)한다.
            hbox = QHBoxLayout()
            hbox.setContentsMargins(0, 0, 0, 0)
            hbox.addWidget(self.group_filter, 1)      # 콤보가 남는 폭을 가져감
            hbox.addWidget(self.group_start_btn)
            hbox.addWidget(self.group_stop_btn)

            colspan = max(1, layout.columnCount() - 1)
            layout.addLayout(hbox, row, 1, 1, colspan)
        except Exception as e:      # 레이아웃 구조가 다를 경우 앱이 죽지 않도록
            logger.warning(f"그룹 UI 배치 실패(무시하고 계속): {e}")

        self._refresh_group_filter()

    def _create_group_action_button(self, icon_name: str, tooltip: str, slot) -> QPushButton:
        """
        그룹 일괄 제어용 아이콘 버튼을 생성한다.

        텍스트 버튼("그룹 시작")을 쓰면 3컬럼 제약 때문에 상단 행의 버튼 폭이
        줄어들므로 아이콘 전용(28x28)으로 만든다. 대신 툴팁을 반드시 붙여
        어떤 동작인지 알 수 있게 한다.
        """
        button = QPushButton(self)
        button.setIcon(qta.icon(icon_name, color=self._icon_color))
        button.setIconSize(QSize(16, 16))
        button.setFixedSize(28, 28)
        button.setToolTip(tooltip)
        button.clicked.connect(slot)
        return button

    def refresh_group_button_icons(self):
        """
        테마(아이콘 색) 변경 시 그룹 버튼 아이콘을 다시 그린다.
        아이콘 버튼은 텍스트 색 상속이 안 되므로 직접 갱신해야 한다.
        """
        for attr, icon_name in (("group_start_btn", "mdi6.play"),
                                ("group_stop_btn", "mdi6.stop")):
            button = getattr(self, attr, None)
            if button is not None:
                button.setIcon(qta.icon(icon_name, color=self._icon_color))

    # ================================================================== #
    # 2. 그룹 관련 헬퍼 (신규)
    # ================================================================== #

    def _available_groups(self) -> List[str]:
        """현재 등록된 모든 그룹명 (정렬, '미분류' 제외)"""
        return sorted({
            c.group_name
            for c in self.camera_manager.get_all_cameras()
            if c.group_name
        })

    def _refresh_group_filter(self):
        """
        그룹 필터 콤보를 현재 카메라 목록 기준으로 다시 채운다.

        카메라 추가/편집/삭제 후 호출해야 새 그룹이 나타난다.
        이전 선택은 가능한 한 유지한다.
        """
        # hasattr 만으로는 부족하다 (속성이 None 으로 존재할 수 있음)
        if getattr(self, "group_filter", None) is None:
            return

        previous = self.group_filter.currentData()

        self.group_filter.blockSignals(True)      # 재구성 중 시그널 폭주 방지
        self.group_filter.clear()
        self.group_filter.addItem("전체 그룹", FILTER_ALL)

        for group in self._available_groups():
            self.group_filter.addItem(group, group)

        # '미분류' 항목은 그룹 없는 카메라가 실제로 존재할 때만 노출
        has_ungrouped = any(
            not c.group_name for c in self.camera_manager.get_all_cameras()
        )
        if has_ungrouped:
            self.group_filter.addItem("미분류", UNGROUPED)

        index = self.group_filter.findData(previous)
        self.group_filter.setCurrentIndex(index if index >= 0 else 0)
        self.group_filter.blockSignals(False)

    def _current_group_filter(self):
        """
        현재 선택된 그룹 필터 값.

        반환값: None = 전체 그룹, '' = 미분류, 그 외 = 그룹명

        선택된 항목이 없을 때(currentIndex == -1)도 '전체'로 취급한다.
        예를 들어 findData() 가 실패해 setCurrentIndex(-1) 이 호출되면
        아무것도 선택되지 않은 상태가 되는데, 이때 필터가 조용히
        '전부 통과'로 동작하면서 UI 에는 잘못된 그룹명이 표시될 수 있다.
        """
        if getattr(self, "group_filter", None) is None:
            return FILTER_ALL
        if self.group_filter.currentIndex() < 0:
            return FILTER_ALL
        return self.group_filter.currentData()

    def _current_group_label(self) -> str:
        """상태 표시용 현재 그룹 이름"""
        current = self._current_group_filter()
        if current is FILTER_ALL:
            return "전체"
        return current or "미분류"

    def _matches_group_filter(self, config: CameraConfig) -> bool:
        """이 카메라가 현재 그룹 필터에 부합하는가"""
        selected = self._current_group_filter()
        if selected is FILTER_ALL:
            return True
        return (config.group_name or UNGROUPED) == selected

    def _cameras_in_current_group(self) -> List[CameraConfig]:
        """현재 그룹 필터에 해당하는 카메라 목록 (camera_order 순서)"""
        result = []
        for camera_id in self.camera_order:
            config = self.camera_manager.get_camera(camera_id)
            if config and self._matches_group_filter(config):
                result.append(config)
        return result

    @Slot()
    def _on_group_filter_changed(self):
        """
        그룹 필터 변경 → 테이블과 그리드에 동시 반영.

        주의: _rebuild_camera_table() 은 테이블만 갱신한다.
        그리드까지 갱신하려면 _update_grid_layout() 을 함께 호출해야 한다.
        (_sync_camera_state() 는 두 개를 모두 호출하지만, 필터 변경은
          설정 저장이 필요 없으므로 직접 호출한다)
        """
        self._rebuild_camera_table()
        self._update_grid_layout()
        logger.debug(f"그룹 필터 변경: {self._current_group_filter()!r}")

    def rename_group(self, old_name: str, new_name: str, save: bool = True):
        """
        그룹명을 일괄 변경한다.

        group_name 을 문자열로 관리하므로 그룹명 변경은 이 한 곳에서 처리된다.
        필터 콤보 갱신까지 포함하므로 호출 후 별도 처리가 필요 없다.

        Args:
            old_name: 기존 그룹명 ('' 이면 미분류)
            new_name: 새 그룹명 ('' 이면 미분류로 이동)
        """
        new_name = (new_name or "").strip()
        changed = 0
        for config in self.camera_manager.get_all_cameras():
            if (config.group_name or UNGROUPED) == (old_name or UNGROUPED):
                config.group_name = new_name
                changed += 1

        if changed:
            self._sync_camera_state(save_cameras=save)
            # 필터 콤보의 항목 이름도 바뀌어야 하므로 반드시 갱신한다.
            # (이 호출이 없으면 콤보에 옛 그룹명이 남아 필터가 동작하지 않는다)
            self._refresh_group_filter()
            logger.info(f"그룹명 변경: {old_name!r} → {new_name!r} ({changed}대)")
        return changed

    # ================================================================== #
    # 3. 카메라 추가 / 제거
    # ================================================================== #

    def _add_camera_internal(self, config: CameraConfig, skip_table_add: bool = False):
        """ 카메라 추가 """
        # self.camera_manager.add_camera(config) # _load_settings_and_cameras에서 이미 추가됨
        self.camera_statuses[config.camera_id] = CameraStatus.DISCONNECTED
        if not skip_table_add:
            # 테이블 행 추가
            self._add_table_row(config)

        # 위젯 생성
        widget = CameraWidget(config)
        widget.set_custom_colors(self.custom_class_colors)  # 커스텀 색상 적용
        widget.open_in_new_window.connect(self._on_open_in_new_window)
        # 20260601 swjang 영상 화면을 더블 클릭했을 때 새창 표시 (최종본 반영)
        widget.double_clicked.connect(self._on_open_in_new_window)
        self.camera_widgets[config.camera_id] = widget

        # 워커 설정
        self._setup_worker(config, widget)

        # 그리드 업데이트
        self._update_grid_layout()

        self._update_counts()

    def _setup_worker(self, config: CameraConfig, widget: CameraWidget):
        """워커 생성 및 시그널 연결"""
        worker = CameraWorker(config)
        worker.frame_captured.connect(self._on_frame_captured)
        worker.status_changed.connect(self._on_camera_status_changed)
        worker.error_occurred.connect(self._on_camera_error)
        # worker.fps_updated.connect(widget.update_fps)
        worker.resolution_detected.connect(widget.update_resolution)
        worker.status_changed.connect(widget.update_status)
        self.camera_workers[config.camera_id] = worker

        # "전체 시작" 상태이면 새로 추가된 카메라도 시작
        if self.is_system_running:
            if config.display_enabled and not worker.isRunning():
                worker.start()

    @Slot()
    def _on_add_camera(self):
        """
        카메라 추가.

        [수정] 다이얼로그에 기존 그룹 목록을 전달하여 드롭다운에서 고를 수 있게 함.
               추가 후 그룹 필터를 갱신.
        """
        dialog = CameraDialog(parent=self)
        dialog.set_available_groups(self._available_groups())     # [신규]
        if dialog.exec():
            config = dialog.get_config()
            self.camera_manager.add_camera(config)
            self._add_camera_internal(config, skip_table_add=True)  # skip_table_add=True로 호출

            # 새 카메라를 camera_order의 마지막에 추가
            self.camera_order.append(config.camera_id)
            self._refresh_group_filter()                          # [신규]
            self._sync_camera_state(save_cameras=True)

    @Slot()
    def _on_edit_camera(self):
        """
        카메라 수정.

        [수정] 그룹 변경 시에도 워커 재시작이 필요 없도록 별도 처리.
               (그룹은 표시 속성이므로 소스가 그대로면 워커를 건드리지 않는다)
        """
        camera_id = self._get_single_selected_camera_id("수정")
        if not camera_id:
            return

        config = self.camera_manager.get_camera(camera_id)
        if not config:
            return

        dialog = CameraDialog(config, parent=self)
        dialog.set_available_groups(self._available_groups())     # [신규]
        if dialog.exec():
            new_config = dialog.get_config()

            # 워커 재시작 필요 여부
            need_restart = config.source != new_config.source

            # 업데이트
            self.camera_manager.update_camera(camera_id, new_config)
            self._update_table_row(camera_id, new_config)
            self.settings_manager.save_cameras_info(self.camera_manager.get_all_cameras())
            self._refresh_group_filter()                          # [신규]
            # 20260528 swjang 수정시 시스템 현황이 갱신 기능 누락 (최종본 반영)
            self._update_counts()

            if camera_id in self.camera_widgets:
                self.camera_widgets[camera_id].update_config(new_config)

            if need_restart and camera_id in self.camera_workers:
                worker = self.camera_workers[camera_id]
                if worker.isRunning():
                    worker.stop()
                    worker.update_config(new_config)
                    worker.start()

    @Slot()
    def _on_delete_camera(self):
        """선택된 카메라(들) 삭제"""
        camera_ids = self._get_checked_camera_ids()
        if not camera_ids:
            QMessageBox.warning(self, "경고", "삭제할 카메라를 선택하세요.")
            return

        count = len(camera_ids)
        msg = f"선택한 {count}개의 카메라를 모두 삭제하시겠습니까?" if count > 1 else "선택한 카메라를 삭제하시겠습니까?"
        reply = QMessageBox.question(self, "확인", msg, QMessageBox.Yes | QMessageBox.No)

        if reply == QMessageBox.Yes:
            for camera_id in camera_ids:
                self._remove_camera(camera_id, skip_ui_update=True)

            self._refresh_group_filter()      # [신규] 그룹이 사라질 수 있음
            self._sync_camera_state(save_cameras=True)

    def _remove_camera(self, camera_id: str, skip_ui_update: bool = False):
        """카메라 제거 및 리소스 정리"""
        # [신규] 정지 상태 감시 결과 제거
        self.camera_health.pop(camera_id, None)

        # 워커 정지 및 제거
        if camera_id in self.camera_workers:
            self.camera_workers[camera_id].stop()
            del self.camera_workers[camera_id]

        # 위젯 제거
        if camera_id in self.camera_widgets:
            widget = self.camera_widgets[camera_id]
            self.grid_layout.removeWidget(widget)
            widget.deleteLater()
            del self.camera_widgets[camera_id]

        # 분리된 창(Detached)이 열려있다면 닫기
        if camera_id in self.detached_windows:
            for window in self.detached_windows[camera_id]:
                window.close()
            del self.detached_windows[camera_id]

        # 테이블에서 제거
        row = self._find_row_by_camera_id(camera_id)
        if row != -1:
            self.camera_table.removeRow(row)

        # 매니저에서 제거
        self.camera_manager.remove_camera(camera_id)

        # 상태 dict에서 제거
        if camera_id in self.camera_statuses:
            del self.camera_statuses[camera_id]

        # camera_order에서 제거
        if camera_id in self.camera_order:
            self.camera_order.remove(camera_id)

        # 위젯 참조 제거
        if camera_id in self.table_row_widgets:
            del self.table_row_widgets[camera_id]

        if not skip_ui_update:
            self._sync_camera_state()

    # ================================================================== #
    # 4. 테이블 렌더링
    # ================================================================== #

    def _find_row_by_camera_id(self, camera_id: str) -> int:
        """
        카메라 ID로 테이블 행 인덱스 찾기.

        camera_id 를 Qt.UserRole 에서 읽으므로, 그룹 헤더 행 같은
        특수 행이 삽입되어도 안전하다.
        """
        for row in range(self.camera_table.rowCount()):
            item = self.camera_table.item(row, Col.CHECK)
            if item and item.data(Qt.UserRole) == camera_id:
                return row
        return -1

    def _add_table_row(self, config: CameraConfig):
        """
        테이블에 행 추가.

        [수정] 그룹 컬럼 셀 추가.
        """
        row = self.camera_table.rowCount()
        self.camera_table.insertRow(row)

        # 컬럼별 아이템/위젯 생성
        status_label = self._create_status_widget()
        manage_buttons = self._create_manage_buttons(config.camera_id)
        move_buttons_widget = self._create_move_buttons_widget(config.camera_id)

        self.camera_table.setItem(row, Col.CHECK, self._create_check_item(config))
        self.camera_table.setCellWidget(row, Col.MOVE, move_buttons_widget)
        self.camera_table.setCellWidget(row, Col.STATUS, self._center_widget(status_label))

        # --- [신규] 그룹 셀 ---
        self.camera_table.setItem(row, Col.GROUP, self._create_group_item(config))

        self.camera_table.setItem(row, Col.NAME, QTableWidgetItem(config.name))

        # 영상/탐지 체크박스
        display_container, display_checkbox = self._create_table_checkbox(
            config.display_enabled, 
            lambda checked: self._on_display_toggled(config.camera_id, checked)
        )
        self.camera_table.setCellWidget(row, Col.DISPLAY, display_container)
        
        detect_container, detect_checkbox = self._create_table_checkbox(
            config.detection_enabled, 
            lambda checked: self._on_detection_toggled(config.camera_id, checked)
        )
        self.camera_table.setCellWidget(row, Col.DETECT, detect_container)

        self.camera_table.setItem(row, Col.ADDRESS, self._create_address_item(config))
        self.camera_table.setCellWidget(row, Col.MANAGE, self._create_manage_button_container(manage_buttons))

        # 위젯 참조 저장 (already handled in _create_move_buttons_widget for up/down buttons)
        self.table_row_widgets[config.camera_id].update({
            "status_label": status_label,
            "display_checkbox": display_checkbox,
            "detect_checkbox": detect_checkbox,
            **{f"{name}_btn": btn for name, btn in manage_buttons.items()}
        })

        # [수정] 다시 그린 행에도 현재 상태(아이콘/관리 버튼)를 그대로 반영한다.
        # 기존에는 재구성 시 모든 행이 '연결 끊김' + 버튼 전부 활성으로 초기화되었다.
        self._apply_row_status(
            config.camera_id,
            self.camera_statuses.get(config.camera_id, CameraStatus.DISCONNECTED)
        )

    def _create_check_item(self, config: CameraConfig) -> QTableWidgetItem:
        """체크박스 아이템 생성"""
        item = QTableWidgetItem()
        item.setCheckState(Qt.Checked if config.enabled else Qt.Unchecked)
        item.setData(Qt.UserRole, config.camera_id)
        return item

    def _create_group_item(self, config: CameraConfig) -> QTableWidgetItem:
        """
        [신규] 그룹명 아이템 생성.

        그룹이 없으면 '미분류'를 옅은 색으로 표시한다.
        """
        item = QTableWidgetItem(config.group_display)
        item.setData(Qt.UserRole, config.group_name)
        if not config.is_grouped:
            # 미분류는 시각적으로 구분 (회색 처리)
            item.setForeground(QColor("#8b949e"))
        item.setToolTip(f"그룹: {config.group_display}")
        return item

    def _create_status_widget(self) -> QLabel:
        """상태 아이콘 라벨 생성"""
        label = QLabel()
        label.setObjectName("status_lbl")
        icon_name, color, tooltip, _ = self.STATUS_CONFIG[CameraStatus.DISCONNECTED]
        label.setPixmap(qta.icon(icon_name, color=color).pixmap(20, 20))
        label.setToolTip(tooltip)
        return label

    @staticmethod
    def _format_address(source: str, max_len: int = 30) -> str:
        """주소 문자열을 말줄임표(...)와 함께 포맷팅"""
        return source[:max_len] + "..." if len(source) > max_len else source

    def _create_address_item(self, config: CameraConfig) -> QTableWidgetItem:
        """주소 아이템 생성"""
        item = QTableWidgetItem(self._format_address(config.source))
        item.setToolTip(config.source)
        return item

    def _create_table_checkbox(self, is_checked: bool, slot) -> tuple[QWidget, QCheckBox]:
        """테이블 내에 사용될 중앙 정렬된 체크박스를 생성합니다."""
        checkbox = QCheckBox()
        checkbox.setChecked(is_checked)
        checkbox.toggled.connect(slot)
        return self._center_widget(checkbox), checkbox

    def _create_move_buttons_widget(self, camera_id: str) -> QWidget:
        """테이블 내 '이동' 컬럼에 들어갈 위/아래 버튼들을 생성합니다."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        up_btn = self._create_tool_button('mdi6.chevron-up', "위로 이동", lambda: self._on_move_camera_up(camera_id))
        down_btn = self._create_tool_button('mdi6.chevron-down', "아래로 이동", lambda: self._on_move_camera_down(camera_id))
        
        layout.addWidget(up_btn)
        layout.addWidget(down_btn)
        layout.setAlignment(Qt.AlignCenter)
        
        # 버튼 참조를 저장하여 활성화/비활성화 제어
        if camera_id not in self.table_row_widgets:
            self.table_row_widgets[camera_id] = {}
        self.table_row_widgets[camera_id]["up_btn"] = up_btn
        self.table_row_widgets[camera_id]["down_btn"] = down_btn

        return widget

    def _create_manage_buttons(self, camera_id: str) -> Dict[str, QPushButton]:
        """테이블 내 '관리' 컬럼에 들어갈 버튼들을 생성하고 딕셔너리로 반환합니다."""
        # 버튼 생성
        start_btn = self._create_tool_button('mdi6.play', "시작", lambda: self._on_individual_start(camera_id))
        stop_btn = self._create_tool_button('mdi6.stop', "정지", lambda: self._on_individual_stop(camera_id))
        reconnect_btn = self._create_tool_button('mdi6.refresh', "재연결", lambda: self._on_individual_reconnect(camera_id))
        
        return {"start": start_btn, "stop": stop_btn, "reconnect": reconnect_btn}

    def _create_manage_button_container(self, buttons: Dict[str, QPushButton]) -> QWidget:
        """관리 버튼들을 담을 컨테이너 위젯을 생성합니다."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(buttons["start"])
        layout.addWidget(buttons["stop"])
        layout.addWidget(buttons["reconnect"])
        layout.setAlignment(Qt.AlignCenter)
        return widget

    def _create_tool_button(self, icon_name: str, tooltip: str, slot) -> QPushButton:
        """아이콘 기반의 작은 툴 버튼을 생성합니다."""
        button = QPushButton()
        button.setIcon(qta.icon(icon_name, color=self._icon_color))
        button.setIconSize(QSize(22, 22))
        button.setFixedSize(23, 23)
        button.setToolTip(tooltip)
        button.clicked.connect(slot)
        # 260905 swjang 버튼이 포커스를 가져가지 않도록 설정
        # (설정 버튼을 클릭해도 포커스가 테이블 셀에 그대로 남아있게 유지)
        button.setFocusPolicy(Qt.NoFocus)
        return button

    def _center_widget(self, widget: QWidget) -> QWidget:
        """위젯을 중앙에 배치하기 위한 컨테이너 위젯을 반환합니다."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.addWidget(widget)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(0, 0, 0, 0)
        return container

    def _rebuild_camera_table(self):
        """
        camera_order 에 따라 카메라 테이블을 완전히 다시 그린다.

        [수정] 그룹 필터가 적용된 카메라는 건너뛴다.
               (camera_order 는 그대로 유지되므로 순서 정보는 보존된다)
        """
        self.camera_table.clearContents()
        self.camera_table.setRowCount(0)

        # [수정] clearContents() 로 셀 위젯이 모두 삭제되므로 이전 참조를 비운다.
        # (필터로 제외된 카메라의 참조가 남으면 삭제된 C++ 객체 접근으로 RuntimeError 발생)
        self.table_row_widgets.clear()

        for camera_id in self.camera_order:
            config = self.camera_manager.get_camera(camera_id)
            if config and self._matches_group_filter(config):     # [수정] 필터 조건
                self._add_table_row(config)
        self._update_move_button_states()

    def _update_move_button_states(self):
        """
        이동 버튼들의 활성화/비활성화 상태를 업데이트합니다.

        camera_order 상의 실제 위치를 기준으로 판단하므로,
        그룹 필터로 일부 행이 숨겨져 있어도 정확하다.
        """
        for row in range(self.camera_table.rowCount()):
            # Col.CHECK에 QTableWidgetItem이 있으므로 item()으로 접근
            check_item = self.camera_table.item(row, Col.CHECK)
            if not check_item:  # Check if item exists
                continue
            camera_id = check_item.data(Qt.UserRole)

            if camera_id in self.table_row_widgets:
                up_btn = self.table_row_widgets[camera_id]["up_btn"]
                down_btn = self.table_row_widgets[camera_id]["down_btn"]

                order_index = self.camera_order.index(camera_id) if camera_id in self.camera_order else -1
                up_btn.setEnabled(order_index > 0)
                down_btn.setEnabled(0 <= order_index < len(self.camera_order) - 1)

    def _update_table_row(self, camera_id: str, config: CameraConfig):
        """
        테이블 행 업데이트.

        [수정] 그룹 셀 갱신 추가.
        """
        row = self._find_row_by_camera_id(camera_id)
        if row != -1:
            # 그룹 업데이트 (신규)
            group_item = self.camera_table.item(row, Col.GROUP)
            if group_item:
                group_item.setText(config.group_display)
                group_item.setData(Qt.UserRole, config.group_name)
                group_item.setToolTip(f"그룹: {config.group_display}")

            # 이름 업데이트
            self.camera_table.item(row, Col.NAME).setText(config.name)

            # 영상 표시 및 탐지 체크박스 업데이트
            row_widgets = self.table_row_widgets.get(camera_id)
            if row_widgets:
                if display_checkbox := row_widgets.get("display_checkbox"):
                    display_checkbox.setChecked(config.display_enabled)
                if detect_checkbox := row_widgets.get("detect_checkbox"):
                    detect_checkbox.setChecked(config.detection_enabled)

            # 주소 업데이트
            address_item = self.camera_table.item(row, Col.ADDRESS)
            address_item.setText(self._format_address(config.source))
            address_item.setToolTip(config.source)

    # ================================================================== #
    # 5. 테이블 상호작용 (정렬 / 선택)
    # ================================================================== #

    @Slot(str)
    def _on_move_camera_up(self, camera_id: str):
        """카메라를 위로 이동"""
        current_index = self.camera_order.index(camera_id)
        if current_index > 0:
            self.camera_order[current_index], self.camera_order[current_index - 1] = \
                self.camera_order[current_index - 1], self.camera_order[current_index]
            self._sync_camera_state()

    @Slot(str)
    def _on_move_camera_down(self, camera_id: str):
        """카메라를 아래로 이동"""
        current_index = self.camera_order.index(camera_id)
        if current_index < len(self.camera_order) - 1:
            self.camera_order[current_index], self.camera_order[current_index + 1] = \
                self.camera_order[current_index + 1], self.camera_order[current_index]
            self._sync_camera_state()

    @Slot(QTableWidgetItem)
    def _on_table_item_double_clicked(self, item: QTableWidgetItem):
        """
        테이블 행 더블클릭 시 편집 모드 진입.

        [수정] 그룹 컬럼 더블클릭 시에는 그룹명을 바로 편집한다.
        """
        if not item:
            return

        clicked_row = item.row()
        clicked_col = item.column()

        # [신규] 그룹 셀 더블클릭 → 그룹명 변경
        if clicked_col == Col.GROUP:
            self._on_rename_group_dialog(clicked_row)
            return

        # 모든 체크박스 해제
        for row in range(self.camera_table.rowCount()):
            chk_item = self.camera_table.item(row, Col.CHECK)
            if chk_item and chk_item.row() != clicked_row:
                chk_item.setCheckState(Qt.Unchecked)

        # 더블클릭한 행의 체크박스 선택
        chk_item_to_select = self.camera_table.item(clicked_row, Col.CHECK)
        if chk_item_to_select:
            chk_item_to_select.setCheckState(Qt.Checked)

        # 편집 함수 호출
        self._on_edit_camera()

    @Slot(int, int)
    def _on_table_cell_clicked(self, row: int, col: int):
        """테이블 셀 클릭 시 행 선택 (체크박스 컬럼 클릭 시에도 행 선택)"""
        if col == Col.CHECK:
            self.camera_table.selectRow(row)

    def _get_checked_camera_ids(self) -> List[str]:
        """테이블에서 체크된 모든 카메라 ID 목록을 반환합니다."""
        checked_ids = []
        for row in range(self.camera_table.rowCount()):
            item = self.camera_table.item(row, Col.CHECK)
            if item and item.checkState() == Qt.Checked:
                camera_id = item.data(Qt.UserRole)
                if camera_id:
                    checked_ids.append(camera_id)
        return checked_ids

    def _get_selected_camera_id(self) -> List[str]:
        """
        테이블에서 체크된 카메라 ID 목록을 반환합니다.

        [2026 최종본 호환] 최종본은 이 메서드가 '리스트'를 돌려주는 형태로
        바뀌어 있었다(_on_delete_camera 가 여러 대를 한 번에 삭제).
        기존 호출부가 깨지지 않도록 리스트 반환을 유지한다.
        한 대만 필요한 곳(수정 등)은 _get_single_selected_camera_id() 를 쓴다.
        """
        return self._get_checked_camera_ids()

    def _get_single_selected_camera_id(self, operation: str) -> Optional[str]:
        """
        체크된 카메라가 정확히 1대일 때만 그 ID 를 반환한다.
        (0대 / 2대 이상이면 경고 후 None)
        """
        checked_ids = self._get_checked_camera_ids()

        if len(checked_ids) == 1:
            return checked_ids[0]

        if len(checked_ids) > 1:
            QMessageBox.warning(self, "경고", f"하나의 카메라만 선택하여 {operation}할 수 있습니다.")
        else:  # 0 checked
            QMessageBox.warning(self, "경고", f"{operation}할 카메라를 선택하세요.")

        return None

    # ================================================================== #
    # 6. 표시 토글
    # ================================================================== #

    @Slot(str, bool)
    def _on_display_toggled(self, camera_id: str, checked: bool):
        """영상 표시 상태 변경. 표시가 꺼지면 탐지도 자동으로 끔."""
        config = self.camera_manager.get_camera(camera_id)
        if not config:
            return

        # 1. Update display config
        config.display_enabled = checked
        self.camera_manager.update_camera(camera_id, config)
        self._update_grid_layout()
        self.settings_manager.save_cameras_info(self.camera_manager.get_all_cameras())
        logger.debug(f"Camera '{config.name}' display toggled: {checked}")

        # 2. If display is turned off, also turn off detection checkbox
        if not checked:
            row_widgets = self.table_row_widgets.get(camera_id)
            if row_widgets and (detect_checkbox := row_widgets.get("detect_checkbox")):
                if detect_checkbox.isChecked():
                    detect_checkbox.setChecked(False)

    @Slot(str, bool)
    def _on_detection_toggled(self, camera_id: str, checked: bool):
        """탐지 상태 변경"""
        config = self.camera_manager.get_camera(camera_id)
        if config:
            config.detection_enabled = checked
            self.camera_manager.update_camera(camera_id, config)
            self.settings_manager.save_cameras_info(self.camera_manager.get_all_cameras())
            logger.debug(f"Camera '{config.name}' detection toggled: {checked}")

            # 위젯에 즉시 반영 (아이콘 업데이트 및 바운딩 박스 표시 설정)
            if camera_id in self.camera_widgets:
                widget = self.camera_widgets[camera_id]
                widget.update_config(config)            # 상단 아이콘(돋보기) 상태 업데이트
                widget.set_detection_visible(checked)   # 현재 프레임의 바운딩 박스 즉시 숨김/표시

    # ================================================================== #
    # 7. 그리드 레이아웃 / 카운트
    # ================================================================== #

    @Slot()
    def _update_grid_layout(self):
        """
        그리드 레이아웃 업데이트.

        [수정] 그룹 필터 조건 추가.
               그룹을 선택하면 그리드에 해당 그룹 카메라만 배치된다.
        """
        # 기존 위젯을 레이아웃에서 제거. 위젯의 부모를 None으로 설정하여 완전히 제거.
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)

        # 기존 스트레치 초기화 (균등 배치를 위해 필요)
        for r in range(self.grid_layout.rowCount()):
            self.grid_layout.setRowStretch(r, 0)
        for c in range(self.grid_layout.columnCount()):
            self.grid_layout.setColumnStretch(c, 0)

        # 표시할 위젯 필터링 (camera_order 순서에 따라)
        visible_widgets = []
        for camera_id in self.camera_order:
            if camera_id in self.camera_widgets:
                widget = self.camera_widgets[camera_id]
                config = self.camera_manager.get_camera(camera_id)
                if config and config.display_enabled and self._matches_group_filter(config):
                    visible_widgets.append(widget)

        if not visible_widgets:
            return

        # 그리드 크기 결정
        grid_index = self.grid_combo.currentIndex()
        count = len(visible_widgets)

        cols = 1
        if grid_index == 0:  # 자동
            if count > 0:
                cols = math.ceil(math.sqrt(count))
        else:
            cols = grid_index  # 1, 2, 3, 4

        for i, widget in enumerate(visible_widgets):
            row = i // cols
            col = i % cols
            self.grid_layout.addWidget(widget, row, col)

        # 균등 배치를 위해 행/열 스트레치 설정
        total_rows = math.ceil(count / cols)
        for r in range(total_rows):
            self.grid_layout.setRowStretch(r, 1)
        for c in range(cols):
            self.grid_layout.setColumnStretch(c, 1)

    def _update_counts(self):
        """카운트 업데이트. camera_statuses를 기반으로 UI의 카운트를 업데이트합니다."""
        total_count = len(self.camera_statuses)
        # [수정] Active = 스트리밍 중 정상(ACTIVE) + 정지 중이지만 RTSP 응답이 있는 카메라.
        # 운용자가 시청하지 않는(정지한) 카메라도 실제 생존 여부로 집계한다.
        active_count = sum(
            1 for cid, status in self.camera_statuses.items()
            if status == CameraStatus.ACTIVE
            or (not self._is_streaming(cid) and self._health_alive(cid) is True)
        )
        disabled_count = total_count - active_count

        self.total_count_label.setText(str(total_count))
        self.active_count_label.setText(str(active_count))
        self.disable_count_label.setText(str(disabled_count))

        self.active_label.setText(f"Total : {total_count}, Active : {active_count}, Disabled : {disabled_count} ")

    def _sync_camera_state(self, save_cameras: bool = False):
        """카메라 순서/상태 변경에 따른 UI 갱신 및 설정 파일 동기화"""
        self._rebuild_camera_table()
        self._update_grid_layout()
        self._update_counts()
        if save_cameras:
            self.settings_manager.save_cameras_info(self.camera_manager.get_all_cameras())
        self._save_app_settings()

    # ================================================================== #
    # 8. 워커 수명주기 (시작 / 정지 / 재연결)
    # ================================================================== #

    @Slot()
    def _on_start_all(self):
        """전체 시작"""
        for camera_id, worker in self.camera_workers.items():
            config = self.camera_manager.get_camera(camera_id)
            # if config and config.enabled and not worker.isRunning():
            if config and config.display_enabled and not worker.isRunning():
                worker.start()

        self.is_system_running = True
        self.status_label.setText("전체 카메라 시작됨")

    @Slot()
    def _on_stop_all(self):
        """전체 정지"""
        for worker in self.camera_workers.values():
            if worker.isRunning():
                worker.stop()

        self.is_system_running = False
        self.status_label.setText("전체 카메라 정지됨")

    @Slot(str)
    def _on_individual_start(self, camera_id: str):
        """개별 카메라 시작"""
        if camera_id in self.camera_workers:
            worker = self.camera_workers[camera_id]
            if not worker.isRunning():
                worker.start()
                logger.debug(f"Started individual camera: {camera_id}")

    @Slot(str)
    def _on_individual_stop(self, camera_id: str):
        """개별 카메라 정지"""
        if camera_id in self.camera_workers:
            worker = self.camera_workers[camera_id]
            if worker.isRunning():
                worker.stop()
                logger.debug(f"Stopped individual camera: {camera_id}")

    @Slot(str)
    def _on_individual_reconnect(self, camera_id: str):
        """개별 카메라 재연결"""
        if camera_id in self.camera_workers:
            worker = self.camera_workers[camera_id]
            worker.reconnect()
            logger.debug(f"Triggered manual reconnect for camera: {camera_id}")

    # ------------------------------------------------------------------ #
    # [신규] 그룹 단위 일괄 제어
    # ------------------------------------------------------------------ #

    @Slot()
    def _on_start_group(self):
        """현재 그룹 필터에 해당하는 카메라만 시작"""
        targets = self._cameras_in_current_group()
        if not targets:
            QMessageBox.information(self, "알림", "시작할 카메라가 없습니다.")
            return

        started = 0
        for config in targets:
            worker = self.camera_workers.get(config.camera_id)
            if worker and not worker.isRunning():
                worker.start()
                started += 1

        group_label = self._current_group_label()
        self.status_label.setText(f"[{group_label}] {started}개 카메라 시작됨")
        logger.info(f"그룹 '{group_label}' 시작: {started}대")

    @Slot()
    def _on_stop_group(self):
        """현재 그룹 필터에 해당하는 카메라만 정지"""
        targets = self._cameras_in_current_group()
        stopped = 0
        for config in targets:
            worker = self.camera_workers.get(config.camera_id)
            if worker and worker.isRunning():
                worker.stop()
                stopped += 1

        group_label = self._current_group_label()
        self.status_label.setText(f"[{group_label}] {stopped}개 카메라 정지됨")
        logger.info(f"그룹 '{group_label}' 정지: {stopped}대")

    def _on_rename_group_dialog(self, row: int):
        """
        [신규] 그룹 컬럼 더블클릭 시 그룹명을 입력받아 일괄 변경한다.

        그룹명은 문자열로 관리되므로, 이름을 바꾸면 같은 그룹의
        모든 카메라가 함께 갱신된다.
        """
        check_item = self.camera_table.item(row, Col.CHECK)
        if not check_item:
            return
        camera_id = check_item.data(Qt.UserRole)
        config = self.camera_manager.get_camera(camera_id)
        if not config:
            return

        old_name = config.group_name
        new_name, ok = QInputDialog.getText(
            self,
            "그룹명 변경",
            f"'{config.group_display}' 그룹의 새 이름을 입력하세요.\n"
            f"(같은 그룹의 다른 카메라도 함께 변경됩니다.\n"
            f" 빈 칸으로 두면 '미분류'가 됩니다)",
            text=old_name,
        )
        if not ok:
            return

        new_name = (new_name or "").strip()
        if new_name == old_name:
            return

        self.rename_group(old_name, new_name)
        self._refresh_group_filter()

    # ================================================================== #
    # 9. 프레임 / 상태 전달
    # ================================================================== #

    def _distribute_frame_to_widgets(self, camera_id: str, frame_data: FrameData):
        """메인 그리드 위젯 및 분리된 창에 프레임 전달"""
        if camera_id in self.camera_widgets:
            self.camera_widgets[camera_id].update_frame(frame_data)
        if camera_id in self.detached_windows:
            for window in self.detached_windows[camera_id]:
                window.update_frame(frame_data)

    @Slot(object)
    def _on_frame_captured(self, frame_data: FrameData):
        """프레임 수신 (하이브리드 1단계: 원본 프레임)"""
        camera_id = frame_data.camera_id
        config = self.camera_manager.get_camera(camera_id)

        if not config:
            return

        # [수정] '영상' 열이 꺼진 카메라는 화면 그리기를 건너뛴다.
        # 워커는 계속 동작하므로 상태(시스템 현황)는 그대로 갱신되고,
        # 숨겨진 위젯의 색 변환/스케일링 비용만 사라진다.
        # 단, 탐지가 켜져 있거나 분리 창으로 보고 있는 경우는 기존대로 처리한다.
        if (not config.display_enabled
                and not config.detection_enabled
                and not self.detached_windows.get(camera_id)):
            return

        if config.detection_enabled:
            # 탐지 활성화: 추론 워커로 전달
            self.inference_worker.add_frame(frame_data)
        else:
            # 탐지 비활성화: 바로 UI 업데이트
            self._distribute_frame_to_widgets(camera_id, frame_data)

    @Slot(str, object)
    def _on_camera_status_changed(self, camera_id: str, status: CameraStatus):
        """카메라 상태 변경"""
        self.camera_statuses[camera_id] = status

        # [신규] 정지 상태 감시 연동
        # - 스트리밍을 시작하면 이전 감시 결과는 버린다 (워커 상태가 우선)
        # - 스트리밍이 끝나면(DISCONNECTED) 감시 대상에 다시 넣고 즉시 한 번 확인한다
        if status in (CameraStatus.CONNECTING, CameraStatus.ACTIVE):
            self.camera_health.pop(camera_id, None)
        self._refresh_health_targets(check_now=(status == CameraStatus.DISCONNECTED))

        # 테이블 행(상태 아이콘/관리 버튼) 갱신
        self._apply_row_status(camera_id, status)

        # 분리된 창에도 상태 전파
        if camera_id in self.detached_windows:
            for window in self.detached_windows[camera_id]:
                window.update_status(status)

        self._update_counts()

    def _apply_row_status(self, camera_id: str, status: CameraStatus):
        """
        [신규] 카메라 목록 행의 상태 아이콘과 관리 버튼을 상태에 맞게 갱신한다.

        상태 변경 시(_on_camera_status_changed)와 테이블 재구성 시(_add_table_row)
        같은 코드를 쓰도록 분리했다.
        """
        # 클래스 상수로 정의된 설정 사용
        icon_name, color, tooltip, button_state = self.STATUS_CONFIG.get(
            status, ("mdi6.help-circle", "#6e7681", "알 수 없음", self.ButtonState(False, False, False))
        )

        # [신규] 정지 중인 카메라는 RTSP 응답 확인 결과로 색/툴팁을 보완한다 (아이콘 종류는 유지)
        if status == CameraStatus.DISCONNECTED and not self._is_streaming(camera_id):
            health = self.camera_health.get(camera_id)
            if health is not None:
                alive, checked_at = health
                when = datetime.fromtimestamp(checked_at).strftime("%H:%M:%S")
                if alive:
                    # 색 = 생존 여부(초록/빨강), 아이콘 = 스트리밍 여부. 확인 전/확인 불가는 회색 유지
                    color = "#3fb950"
                    tooltip = f"대기 · 카메라 응답 있음 (확인 {when})"
                else:
                    color = "#f85149"
                    tooltip = f"오프라인 · 카메라 응답 없음 (확인 {when})"

        # 위젯 직접 참조를 통해 UI 업데이트
        row_widgets = self.table_row_widgets.get(camera_id)
        if row_widgets:
            # 1. 상태 아이콘 업데이트
            if status_label := row_widgets.get("status_label"):
                status_label.setPixmap(qta.icon(icon_name, color=color).pixmap(24, 24))
                status_label.setToolTip(tooltip)
            
            # 2. 관리 버튼 상태 업데이트
            if start_btn := row_widgets.get("start_btn"):
                start_btn.setEnabled(button_state.start_enabled)
            if stop_btn := row_widgets.get("stop_btn"):
                stop_btn.setEnabled(button_state.stop_enabled)
            if reconnect_btn := row_widgets.get("reconnect_btn"):
                reconnect_btn.setEnabled(button_state.reconnect_enabled)

    @Slot(str, str)
    def _on_camera_error(self, camera_id: str, message: str):
        """카메라 오류 발생"""
        config = self.camera_manager.get_camera(camera_id)
        camera_name = config.name if config else camera_id
        
        # 이벤트 로그에 추가
        log_message = f"연결 오류: {message}"
        self._add_event_log(camera_name, log_message)

        # 테이블 상태 아이콘에 툴팁 설정
        row_widgets = self.table_row_widgets.get(camera_id)
        if row_widgets and (status_label := row_widgets.get("status_label")):
            status_label.setToolTip(log_message)

    # ================================================================== #
    # 10. 분리 창(Detached Window)
    # ================================================================== #

    @Slot(str)
    def _on_open_in_new_window(self, camera_id: str):
        """카메라 새 창으로 열기 (Viewer-only)"""
        config = self.camera_manager.get_camera(camera_id)
        if not config:
            return

        logger.debug(f"camera status : {self.camera_statuses.get(camera_id)}")

        # 2026 최종본 동작: 카메라가 ACTIVE 상태일 때만 새 창을 연다.
        if self.camera_statuses.get(camera_id) != CameraStatus.ACTIVE:
            return
        
        detached_window = DetachedCameraWindow(config, self)
        detached_window.camera_widget.set_custom_colors(self.custom_class_colors)

        windows = self.detached_windows.setdefault(camera_id, [])
        windows.append(detached_window)

        detached_window.finished.connect(
            lambda: self._on_detached_window_closed(
                detached_window, camera_id
            )
        )

        worker = self.camera_workers.get(camera_id)
        if worker:
            worker.set_high_resolution_enabled(True)

        current_status = self.camera_statuses.get(
            camera_id, CameraStatus.DISCONNECTED
        )
        detached_window.update_status(current_status)

        detached_window.showFullScreen()

    def _on_detached_window_closed(
        self,
        window: DetachedCameraWindow,
        camera_id: str,
    ):
        windows = self.detached_windows.get(camera_id, [])
        if window in windows:
            windows.remove(window)

        if not windows:
            self.detached_windows.pop(camera_id, None)

        # 다른 분리 창이 남아 있으면 고해상도 요청을 유지한다.
        worker = self.camera_workers.get(camera_id)
        if worker:
            worker.set_high_resolution_enabled(
                bool(self.detached_windows.get(camera_id))
            )

        logger.debug(
            f"Detached window for camera {camera_id} closed and removed."
        )

    def _is_streaming(self, camera_id: str) -> bool:
        """카메라 워커가 실행 중(스트리밍 중)인지 여부"""
        worker = self.camera_workers.get(camera_id)
        return bool(worker and worker.isRunning())

    def _health_alive(self, camera_id: str) -> Optional[bool]:
        """정지 상태 감시 결과. 아직 확인 전이거나 RTSP 가 아니면 None"""
        health = self.camera_health.get(camera_id)
        return health[0] if health else None

    def _refresh_health_targets(self, check_now: bool = False):
        """스트리밍 중이 아닌 카메라만 감시 대상으로 넘긴다."""
        monitor = getattr(self, "health_monitor", None)
        if monitor is None:
            return
        targets = {}
        for cid in self.camera_order:
            config = self.camera_manager.get_camera(cid)
            if config and not self._is_streaming(cid):
                targets[cid] = str(config.source)
        monitor.set_targets(targets)
        if check_now:
            monitor.check_now()

    @Slot(str, bool, float)
    def _on_health_checked(self, camera_id: str, alive: bool, checked_at: float):
        """정지 상태 감시 결과 수신"""
        # 그 사이 스트리밍을 시작했거나 삭제된 카메라는 무시
        if self._is_streaming(camera_id) or self.camera_manager.get_camera(camera_id) is None:
            return

        prev = self._health_alive(camera_id)
        self.camera_health[camera_id] = (alive, checked_at)

        # 상태가 바뀔 때만 이벤트 로그 (처음 확인에서 '응답 없음'인 경우도 기록)
        if prev != alive and (prev is not None or not alive):
            config = self.camera_manager.get_camera(camera_id)
            name = config.name if config else camera_id
            self._add_event_log(name, "카메라 응답 복구 (정지 중 상태 확인)" if alive
                                else "카메라 응답 없음 (정지 중 상태 확인)")

        self._apply_row_status(camera_id, self.camera_statuses.get(camera_id, CameraStatus.DISCONNECTED))
        self._update_counts()
