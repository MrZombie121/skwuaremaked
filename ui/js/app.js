/**
 * SKYWATCH C4ISR — Military Tactical Air Threat Radar Engine v2.0
 * High-performance 60 FPS kinematic flight engine, multi-target tracking,
 * hazard danger cones, orbital loiter mechanics, and real-time Telethon feeds.
 */

// Global State Variables (Declared first to avoid ReferenceErrors)
let map = null;
let baseLayers = {};
let currentBaseLayer = 'dark';
let markersMap = new Map();
let activeTargets = [];
let currentFilter = "ALL";
let isAudioEnabled = true;
let audioVolume = 0.7;
let selectedTargetId = null;
let followTargetId = null;
let ws = null;
let logCount = 0;
let channelsList = [];
let lastAnimTimestamp = performance.now();
let rangeRingsLayers = [];
let simulatorActive = false;
let neptunActive = false;

let showHazardCones = true;
let showRangeRings = true;
let showTrails = true;
let showVectors = true;
let speedMultiplier = 1.0;
let soundMissileEnabled = true;
let soundUavEnabled = true;
let soundKillEnabled = true;
let isWsConnected = false;
let pollingInterval = null;

const USER_PREFS_KEY = "skywatch_user_prefs_v2";

// Web Audio Alert Synthesizer
class RadarSoundFx {
    constructor() {
        this.ctx = null;
    }

    init() {
        if (!this.ctx) {
            try {
                const AudioCtx = window.AudioContext || window.webkitAudioContext;
                if (AudioCtx) this.ctx = new AudioCtx();
            } catch (e) {}
        }
    }

    playTone(frequency = 880, duration = 0.15, type = "sine", gainVal = 0.15) {
        if (!isAudioEnabled) return;
        try {
            this.init();
            if (!this.ctx) return;
            if (this.ctx.state === 'suspended') {
                this.ctx.resume();
            }
            const osc = this.ctx.createOscillator();
            const gain = this.ctx.createGain();

            osc.type = type;
            osc.frequency.setValueAtTime(frequency, this.ctx.currentTime);
            osc.frequency.exponentialRampToValueAtTime(frequency * 1.4, this.ctx.currentTime + duration);

            const finalGain = gainVal * audioVolume;
            gain.gain.setValueAtTime(finalGain, this.ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, this.ctx.currentTime + duration);

            osc.connect(gain);
            gain.connect(this.ctx.destination);

            osc.start();
            osc.stop(this.ctx.currentTime + duration);
        } catch (e) {
            console.warn("Audio error:", e);
        }
    }

    playNewThreatAlarm(targetType = "SHAHED") {
        if (targetType === "BALLISTIC" || targetType === "MISSILE" || targetType === "KAB") {
            if (!soundMissileEnabled) return;
            this.playTone(950, 0.18, "sawtooth", 0.2);
            setTimeout(() => this.playTone(1200, 0.22, "sawtooth", 0.25), 140);
        } else if (targetType === "JET_UAV") {
            if (!soundUavEnabled) return;
            this.playTone(740, 0.12, "triangle", 0.18);
            setTimeout(() => this.playTone(880, 0.15, "triangle", 0.2), 100);
        } else {
            if (!soundUavEnabled) return;
            this.playTone(587, 0.12, "sine", 0.15);
            setTimeout(() => this.playTone(880, 0.16, "sine", 0.18), 120);
        }
    }

    playNeutralizedSound() {
        if (!soundKillEnabled) return;
        this.playTone(880, 0.08, "sine", 0.12);
        setTimeout(() => this.playTone(440, 0.15, "sine", 0.1), 90);
    }
}

const soundFx = new RadarSoundFx();

// 1. Initialize Leaflet Map
function initMap() {
    const mapEl = document.getElementById('map');
    if (!mapEl) return;

    const ukraineBounds = [
        [43.8, 21.5],
        [52.8, 40.5]
    ];

    map = L.map('map', {
        center: [48.3794, 31.1656],
        zoom: 6.5,
        minZoom: 5.0,
        maxZoom: 14,
        maxBounds: ukraineBounds,
        maxBoundsViscosity: 0.9,
        zoomControl: false
    });

    L.control.zoom({ position: 'topright' }).addTo(map);

    // Google Maps Dark Tactical Hybrid
    baseLayers.dark = L.tileLayer('https://mt{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}&hl=uk', {
        attribution: '&copy; Google Maps',
        subdomains: ['0', '1', '2', '3'],
        className: 'google-dark-tactical',
        maxZoom: 20
    });

    // Google Maps Satellite Imagery
    baseLayers.satellite = L.tileLayer('https://mt{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}&hl=uk', {
        attribution: '&copy; Google Maps Satellite Imagery',
        subdomains: ['0', '1', '2', '3'],
        maxZoom: 20
    });

    // Google Maps Terrain / Relief
    baseLayers.contrast = L.tileLayer('https://mt{s}.google.com/vt/lyrs=p&x={x}&y={y}&z={z}&hl=uk', {
        attribution: '&copy; Google Maps Terrain',
        subdomains: ['0', '1', '2', '3'],
        className: 'google-dark-tactical',
        maxZoom: 20
    });

    // Esri Dark Canvas
    baseLayers.esriDark = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
        attribution: '&copy; Esri &copy; DeLorme, HERE',
        maxZoom: 16
    });

    if (baseLayers[currentBaseLayer]) {
        baseLayers[currentBaseLayer].addTo(map);
    } else {
        baseLayers.dark.addTo(map);
    }

    createRangeRings();

    map.on('mousemove', (e) => {
        const coordsEl = document.getElementById('cursor-coords');
        if (coordsEl) {
            coordsEl.innerText = `LAT: ${e.latlng.lat.toFixed(4)} | LON: ${e.latlng.lng.toFixed(4)} | ZOOM: ${map.getZoom().toFixed(1)}`;
        }
    });

    requestAnimationFrame(flightAnimationLoop);
}

