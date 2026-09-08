/**
 * SKYWATCH Embedded Map Widget SDK v2.1
 * Drop-in one-line radar map integration for external websites.
 * Usage:
 *   <div id="skywatch-radar" style="width: 100%; height: 550px;"></div>
 *   <script src="https://ua-skywatch.pp.ua/embed/skywatch-widget.js" 
 *           data-api-key="sk-live-..." 
 *           data-target="skywatch-radar" 
 *           data-theme="dark" 
 *           data-hud="true"></script>
 */
(function() {
    function initSkywatchWidget() {
        const scripts = document.querySelectorAll('script[src*="skywatch-widget.js"]');
        const currentScript = scripts[scripts.length - 1];
        if (!currentScript) return;

        const apiKey = currentScript.getAttribute('data-api-key') || '';
        const targetId = currentScript.getAttribute('data-target') || 'skywatch-radar';
        const theme = currentScript.getAttribute('data-theme') || 'dark';
        const showHud = currentScript.getAttribute('data-hud') || 'false';
        const height = currentScript.getAttribute('data-height') || '550px';
        const origin = currentScript.src.replace(/\/embed\/skywatch-widget\.js.*$/, '');

        let targetContainer = document.getElementById(targetId);
        if (!targetContainer) {
            targetContainer = document.createElement('div');
            targetContainer.id = targetId;
            targetContainer.style.width = '100%';
            targetContainer.style.height = height;
            currentScript.parentNode.insertBefore(targetContainer, currentScript.nextSibling);
        }

        const iframe = document.createElement('iframe');
        iframe.src = `${origin}/embed/map?api_key=${encodeURIComponent(apiKey)}&theme=${encodeURIComponent(theme)}&hud=${encodeURIComponent(showHud)}`;
        iframe.style.width = '100%';
        iframe.style.height = '100%';
        iframe.style.border = '1px solid #1a2738';
        iframe.style.borderRadius = '6px';
        iframe.style.overflow = 'hidden';
        iframe.setAttribute('allowfullscreen', 'true');
        iframe.setAttribute('loading', 'lazy');

        targetContainer.innerHTML = '';
        targetContainer.appendChild(iframe);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initSkywatchWidget);
    } else {
        initSkywatchWidget();
    }
})();
