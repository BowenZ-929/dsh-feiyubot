/**
 * 面板公共片段：MOUNT / BASE 只在这里算一次，别在页里再写第二份。
 *
 * 页面用**相对**地址引进来，并声明自己是谁（名字 = 它在核上的路由去掉斜杠）：
 *     <script src="panel-base.js" data-page="heart"></script>
 *
 * 给出两样东西：
 *   window.PANEL_BASE   本页的挂载前缀（直开核时常常是空串）
 *   window.panelFetch(path, init)   取数一律走它：内部 fetch(PANEL_BASE + path, init)
 *
 * 为什么相对地址在两种打开方式下都对：
 *   直开核：  文档在 /heart              → 脚本地址 /panel-base.js
 *   壳 iframe：文档在 /qqagent/panel/heart → 脚本地址 /qqagent/panel/panel-base.js，
 *              壳把 /qqagent/panel 前缀剥掉，核看到的仍是 /panel-base.js
 *
 * BASE 的算法：把 pathname 末尾的「/＋本页名字」剥掉，剥不到就原样留着。
 *   直开核     /heart               → BASE ""              → panelFetch("/heart/mood") 打到 /heart/mood
 *   壳 iframe  /qqagent/panel/heart → BASE "/qqagent/panel" → 打到 /qqagent/panel/heart/mood，壳剥前缀后是 /heart/mood
 * 魂页挂在根上，两种方式分别是 "" 与 /qqagent/panel；/day、/settings 这类没有子路径的页同理。
 * 剥的是「本页名字」这个后缀而不是「最后一段」——名字自带，页面说什么就是什么，不猜。
 *
 * 一条教训（心页三处取数打错那次）：/heart 是**页面本身**（HTML），JSON 概览在 /heart/overview。
 * 前缀与路径分开算，这两件事就不会再混成一件。
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var page = script ? (script.getAttribute("data-page") || "") : "";
  var mount = location.pathname.replace(/\/+$/, "");
  var suffix = page ? "/" + page : "";
  var base = suffix && mount.slice(-suffix.length) === suffix ? mount.slice(0, -suffix.length) : mount;

  window.PANEL_BASE = base;
  window.panelFetch = function (path, init) {
    return fetch(base + path, init);
  };
}());
