/**
 * SKYWATCH C4ISR — High Performance Tactical PWA Service Worker
 * Features:
 * - Ultra-fast Map Tile Caching (Google Dark, Satellite, Terrain, Esri)
 * - Offline App Shell & Tactical HUD loading
 * - Static Assets Stale-While-Revalidate
 * - Cache Size Management & Quota Protection
 * - Network-First for Live Telemetry / API
 */

const CACHE_VERSION = 'skywatch-v2.1.2';
const STATIC_CACHE = `${CACHE_VERSION}-static`;
const TILE_CACHE = `${CACHE_VERSION}-map-tiles`;

// Maximum number of cached map tiles (prevents mobile quota overflow)
const MAX_TILES = 3000;

// Transparent 1x1 PNG for offline tile fallback
const EMPTY_TILE_PNG_BASE64 = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';

// Pre-cached core app shell assets
const PRECACHE_ASSETS = [
    '/',
    '/ui/index.html',
    '/manifest.webmanifest',
    '/ui/css/style.css',
    '/ui/js/app.js',
    '/ui/data/ukraine_oblasts.json',
    '/ui/data/ukraine_raions.json',
    '/markers/shahed.png',
    '/markers/rs.png',
    '/markers/missile.png',
    '/markers/ballistic.png',
    '/markers/kab.png',
    '/markers/aircraft.png',
    '/markers/recon.png',
    '/markers/fpv.png',
    '/markers/decoy.png',
    '/ui/icons/icon-32.png',
    '/ui/icons/icon-180.png',
    '/ui/icons/icon-192.png',
    '/ui/icons/icon-512.png',
    'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
    'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'
];

// Helper: Trim cache to limit size
async function trimCache(cacheName, maxItems) {
    try {
        const cache = await caches.open(cacheName);
        const keys = await cache.keys();
        if (keys.length > maxItems) {
            const deleteCount = keys.length - maxItems;
            for (let i = 0; i < deleteCount; i++) {
                await cache.delete(keys[i]);
            }
        }
    } catch (e) {
        console.warn('[SW] trimCache warning:', e);
    }
}

// Helper: Detect if a request is a map tile
function isMapTile(url) {
    const u = url.toLowerCase();
    return (
        // Google Maps Tiles (lyrs=m, lyrs=y, lyrs=p)
        (u.includes('google.com') && (u.includes('/vt/lyrs=') || u.includes('/vt?') || (u.includes('&x=') && u.includes('&y=')))) ||
        // Esri ArcGIS Canvas Tiles
        (u.includes('arcgisonline.com') && u.includes('/tile/')) ||
        // OpenStreetMap / Carto / Stamen style tiles
        u.includes('/tiles/') ||
        (u.includes('/tile/') && (u.endsWith('.png') || u.endsWith('.jpg') || u.endsWith('.jpeg')))
    );
}

// 1. Install Event: Cache app shell
self.addEventListener('install', (event) => {
    self.skipWaiting();
    event.waitUntil(
        caches.open(STATIC_CACHE).then(async (cache) => {
            console.log('[SW] Precaching SkyWatch App Shell...');
            for (const asset of PRECACHE_ASSETS) {
                try {
                    await cache.add(asset);
                } catch (err) {
                    console.warn('[SW] Failed to precache asset:', asset, err);
                }
            }
        })
    );
});

// 2. Activate Event: Clean up outdated caches
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then(async (keys) => {
            for (const key of keys) {
                if (key !== STATIC_CACHE && key !== TILE_CACHE) {
                    console.log('[SW] Deleting old cache:', key);
                    await caches.delete(key);
                }
            }
            await self.clients.claim();
            console.log('[SW] SkyWatch Service Worker Activated & Claimed clients.');
        })
    );
});

