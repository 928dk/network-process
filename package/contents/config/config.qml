import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import org.kde.kirigami as Kirigami
import org.kde.plasma.core

Kirigami.FormLayout {
    id: configRoot

    property alias cfg_refreshInterval: refreshSpinBox.value
    property alias cfg_monitorInterface: interfaceField.text

    SpinBox {
        id: refreshSpinBox
        Kirigami.FormData.label: "Refresh interval (ms):"
        from: 500
        to: 5000
        stepSize: 100
        value: 1000
    }

    TextField {
        id: interfaceField
        Kirigami.FormData.label: "Interface:"
        placeholderText: "Leave empty for all interfaces"
        text: ""
    }

    Label {
        Kirigami.FormData.label: " "
        text: "Tip: Run ./start-monitoring.sh [interface] in a terminal to start nethogs"
        color: PlasmaCore.Theme.textColor
        opacity: 0.6
        font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.8
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }
}