function switchBaseLayer(layerKey) {
    if (!map) return;
    if (baseLayers[currentBaseLayer]) {
        map.removeLayer(baseLayers[currentBaseLayer]);
    }
    if (baseLayers[layerKey]) {
        baseLayers[layerKey].addTo(map);
        currentBaseLayer = layerKey;
    }
}

function createRangeRings() {
    if (!map) return;
    const hubs = [
        [50.4501, 30.5234], // Kyiv
        [46.4825, 30.7233], // Odesa
        [47.8388, 35.1396], // Zaporizhzhia
        [48.4647, 35.0462], // Dnipro
        [49.9935, 36.2304]  // Kharkiv
    ];

    hubs.forEach(center => {
        [50000, 100000, 150000].forEach((radius) => {
            const ring = L.circle(center, {
                radius: radius,
                color: 'rgba(0, 229, 255, 0.15)',
                weight: 1,
                dashArray: '2, 6',
                fill: false,
                interactive: false
            });
            rangeRingsLayers.push(ring);
            if (showRangeRings && map) ring.addTo(map);
        });
    });
}

function toggleRangeRings(enable) {
    showRangeRings = enable;
    if (!map) return;
    rangeRingsLayers.forEach(layer => {
        if (enable) layer.addTo(map);
        else map.removeLayer(layer);
    });
}

// 2. Custom Marker Generator (Individual Unstacked Markers)
function createCustomMarkerIcon(target) {
    let iconFile = '/markers/shahed.png';
    switch (target.target_type) {
        case 'AIRCRAFT': iconFile = '/markers/aircraft.png'; break;
        case 'JET_UAV': iconFile = '/markers/rs.png'; break;
        case 'MISSILE': iconFile = '/markers/missile.png'; break;
        case 'BALLISTIC': iconFile = '/markers/ballistic.png'; break;
        case 'KAB': iconFile = '/markers/kab.png'; break;
        case 'RECON': iconFile = '/markers/recon.png'; break;
        case 'FPV': iconFile = '/markers/fpv.png'; break;
        case 'DECOY': iconFile = '/markers/decoy.png'; break;
        default: iconFile = '/markers/shahed.png'; break;
    }

    const rotation = (target.heading_deg !== undefined && target.heading_deg !== null) ? target.heading_deg : 0;
    const circlingClass = target.is_circling ? 'circling-target' : '';

    const html = `
        <div class="radar-threat-icon ${circlingClass}" style="transform: rotate(${rotation}deg);">
            <img src="${iconFile}" class="threat-img" alt="${target.target_type}">
        </div>
    `;

    return L.divIcon({
        className: 'custom-radar-marker',
        html: html,
        iconSize: [46, 46],
        iconAnchor: [23, 23]
    });
}

function calculateProjectedPoint(lat, lon, bearingDeg, distanceKm = 25) {
    const r = 6371;
    const d = distanceKm / r;
    const radBearing = (bearingDeg * Math.PI) / 180;
    const radLat = (lat * Math.PI) / 180;
    const radLon = (lon * Math.PI) / 180;

    const lat2 = Math.asin(
        Math.sin(radLat) * Math.cos(d) +
        Math.cos(radLat) * Math.sin(d) * Math.cos(radBearing)
    );
    const lon2 = radLon + Math.atan2(
        Math.sin(radBearing) * Math.sin(d) * Math.cos(radLat),
        Math.cos(d) - Math.sin(radLat) * Math.sin(lat2)
    );

    return [(lat2 * 180) / Math.PI, (lon2 * 180) / Math.PI];
}

