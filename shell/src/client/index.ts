/**
 * 壳的浏览器半边：**一个**席位「她的面板」，挂三处槽。
 *
 * - `sidebar.panellist`：侧栏入口（一行图标）；
 * - `main`：主区（key 与侧栏 id 同名，点侧栏即进主区）；
 * - `settings.section`：设置页栏目。
 *
 * 三处是同一个组件：一层 iframe，指向核自己的聚合页。页里有一条导航条
 * （魂 / 心 / 脑 / 家 / 日子 / 聊天 / 设置）换 iframe 的地址——那是 HTML 里的事，
 * 壳不搬导航、也不判断该显示哪一页。壳里一个字都没有她的判断，只搬地址。
 *
 * 旧形态（三个席位：魂 / 心 / 脑，各自一份界面 + 壳里一份 3s 轮询）已经收掉：
 * 心与脑的页现在都在核那一侧、都在聚合页的 iframe 里，壳再轮询一遍就是第二条取数路径。
 *
 * 出模块的形态照宿主的客户端模块契约（同 dsh-world / dsh-qqbot）：
 * window.__ModuleLoader__.load({ id: <包名>, factory: (require) => exports })。
 */

type CreateElement = typeof import('react').createElement;

interface SlotRegistry {
  inject(slotName: string, register: () => () => void): () => void;
  register(spec: Record<string, unknown>, component: unknown): () => void;
}

interface ShellContext {
  slots?: SlotRegistry;
  effect?(callback: () => () => void, label?: string): unknown;
}

/** 本文件必须是脚本而不是 ES 模块：宿主按 _ModuleLoader_ 的 CJS 闭包加载它，
 * 顶上出现任何 import/export 都会让产物多一行 `export {}`，脚本加载即语法错误。 */
interface Window {
  __ModuleLoader__?: {
    load(entry: { id: string; factory: (require: (id: string) => unknown) => unknown }): void;
  };
}

interface ReactLike {
  createElement: CreateElement;
}

/** 模块 id 必须等于包名（也等于 cordis.patch.yml 里的行名）。 */
const MODULE_ID = 'qqagent-shell';
/**
 * 聚合页的地址：同源相对路径，由宿主半边转给核。
 * 壳的 /qqagent/panel 前缀要剥掉才到核，所以核的 /panel（聚合页）在壳里是
 * /qqagent/panel/panel——多出来的那一段就是核上的 /panel。
 * 文档路径 /qqagent/panel/panel 的目录是 /qqagent/panel/，页里那些相对地址（heart、
 * panel-base.js、./）因此都落在这个前缀下，壳剥前缀后正好是核上的 /heart、
 * /panel-base.js、/。直开核时同一个文件的文档在 /panel，相对地址同样成立。
 */
const PANEL_URL = '/qqagent/panel/panel';

const frameStyle = {
  border: '0',
  width: '100%',
  height: '72vh',
  minHeight: '520px',
  display: 'block',
} as const;

const loader = window.__ModuleLoader__;
if (loader !== undefined) {
  loader.load({
    id: MODULE_ID,
    factory: (require: (id: string) => unknown) => {
      const module = { exports: {} as Record<string, unknown> };
      const exports = module.exports;
      Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' });
      const react = require('react') as ReactLike;
      const h = react.createElement;

      function glyph(path: string) {
        return function Glyph(owner?: { size?: number }) {
          const size = owner?.size ?? 16;
          return h(
            'svg',
            { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', 'aria-hidden': true, style: { display: 'block' } },
            h('path', { d: path, fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, strokeLinejoin: 'round' }),
          );
        };
      }

      /** 她的面板：只有一层 iframe。导航、取数、判据全在核那一侧的页里。 */
      function PanelPage() {
        return h('iframe', { title: '她的面板', src: PANEL_URL, style: frameStyle });
      }

      /**
       * 一个席位，三处槽。
       * 以前是三个席位（魂 / 心 / 脑），一处一页；收成一个之后，页与页之间的切换
       * 归页里的导航条管，不再变成 DSH 侧栏里的一行——否则每加一页就得加一个席位。
       */
      const SEAT = { id: 'qqagent-panel', order: 90, label: '她的面板' };

      function apply(ctx: ShellContext): void {
        const slots = ctx.slots;
        if (slots === undefined) return;
        const dispose: Array<() => void> = [];
        const icon = glyph('M4 4h7v7H4Zm9 0h7v7h-7ZM4 13h7v7H4Zm9 0h7v7h-7Z');
        dispose.push(slots.inject('sidebar.panellist', () => slots.register(
          { name: 'sidebar.panellist', id: SEAT.id, order: SEAT.order, label: SEAT.label },
          icon,
        )));
        dispose.push(slots.inject('main', () => slots.register(
          { name: 'main', key: SEAT.id },
          PanelPage,
        )));
        dispose.push(slots.inject('settings.section', () => slots.register(
          { name: 'settings.section', id: SEAT.id, order: SEAT.order, label: SEAT.label },
          PanelPage,
        )));
        ctx.effect?.(() => () => {
          for (const off of dispose.reverse()) off?.();
        }, 'qqagent-shell: 席位');
      }

      exports.apply = apply;
      exports.inject = ['slots'];
      exports.name = 'qqagent-shell-client';
      return module.exports;
    },
  });
}
