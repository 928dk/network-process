// Backend: poll nethogs output file, parse, maintain rolling history

var MAX_HISTORY = 60;
var sentHistory = [];
var recvHistory = [];
var processes = [];
var totalSent = 0;
var totalRecv = 0;
var lastUpdate = 0;
var testMode = true;

function parseProgramPath(line) {
    var parts = line.split('\t');
    if (parts.length < 3) return null;
    var programPart = parts[0];
    var sent = parseFloat(parts[1]) || 0;
    var recv = parseFloat(parts[2]) || 0;
    var lastSlash = programPart.lastIndexOf('/');
    var secondLastSlash = programPart.lastIndexOf('/', lastSlash - 1);
    var pid = programPart.substring(secondLastSlash + 1, lastSlash);
    var uid = programPart.substring(lastSlash + 1);
    var program = programPart.substring(0, secondLastSlash);
    return {
        program: program || 'unknown',
        pid: parseInt(pid) || 0,
        uid: parseInt(uid) || 0,
        sentKbps: sent,
        recvKbps: recv,
        totalKbps: sent + recv
    };
}

function parseAll(text) {
    if (!text) return { processes: [], totalSent: 0, totalRecv: 0 };
    var blocks = text.split('Refreshing:');
    if (blocks.length < 2) return { processes: [], totalSent: 0, totalRecv: 0 };
    var latestBlock = blocks[blocks.length - 1].trim();
    var lines = latestBlock.split('\n');
    var procs = [];
    var tSent = 0;
    var tRecv = 0;
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line || line.indexOf('Refreshing') !== -1 || line.indexOf('Adding local') !== -1 ||
            line.indexOf('Ethernet link') !== -1 || line.indexOf('No PPP') !== -1 ||
            line.indexOf('Unknown connection') !== -1 || line.indexOf('PID') !== -1) {
            continue;
        }
        var proc = parseProgramPath(line);
        if (proc) {
            procs.push(proc);
            tSent += proc.sentKbps;
            tRecv += proc.recvKbps;
        }
    }
    procs.sort(function(a, b) { return b.totalKbps - a.totalKbps; });
    return { processes: procs, totalSent: tSent, totalRecv: tRecv, timestamp: new Date().getTime() };
}

function loadTestData() {
    var data = "Refreshing:\n/usr/bin/firefox/12345/1000\t0.50\t2.30\n/usr/bin/qbittorrent/419695/1000\t3.20\t1.50\nunknown TCP/0/0\t0.02\t0.05\n";
    var result = parseAll(data);
    processes = result.processes;
    totalSent = result.totalSent;
    totalRecv = result.totalRecv;
    lastUpdate = result.timestamp;
    sentHistory.push(totalSent);
    recvHistory.push(totalRecv);
    while (sentHistory.length > MAX_HISTORY) {
        sentHistory.shift();
        recvHistory.shift();
    }
}

function pollNow() {
    var doc = new XMLHttpRequest();
    try {
        doc.open('GET', 'file:///tmp/nethogs-raw.txt', false);
        doc.send();
        var status = doc.status;
        if (status === 0 || status === 200) {
            var text = doc.responseText;
            if (text && text.length > 0) {
                var result = parseAll(text);
                processes = result.processes;
                totalSent = result.totalSent;
                totalRecv = result.totalRecv;
                lastUpdate = result.timestamp;
                sentHistory.push(totalSent);
                recvHistory.push(totalRecv);
                while (sentHistory.length > MAX_HISTORY) {
                    sentHistory.shift();
                    recvHistory.shift();
                }
                return true;
            }
        }
    } catch (e) {
        // File read failed
    }
    return false;
}

function getMaxRate() {
    var max = 0;
    for (var i = 0; i < sentHistory.length; i++) {
        if (sentHistory[i] > max) max = sentHistory[i];
        if (recvHistory[i] > max) max = recvHistory[i];
    }
    return max || 1;
}

function formatKbps(kbps) {
    if (kbps >= 1024) {
        return (kbps / 1024).toFixed(1) + ' MB/s';
    } else if (kbps >= 1) {
        return kbps.toFixed(1) + ' KB/s';
    } else {
        return (kbps * 1024).toFixed(0) + ' B/s';
    }
}
