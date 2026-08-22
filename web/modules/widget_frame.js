/* Framed widget bootstrap scripts. The parent remains the route and lifecycle owner. */

export function moduleBridgeScript(nonce) {
    return `
        (() => {
            const nonce = ${JSON.stringify(nonce)};
            let seq = 0;
            let disposed = false;
            const pending = new Map();
            const cleanup = new Set();
            const onDispose = (fn) => {
                if (typeof fn !== 'function') return;
                if (disposed) fn();
                else cleanup.add(fn);
            };
            const dispose = () => {
                if (disposed) return;
                disposed = true;
                pending.forEach(({ reject }) => reject(new Error('widget disposed')));
                pending.clear();
                cleanup.forEach((fn) => { try { fn(); } catch {} });
                cleanup.clear();
                window.removeEventListener('message', onMessage);
            };
            const onMessage = (event) => {
                if (event.source !== window.parent) return;
                const msg = event.data || {};
                if (msg.nonce !== nonce) return;
                if (msg.type === 'ouro-widget-dispose') {
                    dispose();
                    return;
                }
                if (msg.type !== 'ouro-widget-fetch-result' || disposed) return;
                const item = pending.get(msg.id);
                if (!item) return;
                pending.delete(msg.id);
                if (msg.error) {
                    item.reject(new Error(msg.error));
                    return;
                }
                item.resolve(new Response(msg.body || '', {
                    status: msg.status || 200,
                    headers: msg.headers || {},
                }));
            };
            window.addEventListener('message', onMessage);
            window.__ouroWidgetOnDispose = onDispose;
            window.fetch = (url, init = {}) => {
                const id = ++seq;
                return new Promise((resolve, reject) => {
                    if (disposed) {
                        reject(new Error('widget disposed'));
                        return;
                    }
                    pending.set(id, { resolve, reject });
                    window.parent.postMessage({
                        type: 'ouro-widget-fetch',
                        nonce,
                        id,
                        url: String(url || ''),
                        init: {
                            method: init.method || 'GET',
                            headers: init.headers || {},
                            body: init.body || null,
                        },
                    }, '*');
                });
            };
            window.OuroborosWidget = { fetch: window.fetch };
        })();
    `;
}

export function moduleResizeScript(nonce) {
    return `
        (() => {
            const root = document.getElementById('root');
            let lastHeight = 0;
            const report = () => {
                if (!root) return;
                const box = root.getBoundingClientRect();
                const body = document.body;
                const bodyTop = body?.getBoundingClientRect().top || 0;
                // The root's bottom edge includes collapsed child margins and
                // body padding that root.scrollHeight alone can omit. It also
                // avoids treating a fixed 100vh body as content when the module
                // is small.
                const bodyStyle = body ? getComputedStyle(body) : null;
                const bodyHeight = body?.scrollHeight || 0;
                const bodyClientHeight = body?.clientHeight || 0;
                const fixedViewportBody = bodyStyle
                    && Math.abs((parseFloat(bodyStyle.height) || 0) - window.innerHeight) <= 1;
                const bodyContentHeight = !fixedViewportBody || bodyHeight > bodyClientHeight + 1
                    ? bodyHeight
                    : 0;
                const contentHeight = Math.max(
                    root.scrollHeight,
                    box.height,
                    box.bottom - bodyTop,
                    bodyContentHeight,
                );
                const height = Math.ceil(contentHeight);
                if (!height || height === lastHeight) return;
                lastHeight = height;
                window.parent.postMessage({
                    type: 'ouro-widget-resize',
                    nonce: ${JSON.stringify(nonce)},
                    height,
                }, '*');
            };
            const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(report) : null;
            if (observer && root) observer.observe(root);
            const onLoad = () => report();
            window.addEventListener('load', onLoad, { once: true });
            window.__ouroWidgetOnDispose?.(() => {
                observer?.disconnect();
                window.removeEventListener('load', onLoad);
            });
            report();
        })();
    `;
}
