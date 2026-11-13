import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import org.kde.kirigami as Kirigami
import org.kde.plasma.core as PlasmaCore
import "../code/backend.js" as Backend

Item {
    id: fullRoot

    implicitWidth: PlasmaCore.Units.gridUnit * 28
    implicitHeight: PlasmaCore.Units.gridUnit * 20

    property string headerSent: "↑ 0 B/s"
    property string headerRecv: "↓ 0 B/s"
    property string footerText: "No data — run ./start-monitoring.sh in terminal"

    function formatProgram(program) {
        if (!program || program === 'unknown') return 'unknown';
        var parts = program.split('/');
        return parts[parts.length - 1] || program;
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: PlasmaCore.Units.gridUnit
        spacing: PlasmaCore.Units.smallSpacing

        RowLayout {
            Layout.fillWidth: true
            spacing: PlasmaCore.Units.gridUnit
            Kirigami.Heading {
                level: 2
                text: "Per-Process Network"
                Layout.fillWidth: true
            }
            ColumnLayout {
                spacing: 0
                Label { text: fullRoot.headerSent; color: "#3584e4"; font.bold: true }
                Label { text: fullRoot.headerRecv; color: "#33d17a"; font.bold: true }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: PlasmaCore.Units.gridUnit * 6
            color: PlasmaCore.Theme.backgroundColor
            radius: PlasmaCore.Units.smallSpacing
            border.color: PlasmaCore.Theme.textColor
            border.width: 0.3

            Canvas {
                id: chart
                anchors.fill: parent
                anchors.margins: PlasmaCore.Units.smallSpacing
                antialiasing: true
                property bool hasData: false
                onPaint: {
                    var ctx = getContext("2d");
                    ctx.clearRect(0, 0, width, height);
                    var sentH = Backend.sentHistory;
                    var recvH = Backend.recvHistory;
                    if (sentH.length < 2) {
                        ctx.fillStyle = PlasmaCore.Theme.textColor;
                        ctx.font = "12px sans-serif";
                        ctx.textAlign = "center";
                        ctx.fillText("Waiting for data...", width / 2, height / 2 + 4);
                        return;
                    }
                    hasData = true;
                    var maxRate = Backend.getMaxRate() * 1.1;
                    var w = width, h = height;
                    var stepX = w / (Math.max(sentH.length, recvH.length) - 1);
                    ctx.strokeStyle = PlasmaCore.Theme.textColor;
                    ctx.globalAlpha = 0.1; ctx.lineWidth = 0.5;
                    for (var g = 0; g < 4; g++) {
                        var gy = h * (g / 4);
                        ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(w, gy); ctx.stroke();
                    }
                    ctx.globalAlpha = 1;
                    ctx.fillStyle = "#3584e4"; ctx.globalAlpha = 0.15;
                    ctx.beginPath(); ctx.moveTo(0, h);
                    for (var si = 0; si < sentH.length; si++)
                        ctx.lineTo(si * stepX, h - (sentH[si] / maxRate * h));
                    ctx.lineTo((sentH.length - 1) * stepX, h); ctx.closePath(); ctx.fill();
                    ctx.globalAlpha = 1;
                    ctx.strokeStyle = "#3584e4"; ctx.lineWidth = 2;
                    ctx.beginPath();
                    for (var si2 = 0; si2 < sentH.length; si2++) {
                        var sx2 = si2 * stepX, sy2 = h - (sentH[si2] / maxRate * h);
                        if (si2 === 0) ctx.moveTo(sx2, sy2); else ctx.lineTo(sx2, sy2);
                    }
                    ctx.stroke();
                    ctx.fillStyle = "#33d17a"; ctx.globalAlpha = 0.15;
                    ctx.beginPath(); ctx.moveTo(0, h);
                    for (var ri = 0; ri < recvH.length; ri++)
                        ctx.lineTo(ri * stepX, h - (recvH[ri] / maxRate * h));
                    ctx.lineTo((recvH.length - 1) * stepX, h); ctx.closePath(); ctx.fill();
                    ctx.globalAlpha = 1;
                    ctx.strokeStyle = "#33d17a"; ctx.lineWidth = 2;
                    ctx.beginPath();
                    for (var ri2 = 0; ri2 < recvH.length; ri2++) {
                        var rx2 = ri2 * stepX, ry2 = h - (recvH[ri2] / maxRate * h);
                        if (ri2 === 0) ctx.moveTo(rx2, ry2); else ctx.lineTo(rx2, ry2);
                    }
                    ctx.stroke();
                }
            }
        }

        RowLayout {
            spacing: PlasmaCore.Units.gridUnit
            Layout.topMargin: PlasmaCore.Units.smallSpacing
            RowLayout {
                spacing: PlasmaCore.Units.smallSpacing
                Rectangle { width: 10; height: 3; color: "#3584e4"; radius: 1 }
                Label { text: "Sent"; font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.8 }
            }
            RowLayout {
                spacing: PlasmaCore.Units.smallSpacing
                Rectangle { width: 10; height: 3; color: "#33d17a"; radius: 1 }
                Label { text: "Recv"; font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.8 }
            }
            Item { Layout.fillWidth: true }
            Label {
                id: maxLabel
                text: "↑ max: " + Backend.formatKbps(Backend.getMaxRate())
                font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.7
                opacity: 0.6
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: PlasmaCore.Theme.defaultFont.pixelSize + PlasmaCore.Units.smallSpacing * 2
            color: PlasmaCore.Theme.highlightColor; opacity: 0.3; radius: PlasmaCore.Units.smallSpacing
            RowLayout {
                anchors.fill: parent; anchors.margins: PlasmaCore.Units.smallSpacing; spacing: PlasmaCore.Units.smallSpacing
                Label { text: "Program"; Layout.preferredWidth: parent.width * 0.5; font.bold: true }
                Label { text: "PID"; Layout.preferredWidth: parent.width * 0.12; font.bold: true }
                Label { text: "↑ Sent"; Layout.preferredWidth: parent.width * 0.19; font.bold: true; horizontalAlignment: Text.AlignRight }
                Label { text: "↓ Recv"; Layout.preferredWidth: parent.width * 0.19; font.bold: true; horizontalAlignment: Text.AlignRight }
            }
        }

        ListView {
            id: processList
            Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 1
            model: ListModel { id: processModel }
            delegate: Rectangle {
                width: processList.width
                height: PlasmaCore.Theme.defaultFont.pixelSize + PlasmaCore.Units.smallSpacing * 2
                color: index % 2 === 0 ? "transparent" : PlasmaCore.Theme.backgroundColor
                opacity: 0.8
                RowLayout {
                    anchors.fill: parent; anchors.margins: PlasmaCore.Units.smallSpacing; spacing: PlasmaCore.Units.smallSpacing
                    Label {
                        text: fullRoot.formatProgram(program)
                        Layout.preferredWidth: parent.width * 0.5; elide: Text.ElideRight
                        font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.85
                    }
                    Label {
                        text: pid
                        Layout.preferredWidth: parent.width * 0.12
                        font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.75; opacity: 0.6
                    }
                    Label {
                        text: Backend.formatKbps(sentKbps)
                        Layout.preferredWidth: parent.width * 0.19; horizontalAlignment: Text.AlignRight
                        color: "#3584e4"; font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.8
                    }
                    Label {
                        text: Backend.formatKbps(recvKbps)
                        Layout.preferredWidth: parent.width * 0.19; horizontalAlignment: Text.AlignRight
                        color: "#33d17a"; font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.8
                    }
                }
            }
        }

        Label {
            id: footerLabel
            Layout.fillWidth: true
            text: fullRoot.footerText
            font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.65
            opacity: 0.5; horizontalAlignment: Text.AlignRight
        }
    }

    function updateDisplay() {
        headerSent = "↑ " + Backend.formatKbps(Backend.totalSent);
        headerRecv = "↓ " + Backend.formatKbps(Backend.totalRecv);
        maxLabel.text = "↑ max: " + Backend.formatKbps(Backend.getMaxRate());
        footerText = Backend.processes.length > 0
            ? "Last update: " + new Date(Backend.lastUpdate).toLocaleTimeString() + "  |  " + Backend.processes.length + " processes"
            : "No data — run ./start-monitoring.sh in terminal";
        processModel.clear();
        var procs = Backend.processes;
        for (var i = 0; i < procs.length; i++) {
            processModel.append({
                program: procs[i].program,
                pid: procs[i].pid,
                sentKbps: procs[i].sentKbps,
                recvKbps: procs[i].recvKbps
            });
        }
        chart.requestPaint();
    }
}
