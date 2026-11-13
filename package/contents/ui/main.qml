import QtQuick
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import "../code/backend.js" as Backend

PlasmoidItem {
    id: root

    property int refreshInterval: plasmoid.configuration.refreshInterval || 1000
    property string tipText: "No data — run start-monitoring.sh"

    toolTipMainText: "Per-Process Network"
    toolTipSubText: tipText

    Timer {
        id: pollTimer
        interval: root.refreshInterval
        repeat: true
        running: true
        onTriggered: {
            Backend.pollNow();
            root.tipText = Backend.processes.length > 0
                ? "↑ " + Backend.formatKbps(Backend.totalSent) + "  ↓ " + Backend.formatKbps(Backend.totalRecv)
                : "No data — run start-monitoring.sh";
            compactItem.updateDisplay();
            fullItem.updateDisplay();
        }
    }

    compactRepresentation: CompactRepresentation {
        id: compactItem
    }

    fullRepresentation: FullRepresentation {
        id: fullItem
    }

    Component.onCompleted: {
        Backend.pollNow();
        if (Backend.processes.length === 0) {
            Backend.loadTestData();
        }
        compactItem.updateDisplay();
        fullItem.updateDisplay();
    }
}