// 3. Client-Side Continuous 60 FPS Kinematics & Circling Animation Engine
function flightAnimationLoop(currentTimestamp) {
    const dtSeconds = Math.min((currentTimestamp - lastAnimTimestamp) / 1000.0, 0.1);
    lastAnimTimestamp = currentTimestamp;
    const nowEpoch = Date.now() / 1000.0;

    if (dtSeconds > 0 && map) {
        for (let [tid, obj] of markersMap.entries()) {
            if (!obj.targetData || !obj.marker) continue;

            const target = obj.targetData;

            // CIRCLING MODE
            if (target.is_circling) {
                const startTime = target.circling_start || nowEpoch;
                const timeCircling = nowEpoch - startTime;
                const orbitAngle = (2.0 * Math.PI / 2.4) * timeCircling;
                const destLat = target.dest_lat || obj.currentPos[0];
                const destLon = target.dest_lon || obj.currentPos[1];
                const orbitRadiusKm = 0.65;

                const dLat = (orbitRadiusKm / 111.0) * Math.cos(orbitAngle);
                const dLon = (orbitRadiusKm / (111.0 * Math.cos((destLat * Math.PI) / 180.0))) * Math.sin(orbitAngle);

                obj.currentPos = [destLat + dLat, destLon + dLon];
                obj.marker.setLatLng(obj.currentPos);

                const tangentHeading = ((orbitAngle + (Math.PI / 2.0)) * 180.0 / Math.PI + 360.0) % 360.0;
                const iconEl = obj.marker.getElement();
                if (iconEl) {
                    const iconInner = iconEl.querySelector('.radar-threat-icon');
                    if (iconInner) iconInner.style.transform = `rotate(${tangentHeading}deg)`;
                }

                if (obj.vectorLine) {
                    const forwardPoint = calculateProjectedPoint(obj.currentPos[0], obj.currentPos[1], tangentHeading, 12);
                    obj.vectorLine.setLatLngs([obj.currentPos, forwardPoint]);
                }

                if (followTargetId === tid) {
                    map.panTo(obj.currentPos, { animate: false });
                }
                continue;
            }

            // LINEAR FLIGHT MODE (Smooth Glide with Dead-Reckoning & LERP Fusion)
            const headingDeg = (target.heading_deg !== undefined && target.heading_deg !== null) ? target.heading_deg : 315.0;
            const speedKmh = (target.speed_kmh || 185.0) * speedMultiplier;

            const speedKms = speedKmh / 3600.0;
            const distKm = speedKms * dtSeconds;
            const rad = (headingDeg * Math.PI) / 180.0;

            const dLat = (distKm / 111.0) * Math.cos(rad);
            const dLon = (distKm / (111.0 * Math.cos((obj.currentPos[0] * Math.PI) / 180.0))) * Math.sin(rad);

            // Smoothly converge towards server ground-truth GPS coordinates without snapping
            if (obj.serverPos) {
                const lerpWeight = Math.min(dtSeconds * 2.0, 0.15);
                obj.currentPos[0] += (obj.serverPos[0] - obj.currentPos[0]) * lerpWeight + dLat * (1.0 - lerpWeight);
                obj.currentPos[1] += (obj.serverPos[1] - obj.currentPos[1]) * lerpWeight + dLon * (1.0 - lerpWeight);
                obj.serverPos[0] += dLat;
                obj.serverPos[1] += dLon;
            } else {
                obj.currentPos[0] += dLat;
                obj.currentPos[1] += dLon;
            }

            obj.marker.setLatLng(obj.currentPos);

            const iconEl = obj.marker.getElement();
            if (iconEl) {
                const iconInner = iconEl.querySelector('.radar-threat-icon');
                if (iconInner) iconInner.style.transform = `rotate(${headingDeg}deg)`;
            }

            if (obj.vectorLine) {
                const forwardPoint = calculateProjectedPoint(obj.currentPos[0], obj.currentPos[1], headingDeg, 25);
                obj.vectorLine.setLatLngs([obj.currentPos, forwardPoint]);
            }

            if (followTargetId === tid) {
                map.panTo(obj.currentPos, { animate: false });
            }
        }
    }

    requestAnimationFrame(flightAnimationLoop);
}

