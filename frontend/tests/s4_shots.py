# -*- coding: utf-8 -*-
"""S4 本体域截图验收（30 篇 §2 截图闭环）：真实登录 admin → 8 场景 × 亮/暗 存
E:/learn-agnet-project/.qa_ix/s4/。DOM 自查：无横向溢出；工作台三栏与校验面板折叠态高度正确。
运行前提：VITE_ENABLE_MOCK=1 npm run build + npm run preview -- --port 4199。"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = 'http://localhost:4199'
OUT = r'E:\learn-agnet-project\.qa_ix\s4'
os.makedirs(OUT, exist_ok=True)

results = []


def check(name, cond, detail=''):
    results.append({'name': name, 'ok': bool(cond), 'detail': detail})
    print(('PASS ' if cond else 'FAIL ') + name + (' :: ' + detail if detail else ''))


def login(page):
    page.goto(BASE + '/login', wait_until='networkidle')
    page.get_by_label('邮箱').fill('admin@example.com')
    page.get_by_label('密码').fill('password123')
    page.get_by_role('button', name='登 录').click()
    page.wait_for_url(lambda url: '/login' not in url, timeout=15000)
    page.wait_for_load_state('networkidle')


def shot(page, path):
    page.screenshot(path=path, full_page=True)
    print('saved ' + path)


def no_h_overflow(page, tag):
    over = page.evaluate('''() => {
      const d = document.documentElement;
      return { sw: d.scrollWidth, cw: d.clientWidth };
    }''')
    check('no-h-overflow:' + tag, over['sw'] <= over['cw'] + 1, json.dumps(over))


def run(theme, suffix):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={'width': 1440, 'height': 900}, device_scale_factor=2)
        page = ctx.new_page()
        if theme == 'dark':
            page.add_init_script("localStorage.setItem('oa-theme','dark')")
        login(page)

        # 1) 项目列表 /ontology
        page.goto(BASE + '/ontology', wait_until='networkidle')
        page.wait_for_selector('[data-testid="project-card-onto-outage"]', timeout=15000)
        time.sleep(0.4)
        no_h_overflow(page, 'list')
        shot(page, os.path.join(OUT, f'project-list{suffix}.png'))

        # 2) 新建向导第 2 步（方案三卡）
        page.get_by_role('button', name='新建项目').click()
        page.get_by_label('名称（唯一）').fill('配网停电分析本体')
        page.get_by_label('命名空间 IRI').fill('http://example.org/outage#')
        page.get_by_role('button', name='下一步：选择方案').click()
        page.wait_for_selector('[aria-label="重型本体方案"]', timeout=8000)
        page.get_by_role('button', name='重型本体方案').click()
        time.sleep(0.3)
        shot(page, os.path.join(OUT, f'wizard-step2{suffix}.png'))
        page.get_by_role('button', name='取消').click()

        # 3) 工作台三栏（类树 + 画布 + 检查器 + 校验面板）
        page.goto(BASE + '/ontology/onto-outage', wait_until='networkidle')
        page.wait_for_selector('[data-testid="onto-canvas"]', timeout=15000)
        page.wait_for_selector('[data-testid="onto-inspector"]', timeout=10000)
        time.sleep(1.0)  # fitView 动画
        # 三栏几何自查：左树 / 画布 / 检查器 均在视口内且互不重叠（x 区间递增）
        geo = page.evaluate('''() => {
          const g = s => { const el = document.querySelector(s); if (!el) return null;
            const r = el.getBoundingClientRect(); return { x: r.x, r: r.right, y: r.y, b: r.bottom }; };
          return { tree: g('[data-testid="onto-tree-panel"]'), canvas: g('[data-testid="onto-canvas"]'),
                   insp: g('[data-testid="onto-inspector"]'), panel: g('[data-testid="validation-panel"]') };
        }''')
        ok3 = geo['tree'] and geo['canvas'] and geo['insp'] and geo['panel'] \
            and geo['tree']['r'] <= geo['canvas']['x'] + 1 and geo['canvas']['r'] <= geo['insp']['x'] + 1 \
            and geo['panel']['b'] <= 900
        check('workbench-3col-geometry:' + theme, ok3, json.dumps(geo))
        no_h_overflow(page, 'workbench')
        shot(page, os.path.join(OUT, f'workbench-3col{suffix}.png'))

        # 4) 校验面板展开（试校验 → 违例列表 + 定位）
        page.wait_for_selector('[data-testid="run-validate"]', timeout=8000)
        page.get_by_test_id('run-validate').click()
        page.wait_for_selector('[data-testid="locate-0"]', timeout=15000)
        time.sleep(0.3)
        ph = page.evaluate('''() => document.querySelector('[data-testid="validation-panel"]').getBoundingClientRect().height''')
        check('validation-panel-height:' + theme, 180 <= ph <= 230, 'h=' + str(ph))
        shot(page, os.path.join(OUT, f'validation-panel{suffix}.png'))

        # 5) 公理编辑器（?tab=axioms）
        page.goto(BASE + '/ontology/onto-outage?tab=axioms', wait_until='networkidle')
        page.wait_for_selector('[data-testid="axiom-editor"]', timeout=15000)
        time.sleep(0.8)
        page.get_by_test_id('axiom-validate').click()
        time.sleep(1.0)
        shot(page, os.path.join(OUT, f'axiom-editor{suffix}.png'))

        # 6) 版本评审 + diff 逐条决策
        page.goto(BASE + '/ontology/onto-outage/versions?cs=cs_01K', wait_until='networkidle')
        page.wait_for_selector('[data-testid="diff-row-d1"]', timeout=15000)
        page.get_by_test_id('accept-d1').click()
        page.get_by_test_id('reject-d2').click()
        time.sleep(0.3)
        no_h_overflow(page, 'versions')
        shot(page, os.path.join(OUT, f'versions-diff{suffix}.png'))

        # 7) 发布五步物化进度
        page.get_by_test_id('accept-all').click()
        page.get_by_test_id('approve-publish').click()
        page.wait_for_selector('[data-testid="publish-go"]', timeout=10000)
        page.get_by_label('发布说明（必填）').fill('停电工单行动类扩展；故障父类对齐设备事件')
        page.get_by_test_id('publish-go').click()
        page.wait_for_selector('[data-testid="publish-progress-text"]', timeout=6000)
        time.sleep(1.4)  # 步进中段
        shot(page, os.path.join(OUT, f'publish-progress{suffix}.png'))
        page.wait_for_selector('[data-testid="publish-done"]', timeout=8000)
        check('publish-done:' + theme, '已发布' in page.inner_text('[data-testid="publish-done"]'))
        page.get_by_label('发布完成').get_by_role('button', name='关闭', exact=True).last.click()

        # 8) 图谱浏览 + 实体抽屉（IX-EX-01）
        page.goto(BASE + '/kb/explore/outage-kb', wait_until='networkidle')
        page.wait_for_selector('[data-testid="explore-canvas"]', timeout=15000)
        time.sleep(1.0)
        # 搜索选择器选实体 → 抽屉打开
        page.get_by_label('实体搜索选择器').click()
        page.get_by_label('实体搜索选择器').fill('部件A')
        page.get_by_role('button', name='部件A').first.click()
        page.wait_for_selector('[data-testid="entity-iri"]', timeout=8000)
        time.sleep(0.5)
        no_h_overflow(page, 'explore')
        shot(page, os.path.join(OUT, f'explore-drawer{suffix}.png'))

        browser.close()


t0 = time.time()
run('light', '')
run('dark', '-dark')
print('total %.1fs' % (time.time() - t0))
fails = [r for r in results if not r['ok']]
print(json.dumps(results, ensure_ascii=False, indent=1))
sys.exit(1 if fails else 0)
