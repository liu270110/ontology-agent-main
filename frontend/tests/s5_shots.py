# -*- coding: utf-8 -*-
"""S5 平台域截图验收（30 篇 §2 截图闭环）：真实登录 admin → 13 场景 × 亮/暗 存
E:/learn-agnet-project/.qa_ix/s5/。DOM 自查：无横向溢出；弹窗/抽屉不溢出视口、
双栏不重叠（IX-MEM-01 720 弹窗、AGT-03 620 弹窗、MKT-02 640 弹窗、MCP-02 抽屉等）。
运行前提：VITE_ENABLE_MOCK=1 npm run build + npm run preview -- --port 4199。"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = 'http://localhost:4199'
OUT = r'E:\learn-agnet-project\.qa_ix\s5'
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


def modal_geometry(page, tag, selector):
    """弹窗/抽屉自查：完整落在视口内（不溢出）且非零尺寸。"""
    geo = page.evaluate('''(sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, r: r.right, b: r.bottom, w: r.width, h: r.height,
               vw: innerWidth, vh: innerHeight };
    }''', selector)
    ok = geo and geo['w'] > 100 and geo['h'] > 100 \
        and geo['x'] >= -1 and geo['y'] >= -1 \
        and geo['r'] <= geo['vw'] + 1 and geo['b'] <= geo['vh'] + 1
    check('modal-geometry:' + tag, ok, json.dumps(geo))


def two_columns_no_overlap(page, tag, left_sel, right_sel):
    """双栏自查：左右栏 x 区间不重叠。"""
    geo = page.evaluate('''([l, r]) => {
      const g = s => { const el = document.querySelector(s); if (!el) return null;
        const b = el.getBoundingClientRect(); return { x: b.x, r: b.right }; };
      return { left: g(l), right: g(r) };
    }''', [left_sel, right_sel])
    ok = geo and geo['left'] and geo['right'] and geo['left']['r'] <= geo['right']['x'] + 1
    check('two-col-no-overlap:' + tag, ok, json.dumps(geo))


def run(theme, suffix):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={'width': 1440, 'height': 900}, device_scale_factor=2)
        page = ctx.new_page()
        if theme == 'dark':
            page.add_init_script("localStorage.setItem('oa-theme','dark')")
        login(page)

        # 1) 记忆管理 L3 组织层（条目列表 + 徽标）
        page.goto(BASE + '/memory?layer=L3', wait_until='networkidle')
        page.wait_for_selector('[data-testid="fact-row-fact-0012"]', timeout=15000)
        time.sleep(0.4)
        no_h_overflow(page, 'memory-L3')
        shot(page, os.path.join(OUT, f'memory-L3{suffix}.png'))

        # 2) IX-MEM-01 升级审核对照弹窗（720 双栏）
        page.goto(BASE + '/memory?layer=L2', wait_until='networkidle')
        page.wait_for_selector('[data-testid="mem-review-open-PM-0043"]', timeout=15000)
        page.get_by_test_id('mem-review-open-PM-0043').click()
        page.wait_for_selector('[data-testid="mem-candidate-card"]', timeout=8000)
        time.sleep(0.4)
        modal_geometry(page, 'mem-review' + suffix, '[data-testid="mem-candidate-card"]')
        two_columns_no_overlap(page, 'mem-review' + suffix,
                               '[data-testid="mem-candidate-card"]', '[data-testid="mem-quote-0"]')
        no_h_overflow(page, 'mem-review')
        shot(page, os.path.join(OUT, f'memory-review-modal{suffix}.png'))
        page.keyboard.press('Escape')

        # 3) IX-MEM-02 条目详情抽屉（时间线 + 失效边红）
        page.wait_for_selector('[data-testid="fact-row-fact-0009"]', timeout=8000)
        page.get_by_test_id('fact-row-fact-0010').click()
        page.wait_for_selector('[data-testid="fact-timeline"]', timeout=8000)
        time.sleep(0.4)
        no_h_overflow(page, 'mem-detail')
        shot(page, os.path.join(OUT, f'memory-detail-sheet{suffix}.png'))
        page.keyboard.press('Escape')

        # 4) IX-MEM-03 L1 只读视图（TTL 倒计时 + 脱敏）
        page.get_by_test_id('layer-tab-L1').click()
        page.wait_for_selector('[data-testid="l1-card-s-2402"]', timeout=8000)
        time.sleep(0.4)
        no_h_overflow(page, 'memory-L1')
        shot(page, os.path.join(OUT, f'memory-L1-readonly{suffix}.png'))

        # 5) Agent 卡片列表
        page.goto(BASE + '/agents', wait_until='networkidle')
        page.wait_for_selector('[data-testid="agent-card-agt-nanobot-01"]', timeout=15000)
        time.sleep(0.4)
        no_h_overflow(page, 'agents')
        shot(page, os.path.join(OUT, f'agents-cards{suffix}.png'))

        # 6) IX-AGT-01 注册向导第②步（RJSF + 连接测试成功态）
        page.get_by_test_id('agt-register-open').click()
        page.wait_for_selector('[data-testid="agt-adapter-nanobot"]', timeout=8000)
        page.get_by_test_id('agt-adapter-nanobot').click()
        page.get_by_test_id('agt-wiz-next').click()
        page.wait_for_selector('[data-testid="rjsf-form"]', timeout=8000)
        page.get_by_label('接入令牌 token').fill('nbk_a88f2c19f3e')
        page.get_by_test_id('agt-conn-test').click()
        page.wait_for_selector('[data-testid="agt-conn-success"]', timeout=8000)
        time.sleep(0.5)
        ok_conn = 'RTT 86ms' in page.inner_text('[data-testid="agt-conn-success"]')
        check('wizard-conn-success:' + theme, ok_conn)
        no_h_overflow(page, 'agt-wizard')
        shot(page, os.path.join(OUT, f'agent-wizard-step2{suffix}.png'))
        # 第③步 ToolPicker 内嵌
        page.get_by_test_id('agt-wiz-next').click()
        page.wait_for_selector('[data-testid="tool-check-kb.search"]', timeout=8000)
        time.sleep(0.4)
        shot(page, os.path.join(OUT, f'agent-wizard-step3-toolpicker{suffix}.png'))
        page.keyboard.press('Escape')

        # 7) IX-AGT-02 详情四 Tab（运行历史三区）
        page.goto(BASE + '/agents/agt-nanobot-01?tab=history', wait_until='networkidle')
        page.wait_for_selector('[data-testid="agt-panel-history"]', timeout=15000)
        page.wait_for_selector('[data-testid="agt-debug-log"]', timeout=8000)
        time.sleep(0.4)
        no_h_overflow(page, 'agent-detail')
        shot(page, os.path.join(OUT, f'agent-detail-history{suffix}.png'))

        # 8) IX-AGT-03 ToolPicker 弹窗（依赖自动勾选演示）
        page.get_by_test_id('agt-tab-tools').click()
        page.wait_for_selector('[data-testid="agt-tools-adjust"]', timeout=8000)
        page.get_by_test_id('agt-tools-adjust').click()
        page.wait_for_selector('[data-testid="tool-check-cli-anything.exec"]', timeout=8000)
        page.get_by_test_id('tool-check-cli-anything.exec').click()
        time.sleep(0.4)
        ok_dep = '依赖自动勾选' in page.inner_text('[data-testid="tool-row-kb.search"]')
        check('toolpicker-dep-autocheck:' + theme, ok_dep)
        modal_geometry(page, 'toolpicker' + suffix, '[data-testid="toolpicker-tree"]')
        no_h_overflow(page, 'toolpicker')
        shot(page, os.path.join(OUT, f'toolpicker{suffix}.png'))
        page.keyboard.press('Escape')

        # 9) 插件市场卡片网格
        page.goto(BASE + '/marketplace', wait_until='networkidle')
        page.wait_for_selector('[data-testid="plugin-card-p_gdticket"]', timeout=15000)
        time.sleep(0.4)
        no_h_overflow(page, 'market')
        shot(page, os.path.join(OUT, f'marketplace-cards{suffix}.png'))

        # 10) IX-MKT-01 详情抽屉 + IX-MKT-02 安装向导 scope 步（高危强制勾选）
        page.get_by_test_id('plugin-card-p_gdticket').click()
        page.wait_for_selector('[data-testid="mkt-detail-tab-README"]', timeout=8000)
        time.sleep(0.3)
        shot(page, os.path.join(OUT, f'market-detail-sheet{suffix}.png'))
        page.get_by_test_id('mkt-install-open').click()
        page.wait_for_selector('[data-testid="mkt-install-next"]', timeout=8000)
        page.get_by_test_id('mkt-install-next').click()
        page.wait_for_selector('[data-testid="mkt-scope-mcp.call"]', timeout=8000)
        ok_disabled = page.get_by_test_id('mkt-install-next').is_disabled()
        check('mkt-scope-gate-disabled:' + theme, ok_disabled)
        page.get_by_test_id('mkt-danger-ack').click()
        time.sleep(0.3)
        ok_enabled = page.get_by_test_id('mkt-install-next').is_enabled()
        check('mkt-scope-gate-enabled:' + theme, ok_enabled)
        no_h_overflow(page, 'mkt-install')
        shot(page, os.path.join(OUT, f'market-install-scope{suffix}.png'))
        page.keyboard.press('Escape')
        page.keyboard.press('Escape')

        # 11) 工具注册表 + IX-TLS-01 详情抽屉
        page.goto(BASE + '/tools', wait_until='networkidle')
        page.wait_for_selector('[data-testid="tool-tr-crm.query"]', timeout=15000)
        time.sleep(0.4)
        shot(page, os.path.join(OUT, f'tools-table{suffix}.png'))
        page.get_by_test_id('tool-tr-grid.load.query').click()
        page.wait_for_selector('[data-testid="tls-stats-bars"]', timeout=8000)
        time.sleep(0.4)
        no_h_overflow(page, 'tls-detail')
        shot(page, os.path.join(OUT, f'tool-detail-sheet{suffix}.png'))
        page.keyboard.press('Escape')
        # IX-TLS-03 技能库抽屉
        page.get_by_test_id('tls-view-skills').click()
        page.wait_for_selector('[data-testid="skill-card-sk-outage-chain"]', timeout=8000)
        page.get_by_test_id('skill-card-sk-outage-chain').click()
        page.wait_for_selector('[data-testid="tls-frontmatter"]', timeout=8000)
        time.sleep(0.4)
        shot(page, os.path.join(OUT, f'skill-sheet{suffix}.png'))
        page.keyboard.press('Escape')

        # 12) MCP Server 列表 + 详情抽屉
        page.goto(BASE + '/mcp', wait_until='networkidle')
        page.wait_for_selector('[data-testid="mcp-tr-crm-prod"]', timeout=15000)
        time.sleep(0.4)
        no_h_overflow(page, 'mcp')
        shot(page, os.path.join(OUT, f'mcp-list{suffix}.png'))
        page.get_by_test_id('mcp-tr-crm-prod').click()
        page.wait_for_selector('[data-testid="mcp-health-card"]', timeout=8000)
        time.sleep(0.4)
        shot(page, os.path.join(OUT, f'mcp-detail-sheet{suffix}.png'))
        page.keyboard.press('Escape')

        # 13) IX-MCP-01 接入向导第②步（发现成功 + 默认全选 + 需审批徽标）
        page.get_by_test_id('mcp-wizard-open').click()
        page.wait_for_selector('[data-testid="mcp-name"]', timeout=8000)
        page.get_by_test_id('mcp-name').fill('crm-prod-new')
        page.get_by_test_id('mcp-url').fill('https://crm-prod-new.example.com/mcp')
        page.get_by_test_id('mcp-wiz-next').click()
        page.get_by_test_id('mcp-discover-go').click()
        page.wait_for_selector('[data-testid="mcp-discover-ok"]', timeout=8000)
        ok_all = page.get_by_test_id('mcp-adopt-all').is_checked()
        check('mcp-discover-adopt-all:' + theme, ok_all)
        # 5 项工具勾选（不含组头全选框）+ 写入类带「需审批」徽标
        n_tools = page.locator('[data-testid^="mcp-adopt-"]:not([data-testid="mcp-adopt-all"])').count()
        check('mcp-discover-tools:' + theme, n_tools == 5, 'count=' + str(n_tools))
        ok_need = page.locator('[data-testid="mcp-adopt-crm.ticket.create"]').count() == 1
        check('mcp-discover-write-badge:' + theme, ok_need)
        time.sleep(0.4)
        no_h_overflow(page, 'mcp-wizard')
        shot(page, os.path.join(OUT, f'mcp-wizard-step2{suffix}.png'))
        page.keyboard.press('Escape')

        browser.close()


t0 = time.time()
run('light', '')
run('dark', '-dark')
print('total %.1fs' % (time.time() - t0))
fails = [r for r in results if not r['ok']]
print(json.dumps(results, ensure_ascii=False, indent=1))
sys.exit(1 if fails else 0)