// 4. Render Targets on Map
function renderTargetsOnMap(targets) {
    if (!map) return;
    const currentIds = new Set(targets.map(t => t.target_id));

    for (let [tid, obj] of markersMap.entries()) {
        if (!currentIds.has(tid)) {
            if (obj.marker) map.removeLayer(obj.marker);
            if (obj.vectorLine) map.removeLayer(obj.vectorLine);
            if (obj.trajectoryLine) map.removeLayer(obj.trajectoryLine);
            if (obj.hazardPolygon) map.removeLayer(obj.hazardPolygon);
            markersMap.delete(tid);
        }
    }

    targets.forEach(target => {
        if (currentFilter !== "ALL" && target.target_type !== currentFilter) {
            if (markersMap.has(target.target_id)) {
                const obj = markersMap.get(target.target_id);
                map.removeLayer(obj.marker);
                if (obj.vectorLine) map.removeLayer(obj.vectorLine);
                if (obj.trajectoryLine) map.removeLayer(obj.trajectoryLine);
                if (obj.hazardPolygon) map.removeLayer(obj.hazardPolygon);
                markersMap.delete(target.target_id);
            }
            return;
        }

        const serverLatLng = [target.current_lat, target.current_lon];
        const icon = createCustomMarkerIcon(target);

        let vectorLine = null;
        let trajectoryLine = null;
        let hazardPolygon = null;

        // Trajectory History Trail
        if (showTrails && target.trajectory && target.trajectory.length > 1) {
            const trailCoords = target.trajectory.map(p => [p[0], p[1]]);
            trailCoords.push(serverLatLng);
            
            const trailColor = target.target_type === 'AIRCRAFT' ? '#00e5ff' :
                               target.target_type === 'JET_UAV' ? '#00e5ff' : 
                               target.target_type === 'MISSILE' ? '#ff007f' :
                               target.target_type === 'BALLISTIC' ? '#ff2a4b' :
                               target.target_type === 'KAB' ? '#ffd600' :
                               target.target_type === 'RECON' ? '#00ff88' : '#ff9100';
            trajectoryLine = L.polyline(trailCoords, {
                color: trailColor,
                weight: 2.5,
                opacity: 0.65,
                dashArray: '3, 6',
                smoothFactor: 1.0
            });
        }

        // Heading Vector Arrow
        if (showVectors) {
            const headingDeg = (target.heading_deg !== undefined && target.heading_deg !== null) ? target.heading_deg : 315.0;
            const forwardPoint = calculateProjectedPoint(target.current_lat, target.current_lon, headingDeg, 25);
            vectorLine = L.polyline([serverLatLng, forwardPoint], {
                color: (target.target_type === 'AIRCRAFT' || target.target_type === 'JET_UAV') ? '#00e5ff' : '#ff2a4b',
                weight: 3,
                opacity: 0.85,
                dashArray: '4, 4'
            });
        }

        // Danger Hazard Fan Polygon
        if (showHazardCones && target.hazard_cone && target.hazard_cone.length > 2) {
            hazardPolygon = L.polygon(target.hazard_cone, {
                color: target.target_type === 'JET_UAV' ? 'rgba(0, 229, 255, 0.4)' : 'rgba(255, 42, 75, 0.4)',
                fillColor: target.target_type === 'JET_UAV' ? '#00e5ff' : '#ff2a4b',
                fillOpacity: 0.1,
                weight: 1,
                dashArray: '2, 4'
            });
        }

        if (markersMap.has(target.target_id)) {
            const obj = markersMap.get(target.target_id);
            obj.targetData = target;
            obj.serverPos = [serverLatLng[0], serverLatLng[1]];
            
            if (target.is_circling) {
                obj.currentPos = [serverLatLng[0], serverLatLng[1]];
                obj.marker.setLatLng(serverLatLng);
            } else {
                // If position jumped significantly (> 50km), update base
                const dLat = Math.abs(obj.currentPos[0] - serverLatLng[0]);
                const dLon = Math.abs(obj.currentPos[1] - serverLatLng[1]);
                if (dLat > 0.45 || dLon > 0.45) {
                    obj.currentPos = [serverLatLng[0], serverLatLng[1]];
                    obj.marker.setLatLng(serverLatLng);
                }
            }
            
            obj.marker.setIcon(icon);

            if (obj.vectorLine) map.removeLayer(obj.vectorLine);
            if (vectorLine) {
                vectorLine.addTo(map);
                obj.vectorLine = vectorLine;
            }

            if (obj.trajectoryLine) map.removeLayer(obj.trajectoryLine);
            if (trajectoryLine) {
                trajectoryLine.addTo(map);
                obj.trajectoryLine = trajectoryLine;
            }

            if (obj.hazardPolygon) map.removeLayer(obj.hazardPolygon);
            if (hazardPolygon) {
                hazardPolygon.addTo(map);
                obj.hazardPolygon = hazardPolygon;
            }
        } else {
            const marker = L.marker(serverLatLng, { icon: icon }).addTo(map);
            
            const destLabel = target.is_circling ? 
                ` ➔ <b>${target.destination_name} (КРУЖЛЯЄ НАД ЦІЛЛЮ)</b>` : 
                (target.destination_name ? ` ➔ ${target.destination_name}` : '');

            marker.bindTooltip(`<b>${target.target_id}</b>: ${target.target_type} (${target.current_location_name}${destLabel})`, {
                direction: 'top',
                offset: [0, -20],
                className: 'radar-tooltip'
            });

            marker.on('click', () => {
                selectTarget(target.target_id);
            });

            if (vectorLine) vectorLine.addTo(map);
            if (trajectoryLine) trajectoryLine.addTo(map);
            if (hazardPolygon) hazardPolygon.addTo(map);

            markersMap.set(target.target_id, {
                marker: marker,
                vectorLine: vectorLine,
                trajectoryLine: trajectoryLine,
                hazardPolygon: hazardPolygon,
                currentPos: [serverLatLng[0], serverLatLng[1]],
                serverPos: [serverLatLng[0], serverLatLng[1]],
                targetData: target
            });
        }
    });
}

// 5. Update UI Stats & Sidebar List
function updateUI(targets) {
    activeTargets = targets;

    const totalEl = document.getElementById('threats-count');
    const shahedEl = document.getElementById('shahed-count');
    const rsEl = document.getElementById('rs-count');
    const missileEl = document.getElementById('missile-count');
    const ballisticEl = document.getElementById('ballistic-count');
    const kabEl = document.getElementById('kab-count');
    const acEl = document.getElementById('aircraft-count');
    const mobThreatBadge = document.getElementById('mobile-threat-badge');

    if (totalEl) totalEl.innerText = targets.length;
    if (mobThreatBadge) mobThreatBadge.innerText = targets.length;
    if (shahedEl) shahedEl.innerText = targets.filter(t => t.target_type === 'SHAHED').length;
    if (rsEl) rsEl.innerText = targets.filter(t => t.target_type === 'JET_UAV').length;
    if (missileEl) missileEl.innerText = targets.filter(t => t.target_type === 'MISSILE').length;
    if (ballisticEl) ballisticEl.innerText = targets.filter(t => t.target_type === 'BALLISTIC').length;
    if (kabEl) kabEl.innerText = targets.filter(t => t.target_type === 'KAB').length;
    if (acEl) acEl.innerText = targets.filter(t => t.target_type === 'AIRCRAFT').length;

    const listContainer = document.getElementById('targets-list');
    if (!listContainer) return;

    const filtered = currentFilter === "ALL" ? targets : targets.filter(t => t.target_type === currentFilter);

    if (filtered.length === 0) {
        listContainer.innerHTML = '<div class="no-targets">НЕМАЄ АКТИВНИХ ПОВІТРЯНИХ ЗАГРОЗ У ПРОСТОРІ</div>';
        return;
    }

    let html = '';
    filtered.forEach(tgt => {
        const timeAgoSec = Math.floor((Date.now() / 1000) - tgt.last_updated);
        const headingStr = tgt.heading !== "UNKNOWN" ? `${tgt.heading} (${Math.round(tgt.heading_deg)}°)` : "UNKNOWN";
        
        let destStr = headingStr;
        if (tgt.is_circling) {
            destStr = `<span style="color:#ffd600; font-weight:800;">🔄 КРУЖЛЯЄ НАД: ${tgt.destination_name}</span>`;
        } else if (tgt.destination_name) {
            destStr = `➔ ${tgt.destination_name} (ETA: ${tgt.eta_minutes || '?'} хв)`;
        }

        html += `
            <div class="target-card type-${tgt.target_type} ${tgt.target_id === selectedTargetId ? 'selected' : ''}" onclick="selectTarget('${tgt.target_id}')">
                <div class="tgt-top">
                    <span class="tgt-id">${tgt.target_id}</span>
                    <span class="tgt-badge ${tgt.target_type}">${tgt.target_subtype || tgt.target_type}</span>
                </div>
                <div class="tgt-row">
                    <span>Пункт виявлення:</span>
                    <span class="tgt-val">${tgt.current_location_name}</span>
                </div>
                <div class="tgt-row">
                    <span>Маршрут / Ціль:</span>
                    <span class="tgt-val text-cyan" style="font-weight:700;">${destStr}</span>
                </div>
                <div class="tgt-row">
                    <span>Швидкість:</span>
                    <span class="tgt-val">${Math.round(tgt.speed_kmh)} км/год</span>
                </div>
                <div class="tgt-row">
                    <span>Джерела (${tgt.sources.length}):</span>
                    <span class="tgt-val text-orange">${tgt.sources.join(', ')}</span>
                </div>
                <div class="tgt-row">
                    <span>Оновлено:</span>
                    <span class="tgt-val">${timeAgoSec}с тому</span>
                </div>
            </div>
        `;
    });
    listContainer.innerHTML = html;

    if (selectedTargetId) {
        const tgt = targets.find(t => t.target_id === selectedTargetId);
        if (tgt) renderTargetDetail(tgt);
    }
}

