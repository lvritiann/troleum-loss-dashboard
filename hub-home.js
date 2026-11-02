/* ============================================================================
 * HUB-HOME v1  —  综合管理平台「返回首页」通用导航
 * ----------------------------------------------------------------------------
 * 用法：在页面 </body> 之前加一行
 *     <script src="hub-home.js" defer></script>
 *
 * 设计约束（改动前请先读完）：
 *   1. 自包含：样式用 JS 注入，不依赖各站点现有 CSS 变量，避免互相污染。
 *   2. 幂等：重复加载/多份引用只渲染一次。
 *   3. 不拦截：只在自身矩形内接收点击，绝不覆盖整页，不阻断站点原有交互。
 *   4. 深色底 + 浅金字，在明/暗两种主题的站点上都可读（靠自身深色底衬托，
 *      不依赖站点背景色）。
 *   5. 可覆盖：站点若需换位置，在引入前设置 window.HUB_HOME_POS = 'bl'；
 *      若要换目标地址，设置 window.HUB_HOME_URL。
 *
 * ★ 硬约束（2026-11-02 用户明确要求）：不得影响桌面端现有访问效果。
 *   电脑端（鼠标设备 / 宽屏 >1024px）不注入任何 DOM、样式或事件，
 *   看到的布局、样式、交互、页面结构与改造前完全一致。
 *   要撤销全部注入，跑 _hubnav/rollback.py。
 * ========================================================================== */
(function () {
  'use strict';

  var HUB = window.HUB_HOME_URL || 'https://lvritiann.github.io/hub/';
  var POS = window.HUB_HOME_POS || 'tr';   // tr 右上 / tl 左上 / bl 左下 / br 右下
  var MARK = 'hubHomeBtn';

  /* 首页自身不显示按钮 */
  try {
    if (/^\/hub\/?(index\.html)?$/i.test(location.pathname)) return;
  } catch (e) { /* 某些沙箱环境取不到 pathname，忽略 */ }
  if (document.getElementById(MARK)) return;

  /* ★★★ 桌面端零改动 ★★★
   * 只有在「触屏设备」且「窄屏（≤1024px）」时才注入按钮。
   * 电脑（鼠标/触控板）访问时直接 return —— 不建节点、不插样式、不加监听、
   * 不注册任何事件，页面 DOM / CSS / 布局 / 交互与改造前完全一致。
   * 这条判断是「不影响网页版」的唯一闸门，改动前务必确认不要放宽。 */
  var coarse = false;
  try {
    coarse = window.matchMedia('(pointer: coarse)').matches;
  } catch (e) { /* 极老浏览器无 matchMedia，按桌面处理 */ }
  var vw = window.innerWidth || document.documentElement.clientWidth || 0;
  if (!coarse || vw > 1024) return;

  var CSS = ''
    + '#' + MARK + '{position:fixed;z-index:2147483000;display:inline-flex;'
    + 'align-items:center;gap:6px;height:34px;padding:0 13px 0 10px;border-radius:999px;'
    + 'background:rgba(12,16,26,.80);border:1px solid rgba(245,185,66,.42);'
    + 'color:#f5d79a;text-decoration:none;letter-spacing:.4px;white-space:nowrap;'
    + 'font:600 13px/1 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",'
    + '"Microsoft YaHei",system-ui,sans-serif;'
    + 'box-shadow:0 4px 14px rgba(0,0,0,.45),0 0 0 .5px rgba(0,0,0,.3);'
    + '-webkit-backdrop-filter:blur(10px) saturate(1.25);'
    + 'backdrop-filter:blur(10px) saturate(1.25);'
    + 'transition:background .16s ease,border-color .16s ease,transform .1s ease,opacity .3s ease;'
    + '-webkit-tap-highlight-color:transparent;-webkit-user-select:none;user-select:none;'
    + 'opacity:0}'
    + '#' + MARK + '.on{opacity:1}'
    + '#' + MARK + ':hover{background:rgba(30,42,64,.94);border-color:rgba(245,185,66,.80)}'
    + '#' + MARK + ':active{transform:scale(.94)}'
    + '#' + MARK + ':focus-visible{outline:2px solid #f5b942;outline-offset:2px}'
    + '#' + MARK + ' svg{width:15px;height:15px;flex:0 0 auto;fill:none;stroke:currentColor;'
    + 'stroke-width:2;stroke-linecap:round;stroke-linejoin:round;display:block}'
    + '#' + MARK + ' .hh-t{display:inline-block}'
    /* 窄屏收窄内边距，但保留文字 —— 只留图标会让用户猜不出是干什么的 */
    + '@media (max-width:420px){#' + MARK + '{height:32px;padding:0 11px 0 9px;font-size:12.5px;gap:5px}'
    + '#' + MARK + ' svg{width:14px;height:14px}}'
    + '@media print{#' + MARK + '{display:none!important}}';

  var SVG = '<svg viewBox="0 0 24 24" aria-hidden="true">'
    + '<path d="M3 10.2 12 3l9 7.2"/>'
    + '<path d="M5.5 9.2V20a1 1 0 0 0 1 1H10v-5.5h4V21h3.5a1 1 0 0 0 1-1V9.2"/>'
    + '</svg>';

  function place(el) {
    var s = el.style;
    var top = 'calc(env(safe-area-inset-top, 0px) + 12px)';
    var bot = 'calc(env(safe-area-inset-bottom, 0px) + 20px)';
    var side = 'calc(env(safe-area-inset-right, 0px) + 12px)';
    var sideL = 'calc(env(safe-area-inset-left, 0px) + 12px)';
    if (POS === 'tl') { s.top = top; s.left = sideL; }
    else if (POS === 'bl') { s.bottom = bot; s.left = sideL; }
    else if (POS === 'br') { s.bottom = bot; s.right = side; }
    else { s.top = top; s.right = side; }
  }

  function mount() {
    if (document.getElementById(MARK)) return;

    var st = document.createElement('style');
    st.id = MARK + '-style';
    st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);

    var a = document.createElement('a');
    a.id = MARK;
    a.href = HUB;                       // 保留真实 href：可中键/长按新开、可复制
    a.setAttribute('aria-label', '返回综合管理平台首页（导航站）');
    a.setAttribute('title', '返回综合管理平台首页');
    a.innerHTML = SVG + '<span class="hh-t">首页</span>';
    place(a);

    a.addEventListener('click', function (ev) {
      /* 让浏览器默认跳转生效即可：统一用 href，行为可预测。
         不采用 history.back() —— 跨站进入时 history 里未必有 hub，
         退错地方比多一次加载更糟。 */
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button === 1) return;
    });

    document.body.appendChild(a);
    requestAnimationFrame(function () { a.className = 'on'; });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount, { once: true });
  } else {
    mount();
  }
})();
