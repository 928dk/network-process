import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import org.kde.plasma.core as PlasmaCore
import "../code/backend.js" as Backend

Item {
    id: compactRoot
    implicitWidth: PlasmaCore.Units.gridUnit * 8
    implicitHeight: PlasmaCore.Units.gridUnit * 2

    property string sentText: "↑0k"
    property string recvText: "↓0k"
    property int sparklineWidth: compactRoot.width - totalText.implicitWidth - PlasmaCore.Units.smallSpacing * 3

    RowLayout {
        anchors.fill: parent
        anchors.margins: PlasmaCore.Units.smallSpacing
        spacing: PlasmaCore.Units.smallSpacing

        Canvas {
            id: sparkline
            Layout.preferredWidth: compactRoot.sparklineWidth
            Layout.fillHeight: true
            antialiasing: true
            onPaint: {
                var ctx = getContext("2d");
                ctx.clearRect(0, 0, width, height);
                var sentH = Backend.sentHistory;
                var recvH = Backend.recvHistory;
                if (sentH.length < 2) {
                    ctx.fillStyle = PlasmaCore.Theme.textColor;
                    ctx.font = "10px monospace";
                    ctx.textAlign = "center";
                    ctx.fillText("—", width / 2, height / 2 + 4);
                    return;
                }
                var maxRate = Backend.getMaxRate();
                var w = width, h = height;
                var stepX = w / (Math.max(sentH.length, recvH.length) - 1);
                ctx.strokeStyle = "#3584e4"; ctx.lineWidth = 1.5;
                ctx.beginPath();
                for (var i = 0; i < sentH.length; i++) {
                    var x = i * stepX, y = h - (sentH[i] / maxRate * h);
                    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
                }
                ctx.stroke();
                ctx.strokeStyle = "#33d17a";
                ctx.beginPath();
                for (var j = 0; j < recvH.length; j++) {
                    var rx = j * stepX, ry = h - (recvH[j] / maxRate * h);
                    if (j === 0) ctx.moveTo(rx, ry); else ctx.lineTo(rx, ry);
                }
                ctx.stroke();
            }
        }

        ColumnLayout {
            id: totalText
            spacing: 0
            Label {
                text: compactRoot.sentText
                color: "#3584e4"
                font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.75
                font.bold: true
            }
            Label {
                text: compactRoot.recvText
                color: "#33d17a"
                font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.75
                font.bold: true
            }
        }
    }

    function updateDisplay() {
        var s = Backend.totalSent;
        var r = Backend.totalRecv;
        sentText = "↑" + (s >= 1024 ? (s / 1024).toFixed(1) + "M" : s.toFixed(0) + "k");
        recvText = "↓" + (r >= 1024 ? (r / 1024).toFixed(1) + "M" : r.toFixed(0) + "k");
        sparkline.requestPaint();
    }
}
