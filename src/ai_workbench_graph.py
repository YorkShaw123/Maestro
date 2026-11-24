"""Native left-to-right branch canvas for the AI workbench."""
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QFrame, QGraphicsItem, QGraphicsScene, QGraphicsView, QMenu


NODE = 164
GAP = 158
ROW = 78
HEIGHT = 56
ACTIVE = '#00746e'
EDGE = '#8195a8'


@dataclass(frozen=True)
class Branch:
    title: str
    subtitle: str
    condition: str
    detail: str
    continues: bool
    state: int | None = None


@dataclass(frozen=True)
class Lane:
    branches: tuple[Branch, ...]
    selected: int | None = None


def ink(painter, text, rect, color, *, weight=None, points=None):
    font = QFont(painter.font())
    if weight is not None:
        font.setWeight(weight)
    if points is not None:
        font.setPointSize(points)
    painter.setFont(font)
    painter.setPen(QColor(color))
    display = QFontMetricsF(font).elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
    painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, display)


class GraphCard(QGraphicsItem):
    def __init__(self, title, subtitle, active=False, entry=False, on_click=None, terminal=False, on_rename=None):
        super().__init__()
        self.title, self.subtitle = title, subtitle
        self.active, self.entry, self.terminal = active, entry, terminal
        self.on_click = on_click
        self.on_rename = on_rename
        self.hover = False
        self.setAcceptHoverEvents(on_click is not None)
        if on_click is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setFlag(self.GraphicsItemFlag.ItemIsFocusable)

    def boundingRect(self):
        return QRectF(-1, -1, NODE+2, HEIGHT+3)

    def paint(self, painter, _option, _widget):
        rect = QRectF(0, 0, NODE, HEIGHT)
        if self.entry:
            fill, border, text, sub = '#173246', '#173246', '#ffffff', '#c0d6e2'
        elif self.active or self.hover or self.hasFocus():
            fill, border, text, sub = '#e1f5f1', ACTIVE, '#103d40', '#21615e'
        else:
            fill, border, text, sub = '#ffffff', '#b5c7d3', '#162e40', '#4f687a'
        painter.setPen(QPen(QColor(border), 2 if self.active else 1))
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(rect, 9, 9)
        reserve = 24 if not self.entry and not self.terminal else 12
        ink(painter, self.title, QRectF(12, 8, NODE-12-reserve, 21), text,
            weight=QFont.Weight.DemiBold, points=10)
        ink(painter, self.subtitle, QRectF(12, 30, NODE-12-reserve, 17), sub,
            weight=QFont.Weight.Normal, points=8)
        if not self.entry and not self.terminal:
            painter.setPen(QPen(QColor(border), 1.5))
            painter.drawLine(QPointF(NODE-16, 23), QPointF(NODE-12, 28))
            painter.drawLine(QPointF(NODE-12, 28), QPointF(NODE-16, 33))

    def hoverEnterEvent(self, event):
        self.hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if self.on_click and event.button() == Qt.MouseButton.LeftButton:
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self.on_click and event.button() == Qt.MouseButton.LeftButton and self.contains(event.pos()):
            event.accept()
            self.on_click()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if self.on_click and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.on_click()
            event.accept()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        if self.on_rename is None:
            super().contextMenuEvent(event)
            return
        event.accept()
        menu = QMenu(event.widget())
        rename = menu.addAction('命名状态…')
        rename.triggered.connect(self.on_rename)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(event.screenPos())


class ConditionTag(GraphCard):
    def boundingRect(self):
        return QRectF(-1, -1, GAP-36, 24)

    def paint(self, painter, _option, _widget):
        border = ACTIVE if self.active or self.hover or self.hasFocus() else '#ccd9e2'
        painter.setPen(QColor(border))
        painter.setBrush(QColor('#ebfaf6' if self.active else '#f8fbfd'))
        painter.drawRoundedRect(QRectF(0, 0, GAP-38, 22), 6, 6)
        ink(painter, self.title, QRectF(8, 1, GAP-54, 20), '#173e48' if self.active else '#354f61',
            weight=QFont.Weight.Medium, points=8)


