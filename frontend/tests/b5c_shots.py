# -*- coding: utf-8 -*-
"""B5-C 画布布局改造截图自查（Dify 布局语言）：真实登录 → 编辑器画布全幅 / 节点库悬浮
薄栏收展 / 检查器浮层卡 / 空画布引导，存 E:/learn-agnet-project/.qa_ix/wf/。
运行前提：dev server（默认 http://localhost:3275，可用 B5C_BASE 覆盖）。"""
import os

from playwright.sync_api import sync_playwright

BASE = os.environ.get('B5C_BASE', 'http://localhost:3275')
OUT = r'E:\learn-agnet-project\.qa_ix\wf'
os.makedirs(OUT, exist_ok=True)

results = []


def check(name, cond, detail=''):
    results.append({'name': name, 'ok': bool(cond), 'detail': str(detail)})
    print(('PASS ' if cond else 'FAIL ') + name + ((' :: ' + str(detail)) if detail else ''))


def shot(page, name):
    path = os.path.join(OUT, name + '.png')
    page.screenshot(path=path)
    print('saved ' + path)


def login(page):
    page.goto(BASE + '/login', wait_until='load')
    page.get_by_label('邮箱').fill('admin@example.com')
    page.get_by_label('密码').fill('password123')
    page.get_by_role('button', name='登 录').click()
    page.wait_for_url(lambda url: '/login' not in url, timeout=15000)
    page.wait_for_load_state('networkidle')


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width': 1600, 'height': 900})
    login(page)

    # 1) 编辑器（wf-021）——画布全幅 + 收起态薄栏
    page.goto(BASE + '/workflows/wf-021', wait_until='load')
    page.wait_for_selector('[data-testid="wf-editor-page"]', timeout=15000)
    page.wait_for_selector('[data-testid="wf-node-cond-fault-branch"]', timeout=15000)
    page.wait_for_timeout(800)

    geo = page.evaluate('''() => {
      const vp = { w: window.innerWidth, h: window.innerHeight };
      const rf = document.querySelector('.react-flow');
      const rail = document.querySelector('[data-testid="wf-library"]');
      const editor = document.querySelector('[data-testid="wf-editor-page"]');
      const box = el => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; };
      return {
        vp,
        canvas: rf ? box(rf) : null,
        rail: rail ? box(rail) : null,
        editor: editor ? box(editor) : null,
        tabsRow: !!document.querySelector('[data-testid="wf-kind-tabs"]'),
        back: !!document.querySelector('[data-testid="wf-back"]'),
        inspectorClosed: !document.querySelector('[data-testid="wf-inspector"]'),
        sidebarCollapsed: !!Array.from(document.querySelectorAll('button')).find(b => b.getAttribute('aria-label') === '展开侧边栏'),
      };
    }''')
    canvas_ratio = geo['canvas']['w'] / (geo['vp']['w'] - 64) if geo['canvas'] else 0
    check('画布全幅（宽占比≥0.97）', canvas_ratio >= 0.97, f"canvas={geo['canvas']} ratio={canvas_ratio:.3f}")
    check('收起态薄栏≈48px', geo['rail'] and 40 <= geo['rail']['w'] <= 56, f"rail={geo['rail']}")
    check('第二行 Tab 条已删（收起态无 chips）', not geo['tabsRow'])
    check('返回出口存在（wf-back）', geo['back'])
    check('未选中时检查器不渲染', geo['inspectorClosed'])
    check('聚焦模式：主侧边栏已收起', geo['sidebarCollapsed'])
    shot(page, 'wf-editor-fullbleed-rail')

    # 2) 展开节点库面板（点击薄栏图标）+ 过滤 chips
    page.get_by_test_id('wf-rail-agent').click()
    page.wait_for_selector('[data-testid="wf-palette-panel"]', timeout=5000)
    panel = page.evaluate('''() => {
      const el = document.querySelector('[data-testid="wf-palette-panel"]');
      const r = el.getBoundingClientRect();
      const adds = document.querySelectorAll('[data-testid^="wf-add-"]');
      const chips = document.querySelectorAll('[data-testid^="wf-tab-"]');
      return { x: r.x, y: r.y, w: r.width, h: r.height, adds: adds.length, chips: chips.length };
    }''')
    check('展开面板 280px + 8 条目 + 8 chips', abs(panel['w'] - 280) <= 3 and panel['adds'] == 8 and panel['chips'] == 8, f"panel={panel}")
    shot(page, 'wf-editor-palette-expanded')

    # 3) 过滤 agent → 1 条 → 点击条目沿 addNode 加入画布
    page.get_by_test_id('wf-tab-agent').click()
    adds_after_filter = page.locator('[data-testid^="wf-add-"]').count()
    check('过滤 chips 生效（8→1）', adds_after_filter == 1, f"adds={adds_after_filter}")
    before = page.locator('[data-testid^="wf-node-agent-"]').count()
    page.get_by_test_id('wf-add-agent').click()
    page.wait_for_timeout(400)
    after = page.locator('[data-testid^="wf-node-agent-"]').count()
    check('点击条目加入画布（agent 节点 +1）', after == before + 1, f"{before}→{after}")
    shot(page, 'wf-editor-palette-add-node')

    # 4) Esc 收起回薄栏
    page.keyboard.press('Escape')
    page.wait_for_timeout(200)
    check('Esc 收起回薄栏', page.locator('[data-testid="wf-palette-panel"]').count() == 0 and page.locator('[data-testid="wf-library"]').count() == 1)

    # 5) 选中节点 → 检查器浮层卡（右上 320px）+ 关闭钮；中心点不被遮挡
    page.get_by_test_id('wf-node-cond-fault-branch').click()
    page.wait_for_selector('[data-testid="wf-inspector"]', timeout=5000)
    insp = page.evaluate('''() => {
      const el = document.querySelector('[data-testid="wf-inspector"]');
      const r = el.getBoundingClientRect();
      const mid = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
      const closeBtn = document.querySelector('[data-testid="wf-inspector-close"]');
      return {
        w: r.width, x: r.x,
        close: !!closeBtn,
        centerFree: !mid || !!mid.closest('.react-flow'),
      };
    }''')
    check('检查器浮层 320px + 关闭钮', abs(insp['w'] - 320) <= 3 and insp['close'], f"insp={insp}")
    check('画布中心不被悬浮件遮挡（可连线）', insp['centerFree'])
    shot(page, 'wf-editor-inspector-float')

    # 6) 关闭钮 → 浮层消失（画布敞开）
    page.get_by_test_id('wf-inspector-close').click()
    page.wait_for_timeout(200)
    check('关闭钮取消选中', page.locator('[data-testid="wf-inspector"]').count() == 0)
    shot(page, 'wf-editor-inspector-closed')

    # 注：空画布引导（0 节点）在 blank 模板（自带开始/结束）下 UI 不可达，MSW 服务态下
    # Playwright 路由也无法拦截 service worker 内应答——该态由 b5c-canvas-layout.test.tsx
    # 用例 ⑤（MSW 注入空图）覆盖，浏览器截图不重复演练。
    # 附：工作流列表页对照截图
    page.goto(BASE + '/workflows', wait_until='load')
    page.wait_for_selector('[data-testid="wf-card-wf-021"]', timeout=15000)
    page.wait_for_timeout(400)
    shot(page, 'wf-list')

    browser.close()

ok = all(r['ok'] for r in results)
print('\nB5C-SHOTS ' + ('ALL PASS' if ok else 'HAS FAILURES') + f' ({sum(1 for r in results if r["ok"])}/{len(results)})')
raise SystemExit(0 if ok else 1)