// 6. Select Target & Focus Map
function selectTarget(targetId) {
    selectedTargetId = targetId;
    const tgt = activeTargets.find(t => t.target_id === targetId);
    if (!tgt || !map) return;

    renderTargetDetail(tgt);
    map.flyTo([tgt.current_lat, tgt.current_lon], 8.5, { duration: 0.8 });
    updateUI(activeTargets);
}

function renderTargetDetail(tgt) {
    const panel = document.getElementById('target-detail-panel');
    if (!panel) return;
    panel.style.display = 'block';

    const idEl = document.getElementById('det-id');
    const badgeEl = document.getElementById('det-badge');
    const typeEl = document.getElementById('det-type');
    const statusEl = document.getElementById('det-status');
    const locEl = document.getElementById('det-loc');
    const destEl = document.getElementById('det-dest');
    const headingEl = document.getElementById('det-heading');
    const speedEl = document.getElementById('det-speed');
    const distEl = document.getElementById('det-distance');
    const etaEl = document.getElementById('det-eta');

    if (idEl) idEl.innerText = tgt.target_id;
    if (badgeEl) {
        badgeEl.innerText = tgt.target_type;
        badgeEl.className = `tgt-badge ${tgt.target_type}`;
    }
    if (typeEl) typeEl.innerText = `${tgt.target_subtype || tgt.target_type}`;
    if (statusEl) {
        statusEl.innerText = tgt.is_circling ? "🔄 КРУЖЛЯЄ НАД ЦІЛЛЮ" : "АКТИВНИЙ ПОЛІТ";
        statusEl.className = tgt.is_circling ? "val text-yellow" : "val text-green";
    }
    if (locEl) locEl.innerText = `${tgt.current_location_name} [${tgt.current_lat.toFixed(2)}, ${tgt.current_lon.toFixed(2)}]`;
    if (destEl) destEl.innerText = tgt.destination_name || "Не вказано";
    if (headingEl) headingEl.innerText = `${tgt.heading} (${Math.round(tgt.heading_deg)}°)`;
    if (speedEl) speedEl.innerText = `${Math.round(tgt.speed_kmh)} км/год`;
    if (distEl) distEl.innerText = tgt.distance_to_dest_km ? `${tgt.distance_to_dest_km} км` : "N/A";
    if (etaEl) etaEl.innerText = tgt.is_circling ? "ДОСЯГНУТО" : (tgt.eta_minutes ? `~${tgt.eta_minutes} хв` : "N/A");

    const sourcesContainer = document.getElementById('det-sources-list');
    if (sourcesContainer) {
        sourcesContainer.innerHTML = tgt.sources.map(s => `<span class="src-tag">${s}</span>`).join('');
    }

    const historyContainer = document.getElementById('det-history-list');
    if (historyContainer) {
        historyContainer.innerHTML = tgt.raw_reports.map(r => `<div>• ${r}</div>`).join('');
    }
}

// 7. Append Log to Terminal
function appendTerminalLog(logData) {
    logCount++;
    const counterEl = document.getElementById('log-counter');
    const mobLogBadge = document.getElementById('mobile-log-badge');
    if (counterEl) counterEl.innerText = `${logCount} msgs`;
    if (mobLogBadge) mobLogBadge.innerText = `${logCount}`;

    const terminal = document.getElementById('terminal-logs');
    if (!terminal) return;

    const entry = document.createElement('div');
    entry.className = `log-entry ${logData.parsed ? 'parsed' : ''}`;
    entry.innerHTML = `
        <div class="log-meta">
            <span class="log-channel">${logData.channel}</span>
            <span class="log-time">${logData.time}</span>
        </div>
        <div class="log-text">${logData.text}</div>
    `;
    terminal.insertBefore(entry, terminal.firstChild);

    while (terminal.children.length > 150) {
        terminal.removeChild(terminal.lastChild);
    }
}