// 3. Fetch Event: Intelligent routing & caching
self.addEventListener('fetch', (event) => {
    const { request } = event;
    const url = new URL(request.url);

    // Skip non-GET requests
    if (request.method !== 'GET') {
        return;
    }

    // Skip WebSocket connections (FastAPI /ws)
    if (url.pathname.startsWith('/ws')) {
        return;
    }

    // Bypass API requests to ensure real-time telemetry (Network-Only)
    // /api/*, /v1/*, /health, /system-control-panel, /developers
    if (
        url.pathname.startsWith('/api/') ||
        url.pathname.startsWith('/v1/') ||
        url.pathname === '/health'
    ) {
        event.respondWith(
            fetch(request).catch(() => {
                return new Response(JSON.stringify({ error: 'offline', message: 'Немає з\'єднання з сервером' }), {
                    headers: { 'Content-Type': 'application/json' },
                    status: 503
                });
            })
        );
        return;
    }

    // Strategy A: MAP TILES (Cache-First, Network Fallback, background population)
    if (isMapTile(request.url)) {
        event.respondWith(
            caches.open(TILE_CACHE).then(async (cache) => {
                const cachedResponse = await cache.match(request);
                if (cachedResponse) {
                    return cachedResponse;
                }

                try {
                    const networkResponse = await fetch(request);
                    if (networkResponse && (networkResponse.status === 200 || networkResponse.type === 'opaque')) {
                        // Store tile in cache
                        cache.put(request, networkResponse.clone());
                        // Trim cache periodically
                        if (Math.random() < 0.05) {
                            trimCache(TILE_CACHE, MAX_TILES);
                        }
                    }
                    return networkResponse;
                } catch (networkError) {
                    // Offline fallback: try cache once more or return 1x1 transparent tile
                    if (cachedResponse) return cachedResponse;
                    return fetch(EMPTY_TILE_PNG_BASE64);
                }
            })
        );
        return;
    }

    // Strategy B: Navigation Requests (HTML Pages) -> Network-First with Cache Fallback
    if (request.mode === 'navigate') {
        event.respondWith(
            fetch(request)
                .then(async (networkResponse) => {
                    if (networkResponse && networkResponse.status === 200) {
                        const cache = await caches.open(STATIC_CACHE);
                        cache.put(request, networkResponse.clone());
                    }
                    return networkResponse;
                })
                .catch(async () => {
                    const cachedResponse = await caches.match(request);
                    if (cachedResponse) return cachedResponse;
                    // Fallback to cached index.html
                    return caches.match('/ui/index.html') || caches.match('/');
                })
        );
        return;
    }

    // Strategy C: Static Assets (CSS, JS, Fonts, Images, Markers, GeoJSON) -> Stale-While-Revalidate
    event.respondWith(
        caches.match(request).then(async (cachedResponse) => {
            const fetchPromise = fetch(request)
                .then(async (networkResponse) => {
                    if (networkResponse && (networkResponse.status === 200 || networkResponse.type === 'opaque')) {
                        const cache = await caches.open(STATIC_CACHE);
                        cache.put(request, networkResponse.clone());
                    }
                    return networkResponse;
                })
                .catch(() => null);

            return cachedResponse || (await fetchPromise);
        })
    );
});

// 4. Message Event: Cache statistics and management from client UI
self.addEventListener('message', async (event) => {
    if (!event.data || !event.data.type) return;

    if (event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    } else if (event.data.type === 'GET_CACHE_STATS') {
        try {
            const tileCache = await caches.open(TILE_CACHE);
            const tileKeys = await tileCache.keys();
            const staticCache = await caches.open(STATIC_CACHE);
            const staticKeys = await staticCache.keys();

            event.ports[0].postMessage({
                tilesCount: tileKeys.length,
                staticCount: staticKeys.length,
                version: CACHE_VERSION
            });
        } catch (e) {
            if (event.ports[0]) {
                event.ports[0].postMessage({ error: e.message });
            }
        }
    } else if (event.data.type === 'CLEAR_TILE_CACHE') {
        try {
            await caches.delete(TILE_CACHE);
            await caches.open(TILE_CACHE);
            if (event.ports[0]) {
                event.ports[0].postMessage({ status: 'ok', cleared: true });
            }
        } catch (e) {
            if (event.ports[0]) {
                event.ports[0].postMessage({ status: 'error', message: e.message });
            }
        }
    }
});
