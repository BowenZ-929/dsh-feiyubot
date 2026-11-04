"use strict";
/**
 * 壳的浏览器半边：一套组件挂三处席位。
 *
 * - `sidebar.panellist`：侧栏入口（一行图标）；
 * - `main`：主区（key 与侧栏 id 同名，点侧栏即进主区）；
 * - `settings.section`：设置页栏目。
 *
 * 三处是同一份组件、同一份数据源：心与脑的读结果只由本模块里**一个**计时器轮询，
 * 席位再多也不各起一个计时器（dsh-qqbot 为这件事专门做了外部注入的共享轮询）。
 *
 * 魂页只是一层 iframe，指向核自己的面板页（/qqagent/panel）；心与脑是只读页，
 * 页面上没有任何写控件：没有表单、没有按钮、没有链接。
 *
 * 出模块的形态照宿主的客户端模块契约（同 dsh-world / dsh-qqbot）：
 * window.__ModuleLoader__.load({ id: <包名>, factory: (require) => exports })。
 */
/** 模块 id 必须等于包名（也等于 cordis.patch.yml 里的行名）。 */
const MODULE_ID = 'qqagent-shell';
/** 魂页 iframe 的地址：同源相对路径，由宿主半边转给核。 */
const PANEL_URL = '/qqagent/panel';
/** 心与脑的只读端点（宿主半边挂的）；页面只认这两个地址，不认核的地址。 */
const ORGAN_PATHS = { heart: '/qqagent/heart', brain: '/qqagent/brain' };
/** 3s 静默轮询：照 dsh-affinity 的节拍，成功才更新，失败只记一行。 */
const POLL_MS = 3000;
const frameStyle = {
    border: '0',
    width: '100%',
    height: '72vh',
    minHeight: '520px',
    display: 'block',
};
const pageStyle = { padding: '24px', lineHeight: '1.7' };
const titleStyle = { fontSize: '15px', fontWeight: 600, marginBottom: '6px' };
const mutedStyle = { opacity: 0.7 };
const errorStyle = { marginTop: '12px', fontSize: '12px', opacity: 0.6 };
const rowStyle = { display: 'flex', gap: '12px', padding: '3px 0' };
const keyStyle = { minWidth: '9em', opacity: 0.6 };
const loader = window.__ModuleLoader__;
if (loader !== undefined) {
    loader.load({
        id: MODULE_ID,
        factory: (require) => {
            const module = { exports: {} };
            const exports = module.exports;
            Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' });
            const react = require('react');
            const h = react.createElement;
            /**
             * 一份共享的轮询：谁先挂上谁启动计时器，最后一个卸载才停。
             * 数据只在里面换，页面只读快照——三处席位因此看到的是同一份数据。
             */
            function createOrganStore() {
                const views = {
                    heart: { data: null, errors: [] },
                    brain: { data: null, errors: [] },
                };
                const subscribers = new Set();
                let timer;
                let reading = false;
                function notify() {
                    for (const subscriber of subscribers)
                        subscriber();
                }
                async function readOne(organ) {
                    const path = ORGAN_PATHS[organ];
                    if (typeof fetch !== 'function') {
                        views[organ] = { data: views[organ].data, errors: [path + '：这个环境没有 fetch'] };
                        return;
                    }
                    try {
                        const response = await fetch(path, { cache: 'no-store', headers: { accept: 'application/json' } });
                        const body = (await response.json());
                        views[organ] = {
                            data: body?.data ?? null,
                            errors: Array.isArray(body?.errors) ? body.errors : [],
                        };
                    }
                    catch (error) {
                        const reason = error instanceof Error ? error.message : String(error);
                        views[organ] = { data: views[organ].data, errors: [path + '：读不到（' + reason + '）'] };
                    }
                }
                /** 一趟：两个端点一起拉，成功与否都通知一次（页面不闪「读取中」）。 */
                async function refresh() {
                    if (reading)
                        return;
                    reading = true;
                    try {
                        await Promise.all([readOne('heart'), readOne('brain')]);
                    }
                    finally {
                        reading = false;
                        notify();
                    }
                }
                function subscribe(subscriber) {
                    subscribers.add(subscriber);
                    if (timer === undefined) {
                        timer = setInterval(() => { void refresh(); }, POLL_MS);
                        timer.unref?.();
                        void refresh();
                    }
                    return () => {
                        subscribers.delete(subscriber);
                        if (subscribers.size === 0 && timer !== undefined) {
                            clearInterval(timer);
                            timer = undefined;
                        }
                    };
                }
                return { subscribe, view: (organ) => views[organ] };
            }
            const organs = createOrganStore();
            /** 一页只读数据：没有任何写控件，也没有任何链接。 */
            function useOrgan(organ) {
                const [view, setView] = react.useState(organs.view(organ));
                react.useEffect(() => {
                    setView(organs.view(organ));
                    return organs.subscribe(() => setView(organs.view(organ)));
                }, [organ]);
                return view;
            }
            function label(value) {
                if (value === null || value === undefined)
                    return '—';
                if (typeof value === 'string')
                    return value;
                try {
                    return JSON.stringify(value);
                }
                catch {
                    return String(value);
                }
            }
            /** 魂页：核的面板页原样内嵌。 */
            function SoulPage() {
                return h('iframe', { title: '魂', src: PANEL_URL, style: frameStyle });
            }
            /** 心、脑：核给什么就列什么（壳不解释字段含义），没给就说未接入。 */
            function organPage(organ, title) {
                return function OrganPage() {
                    const view = useOrgan(organ);
                    const rows = view.data === null ? [] : Object.keys(view.data).map((key) => h('div', { key, style: rowStyle }, h('span', { style: keyStyle }, key), h('span', null, label(view.data === null ? null : view.data[key]))));
                    return h('div', { style: pageStyle }, h('div', { style: titleStyle }, title), view.data === null ? h('div', { style: mutedStyle }, '未接入') : null, rows, view.errors.length > 0 ? h('div', { style: errorStyle }, '读不到的源：' + view.errors.join('；')) : null);
                };
            }
            function glyph(path) {
                return function Glyph(owner) {
                    const size = owner?.size ?? 16;
                    return h('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', 'aria-hidden': true, style: { display: 'block' } }, h('path', { d: path, fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, strokeLinejoin: 'round' }));
                };
            }
            /** 魂 / 心 / 脑：同一套组件挂三处席位（侧栏入口、主区、设置页）。 */
            const SEATS = [
                { id: 'qqagent-soul', order: 90, label: '魂', page: SoulPage, icon: glyph('M5 8.5a3.5 3.5 0 1 1 7 0c0 3-3.5 3-3.5 6a3.5 3.5 0 1 0 7 0v-3') },
                { id: 'qqagent-heart', order: 91, label: '心', page: organPage('heart', '心'), icon: glyph('M12 20.6 4.7 13.4a4.6 4.6 0 0 1 6.5-6.5l.8.8.8-.8a4.6 4.6 0 1 1 6.5 6.5Z') },
                { id: 'qqagent-brain', order: 92, label: '脑', page: organPage('brain', '脑'), icon: glyph('M9 5.5a2.5 2.5 0 0 1 5 0v13a2.5 2.5 0 0 1-5 0Zm5 4h3.5a2.5 2.5 0 0 1 0 5H14m-4.5-5H6a2.5 2.5 0 0 0 0 5h3.5') },
            ];
            function apply(ctx) {
                const slots = ctx.slots;
                if (slots === undefined)
                    return;
                const dispose = [];
                for (const seat of SEATS) {
                    dispose.push(slots.inject('sidebar.panellist', () => slots.register({ name: 'sidebar.panellist', id: seat.id, order: seat.order, label: seat.label }, seat.icon)));
                    dispose.push(slots.inject('main', () => slots.register({ name: 'main', key: seat.id }, seat.page)));
                    dispose.push(slots.inject('settings.section', () => slots.register({ name: 'settings.section', id: seat.id, order: seat.order, label: seat.label }, seat.page)));
                }
                ctx.effect?.(() => () => {
                    for (const off of dispose.reverse())
                        off?.();
                }, 'qqagent-shell: 席位');
            }
            exports.apply = apply;
            exports.inject = ['slots'];
            exports.name = 'qqagent-shell-client';
            return module.exports;
        },
    });
}