// LocalStorage User Preferences Manager
function saveUserPreferences() {
    try {
        const prefs = {
            isAudioEnabled: isAudioEnabled,
            audioVolume: audioVolume,
            currentBaseLayer: currentBaseLayer,
            showHazardCones: showHazardCones,
            showRangeRings: showRangeRings,
            showTrails: showTrails,
            showVectors: showVectors,
            speedMultiplier: speedMultiplier,
            soundMissileEnabled: soundMissileEnabled,
            soundUavEnabled: soundUavEnabled,
            soundKillEnabled: soundKillEnabled,
            sweepEnabled: document.getElementById('set-sweep-toggle')?.checked ?? true,
            circlingTime: document.getElementById('set-circling-time')?.value ?? "6",
            targetTtl: document.getElementById('set-target-ttl')?.value ?? "900"
        };
        localStorage.setItem(USER_PREFS_KEY, JSON.stringify(prefs));
    } catch (e) {
        console.warn("Error saving to localStorage:", e);
    }
}

function loadUserPreferences() {
    try {
        const raw = localStorage.getItem(USER_PREFS_KEY);
        if (!raw) return;
        const prefs = JSON.parse(raw);

        if (prefs.isAudioEnabled !== undefined) {
            isAudioEnabled = prefs.isAudioEnabled;
            const btnSound = document.getElementById('btn-sound');
            if (btnSound) {
                btnSound.className = `hud-btn ${isAudioEnabled ? 'active' : ''}`;
                btnSound.innerHTML = `<span class="btn-icon">${isAudioEnabled ? '🔊' : '🔇'}</span> AUDIO: ${isAudioEnabled ? 'ON' : 'OFF'}`;
            }
        }

        if (prefs.audioVolume !== undefined) {
            audioVolume = parseFloat(prefs.audioVolume);
            const volInput = document.getElementById('set-volume');
            const volPct = document.getElementById('vol-pct');
            if (volInput) volInput.value = Math.round(audioVolume * 100);
            if (volPct) volPct.innerText = `${Math.round(audioVolume * 100)}%`;
        }

        if (prefs.currentBaseLayer) {
            switchBaseLayer(prefs.currentBaseLayer);
            document.querySelectorAll('.map-layer-selector .layer-btn').forEach(b => {
                b.classList.toggle('active', b.getAttribute('data-layer') === prefs.currentBaseLayer);
            });
        }

        if (prefs.showHazardCones !== undefined) {
            showHazardCones = prefs.showHazardCones;
            const el = document.getElementById('set-cones-toggle');
            if (el) el.checked = showHazardCones;
        }

        if (prefs.showRangeRings !== undefined) {
            showRangeRings = prefs.showRangeRings;
            const el = document.getElementById('set-rings-toggle');
            if (el) el.checked = showRangeRings;
            toggleRangeRings(showRangeRings);
        }

        if (prefs.showTrails !== undefined) {
            showTrails = prefs.showTrails;
            const el = document.getElementById('set-trails-toggle');
            if (el) el.checked = showTrails;
        }

        if (prefs.showVectors !== undefined) {
            showVectors = prefs.showVectors;
            const el = document.getElementById('set-vectors-toggle');
            if (el) el.checked = showVectors;
        }

        if (prefs.speedMultiplier !== undefined) {
            speedMultiplier = parseFloat(prefs.speedMultiplier);
            const el = document.getElementById('set-speed-mult');
            if (el) el.value = String(prefs.speedMultiplier);
        }

        if (prefs.soundMissileEnabled !== undefined) {
            soundMissileEnabled = prefs.soundMissileEnabled;
            const el = document.getElementById('set-sound-missile');
            if (el) el.checked = soundMissileEnabled;
        }

        if (prefs.soundUavEnabled !== undefined) {
            soundUavEnabled = prefs.soundUavEnabled;
            const el = document.getElementById('set-sound-uav');
            if (el) el.checked = soundUavEnabled;
        }

        if (prefs.soundKillEnabled !== undefined) {
            soundKillEnabled = prefs.soundKillEnabled;
            const el = document.getElementById('set-sound-kill');
            if (el) el.checked = soundKillEnabled;
        }

        if (prefs.sweepEnabled !== undefined) {
            const overlay = document.getElementById('radar-sweep-overlay');
            const el = document.getElementById('set-sweep-toggle');
            if (el) el.checked = prefs.sweepEnabled;
            if (overlay) overlay.style.display = prefs.sweepEnabled ? 'block' : 'none';
        }

        if (prefs.circlingTime) {
            const el = document.getElementById('set-circling-time');
            if (el) el.value = prefs.circlingTime;
        }

        if (prefs.targetTtl) {
            const el = document.getElementById('set-target-ttl');
            if (el) el.value = prefs.targetTtl;
        }
    } catch (e) {
        console.warn("Error loading from localStorage:", e);
    }
}

// Modal Dialog Handlers
window.openModal = function(id) {
    const el = document.getElementById(id);
    if (el) {
        el.style.display = 'flex';
    }
};

window.closeModal = function(id) {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
};