class BranchGraph(QGraphicsView):
    branch_selected = Signal(int, int)
    entry_selected = Signal()
    state_name_requested = Signal(int)
    zoom_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(self.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(self.ViewportAnchor.AnchorUnderMouse)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setAccessibleName('AI 分支画布')
        self.cards = {}
        self.tags = {}
        self.pending_reveal = None

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, QColor('#eef4f8'))
        if self.transform().m11() < .60:
            return
        painter.setPen(QPen(QColor('#cddbe5'), 0))
        x0 = int(rect.left()//20)*20
        y0 = int(rect.top()//20)*20
        dots = QPolygonF([QPointF(x, y) for x in range(x0, int(rect.right())+1, 20)
                          for y in range(y0, int(rect.bottom())+1, 20)])
        painter.drawPoints(dots)

    def set_route(self, root, subtitle, lanes, *, reset=False, reveal=None, root_state=None):
        scene = self.scene()
        camera = self.mapToScene(self.viewport().rect().center())
        self.cards.clear()
        self.tags.clear()
        self.pending_reveal = reveal
        scene.clear()
        if not lanes:
            empty = scene.addSimpleText('没有符合当前筛选的分支', self.font())
            empty.setBrush(QColor('#4f687a'))
            empty.setPos(28, 32)
            scene.setSceneRect(scene.itemsBoundingRect().adjusted(-24, -24, 28, 24))
            return
        scene.setFont(self.font())
        entry = GraphCard(root, subtitle, entry=True, on_click=self.entry_selected.emit,
                          on_rename=self.rename_action(root_state))
        entry.setToolTip(self.card_tooltip(f'{root}\n返回链路入口', root_state))
        entry.setPos(24, 28)
        scene.addItem(entry)
        for depth, lane in enumerate(lanes):
            source_x = 24 + depth*(NODE+GAP)
            source_y = entry.y()+HEIGHT/2
            positions = [entry.y()+(index-(lane.selected or 0))*ROW for index in range(len(lane.branches))]
            rail = source_x+NODE+20
            self.draw_rail(source_x+NODE, source_y, rail, positions)
            for index, branch in enumerate(lane.branches):
                active = index == lane.selected
                y = positions[index]
                x = source_x + NODE + GAP
                self.connect_nodes(source_x+NODE, source_y, rail, x, y+HEIGHT/2, active)

                def select(depth=depth, index=index):
                    self.branch_selected.emit(depth, index)

                card = GraphCard(branch.title, branch.subtitle, active=active, on_click=select,
                                 terminal=not branch.continues, on_rename=self.rename_action(branch.state))
                card.setPos(x, y)
                card.setToolTip(self.card_tooltip(f'{branch.title}\n{branch.subtitle}\n{branch.detail}', branch.state))
                self.cards[depth, index] = card
                scene.addItem(card)
                tag = ConditionTag(branch.condition, '', active=active, on_click=select)
                tag.setPos(x-GAP+30, y+HEIGHT/2-11)
                tag.setToolTip(branch.detail)
                self.tags[depth, index] = tag
                scene.addItem(tag)
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-24, -25, 30, 27))
        if reset:
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().minimum())
            self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
        else:
            self.centerOn(camera)
        if reveal is not None:
            self.reveal_selection()
            QTimer.singleShot(0, self, self.reveal_selection)

    def rename_action(self, state):
        return None if state is None else lambda: self.state_name_requested.emit(state)

    @staticmethod
    def card_tooltip(text, state):
        return text if state is None else f'{text}\n右键命名状态'

    def reveal_selection(self):
        card = self.cards.get(self.pending_reveal)
        if card is not None:
            self.ensureVisible(card.sceneBoundingRect(), 28, 24)

    def draw_rail(self, start_x, start_y, rail, positions):
        if len(positions) < 2:
            return
        top = positions[0]+HEIGHT/2
        bottom = positions[-1]+HEIGHT/2
        top = top+9 if top < start_y else top
        bottom = bottom-9 if bottom > start_y else bottom
        pen = QPen(QColor(EDGE), 1.4)
        scene = self.scene()
        scene.addLine(start_x, start_y, rail, start_y, pen).setZValue(-2)
        scene.addLine(rail, top, rail, bottom, pen).setZValue(-2)

    def connect_nodes(self, start_x, start_y, rail, end_x, end_y, active):
        if start_y == end_y:
            path = QPainterPath(QPointF(start_x, start_y))
        else:
            approach = 9 if end_y < start_y else -9
            path = QPainterPath(QPointF(rail, end_y+approach))
            path.quadTo(QPointF(rail, end_y), QPointF(rail+9, end_y))
        path.lineTo(end_x-2, end_y)
        color = QColor(ACTIVE if active else EDGE)
        connector = self.scene().addPath(path, QPen(color, 2.5 if active else 1.3))
        connector.setZValue(-1)
        self.scene().addPolygon(QPolygonF([QPointF(end_x, end_y), QPointF(end_x-7, end_y-4),
                                           QPointF(end_x-7, end_y+4)]), QPen(Qt.PenStyle.NoPen), color).setZValue(1)

    def zoom(self, factor):
        scale = self.transform().m11()
        next_scale = max(.40, min(1.8, scale*factor))
        self.scale(next_scale/scale, next_scale/scale)
        self.zoom_changed.emit(round(next_scale*100))

    def reset_zoom(self):
        self.resetTransform()
        self.zoom_changed.emit(100)

    def fit_route(self):
        rect = self.sceneRect()
        if not self.cards:
            return
        self.resetTransform()
        scale = max(.40, min(1.25, (self.viewport().width()-10)/rect.width(),
                                 (self.viewport().height()-10)/rect.height()))
        self.scale(scale, scale)
        self.centerOn(rect.center())
        self.zoom_changed.emit(round(scale*100))

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.15 if event.angleDelta().y() > 0 else 1/1.15)
            event.accept()
        else:
            super().wheelEvent(event)