// WebSocket Connection Manager with Resilient HTTP Fallback
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    const statusEl = document.getElementById('connection-status');
    if (statusEl) {
        statusEl.innerText = "CONNECTING...";
        statusEl.className = "stat-value text-orange";
    }

    try {
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            isWsConnected = true;
            if (statusEl) {
                statusEl.innerText = "ONLINE";
                statusEl.className = "stat-value text-green";
            }
        };

        ws.onmessage = (event) => {
            try {
                const payload = JSON.parse(event.data);
                handleServerMessage(payload);
            } catch (e) {}
        };

        ws.onclose = () => {
            isWsConnected = false;
            if (statusEl) {
                statusEl.innerText = "RECONNECTING";
                statusEl.className = "stat-value text-red";
            }
            setTimeout(connectWebSocket, 4000);
        };

        ws.onerror = () => {
            isWsConnected = false;
            try { ws.close(); } catch (_) {}
        };
    } catch (e) {
        isWsConnected = false;
    }

    // Fallback REST polling every 2.5s for Render / Proxies
    if (!pollingInterval) {
        pollingInterval = setInterval(async () => {
            try {
                const urlParams = new URLSearchParams(window.location.search);
                const hasBypassKey = urlParams.has('key') || urlParams.has('bypass') || window.location.pathname.includes('test-radar');

                const [tResp, mResp, lResp] = await Promise.all([
                    fetch('/api/targets'),
                    fetch('/api/maintenance/status'),
                    fetch('/api/logs?limit=150')
                ]);
                
                if (mResp.ok && !hasBypassKey) {
                    const mData = await mResp.json();
                    if (mData.maintenance_mode) {
                        window.location.reload();
                        return;
                    }
                }

                if (tResp.ok) {
                    const tData = await tResp.json();
                    if (tData.targets) {
                        renderTargetsOnMap(tData.targets);
                        updateUI(tData.targets);
                        if (!isWsConnected && statusEl) {
                            statusEl.innerText = "ONLINE";
                            statusEl.className = "stat-value text-green";
                        }
                    }
                }

                if (lResp.ok && logCount === 0) {
                    const lData = await lResp.json();
                    if (lData.logs && lData.logs.length > 0) {
                        renderInitialLogs(lData.logs);
                    }
                }
            } catch (_) {}
        }, 2500);
    }
}

function renderInitialLogs(logs) {
    if (!Array.isArray(logs) || logs.length === 0) return;
    const terminal = document.getElementById('terminal-logs');
    if (!terminal) return;

    terminal.innerHTML = '';
    logCount = logs.length;
    const counterEl = document.getElementById('log-counter');
    const mobLogBadge = document.getElementById('mobile-log-badge');
    if (counterEl) counterEl.innerText = `${logCount} msgs`;
    if (mobLogBadge) mobLogBadge.innerText = `${logCount}`;

    logs.forEach(log => {
        const entry = document.createElement('div');
        entry.className = `log-entry ${log.is_threat ? 'parsed' : ''}`;
        const timeStr = log.timestamp ? new Date(log.timestamp * 1000).toLocaleTimeString('uk-UA', { timeZone: 'Europe/Kyiv' }) : 'LOG';
        entry.innerHTML = `
            <div class="log-meta">
                <span class="log-channel">${log.channel_title || 'Telegram'}</span>
                <span class="log-time">${timeStr}</span>
            </div>
            <div class="log-text">${log.raw_text || ''}</div>
        `;
        terminal.appendChild(entry);
    });
}

function handleServerMessage(msg) {
    if (!msg) return;
    switch (msg.type) {
        case "INITIAL_STATE":
            if (msg.data && msg.data.targets) {
                renderTargetsOnMap(msg.data.targets);
                updateUI(msg.data.targets);
            }
            if (msg.data && msg.data.logs) {
                renderInitialLogs(msg.data.logs);
            }
            break;

        case "TARGETS_UPDATE":
            if (msg.data && msg.data.targets) {
                renderTargetsOnMap(msg.data.targets);
                updateUI(msg.data.targets);
            }
            if (msg.data && msg.data.is_new) {
                soundFx.playNewThreatAlarm();
            }
            if (msg.data && msg.data.neutralized_id) {
                soundFx.playNeutralizedSound();
            }
            break;

        case "KINEMATIC_TICK":
            if (msg.data && msg.data.targets) {
                renderTargetsOnMap(msg.data.targets);
                updateUI(msg.data.targets);
            }
            break;

        case "RAW_LOG":
            if (msg.data) appendTerminalLog(msg.data);
            break;
    }
}

// Setup Event Listeners
function setupEventListeners() {
    // Audio Toggle
    const btnSound = document.getElementById('btn-sound');
    btnSound?.addEventListener('click', () => {
        isAudioEnabled = !isAudioEnabled;
        btnSound.className = `hud-btn ${isAudioEnabled ? 'active' : ''}`;
        btnSound.innerHTML = `<span class="btn-icon">${isAudioEnabled ? '🔊' : '🔇'}</span> AUDIO: ${isAudioEnabled ? 'ON' : 'OFF'}`;
        if (isAudioEnabled) soundFx.playTone(880, 0.1);
        saveUserPreferences();
    });

    // Modals
    document.getElementById('btn-settings-modal')?.addEventListener('click', () => openModal('modal-settings'));

    // Clear All
    document.getElementById('btn-clear')?.addEventListener('click', async () => {
        try {
            await fetch('/api/clear', { method: 'POST' });
            selectedTargetId = null;
            followTargetId = null;
            const panel = document.getElementById('target-detail-panel');
            if (panel) panel.style.display = 'none';
        } catch (e) {}
    });

    // Close Detail
    document.getElementById('btn-close-detail')?.addEventListener('click', () => {
        selectedTargetId = null;
        followTargetId = null;
        const panel = document.getElementById('target-detail-panel');
        if (panel) panel.style.display = 'none';
    });

    // Follow Target
    document.getElementById('btn-follow-target')?.addEventListener('click', () => {
        const btn = document.getElementById('btn-follow-target');
        if (followTargetId === selectedTargetId) {
            followTargetId = null;
            btn?.classList.remove('active');
            if (btn) btn.innerText = "🎯 СТЕЖИТИ";
        } else {
            followTargetId = selectedTargetId;
            btn?.classList.add('active');
            if (btn) btn.innerText = "📍 СТЕЖИТЬСЯ";
        }
    });

    // Manual Neutralize
    document.getElementById('btn-kill-target')?.addEventListener('click', async () => {
        if (!selectedTargetId) return;
        try {
            await fetch(`/api/targets/${selectedTargetId}/neutralize`, { method: 'POST' });
            selectedTargetId = null;
            followTargetId = null;
            const panel = document.getElementById('target-detail-panel');
            if (panel) panel.style.display = 'none';
        } catch (e) {}
    });

    // Clear Terminal
    document.getElementById('btn-clear-terminal')?.addEventListener('click', () => {
        const terminal = document.getElementById('terminal-logs');
        if (terminal) terminal.innerHTML = '';
        logCount = 0;
        const cnt = document.getElementById('log-counter');
        if (cnt) cnt.innerText = '0 msgs';
    });

    // Filter Pills
    document.querySelectorAll('.filter-pills .pill').forEach(pill => {
        pill.addEventListener('click', () => {
            document.querySelectorAll('.filter-pills .pill').forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            currentFilter = pill.getAttribute('data-filter') || "ALL";
            renderTargetsOnMap(activeTargets);
            updateUI(activeTargets);
        });
    });

    // Mobile Bottom Navigation Tabs Handler
    document.querySelectorAll('.mobile-bottom-nav .mobile-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tab = btn.getAttribute('data-tab');
            if (tab) {
                document.querySelectorAll('.mobile-bottom-nav .mobile-tab-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                document.body.setAttribute('data-mobile-view', tab);
                if (tab === 'radar' && map) {
                    setTimeout(() => map.invalidateSize(), 50);
                }
            }
        });
    });

    document.getElementById('btn-mobile-settings')?.addEventListener('click', () => {
        openModal('modal-settings');
    });

    // Layer Switcher Buttons
    document.querySelectorAll('.map-layer-selector .layer-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.map-layer-selector .layer-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            switchBaseLayer(btn.getAttribute('data-layer'));
            saveUserPreferences();
        });
    });

    // Tactical Settings Handlers
    document.getElementById('set-sweep-toggle')?.addEventListener('change', (e) => {
        const overlay = document.getElementById('radar-sweep-overlay');
        if (overlay) overlay.style.display = e.target.checked ? 'block' : 'none';
        saveUserPreferences();
    });

    document.getElementById('set-cones-toggle')?.addEventListener('change', (e) => {
        showHazardCones = e.target.checked;
        renderTargetsOnMap(activeTargets);
        saveUserPreferences();
    });

    document.getElementById('set-rings-toggle')?.addEventListener('change', (e) => {
        toggleRangeRings(e.target.checked);
        saveUserPreferences();
    });

    document.getElementById('set-trails-toggle')?.addEventListener('change', (e) => {
        showTrails = e.target.checked;
        renderTargetsOnMap(activeTargets);
        saveUserPreferences();
    });

    document.getElementById('set-vectors-toggle')?.addEventListener('change', (e) => {
        showVectors = e.target.checked;
        renderTargetsOnMap(activeTargets);
        saveUserPreferences();
    });

    document.getElementById('set-speed-mult')?.addEventListener('change', (e) => {
        speedMultiplier = parseFloat(e.target.value) || 1.0;
        saveUserPreferences();
    });

    document.getElementById('set-circling-time')?.addEventListener('change', () => {
        saveUserPreferences();
    });

    document.getElementById('set-target-ttl')?.addEventListener('change', () => {
        saveUserPreferences();
    });

    document.getElementById('set-volume')?.addEventListener('input', (e) => {
        audioVolume = parseFloat(e.target.value) / 100.0;
        const pctEl = document.getElementById('vol-pct');
        if (pctEl) pctEl.innerText = `${Math.round(audioVolume * 100)}%`;
        saveUserPreferences();
    });

    document.getElementById('set-sound-missile')?.addEventListener('change', (e) => {
        soundMissileEnabled = e.target.checked;
        saveUserPreferences();
    });

    document.getElementById('set-sound-uav')?.addEventListener('change', (e) => {
        soundUavEnabled = e.target.checked;
        saveUserPreferences();
    });

    document.getElementById('set-sound-kill')?.addEventListener('change', (e) => {
        soundKillEnabled = e.target.checked;
        saveUserPreferences();
    });
}

// Live Kyiv Clock (Direct execution with fallback)
setInterval(() => {
    try {
        const now = new Date();
        const kyivTime = now.toLocaleTimeString('uk-UA', { timeZone: 'Europe/Kyiv' });
        const clockEl = document.getElementById('kyiv-clock');
        if (clockEl) clockEl.innerText = `${kyivTime} KYIV`;
    } catch (e) {}
}, 1000);

// Safe Bootstrap Sequence
function bootstrapApp() {
    try {
        const urlParams = new URLSearchParams(window.location.search);
        const hasBypassKey = urlParams.has('key') || urlParams.has('bypass') || window.location.pathname.includes('test-radar');
        if (hasBypassKey) {
            const badge = document.getElementById('admin-test-badge');
            if (badge) badge.style.display = 'inline-flex';
        }

        initMap();
        loadUserPreferences();
        setupEventListeners();
        connectWebSocket();
    } catch (e) {
        console.error("Fatal bootstrap error:", e);
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrapApp);
} else {
    bootstrapApp();
}
