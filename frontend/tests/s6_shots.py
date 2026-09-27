# -*- coding: utf-8 -*-
"""S6 治理域截图验收（30 篇 §2 截图闭环）：真实登录 → 16 场景 × 亮/暗 存
E:/learn-agnet-project/.qa_ix/s6/。DOM 自查：无横向溢出；弹窗/抽屉完整落在视口内。
租户 Tab 场景用 super@example.com（super_admin；admin 仅 admin 角色看不到租户入口）。
运行前提：VITE_ENABLE_MOCK=1 npm run build + npm run preview -- --port 4199。"""
import json
import os
import time

from playwright.sync_api import sync_playwright

BASE = 'http://localhost:4199'
OUT = r'E:\learn-agnet-project\.qa_ix\s6'
os.makedirs(OUT, exist_ok=True)

results = []


def check(name, cond, detail=''):
    results.append({'name': name, 'ok': bool(cond), 'detail': detail})
    print(('PASS ' if cond else 'FAIL ') + name + (' :: ' + detail if detail else ''))


def login(page, email='admin@example.com'):
    page.goto(BASE + '/login', wait_until='networkidle')
    page.get_by_label('邮箱').fill(email)
    page.get_by_label('密码').fill('password123')
    page.get_by_role('button', name='登 录').click()
    page.wait_for_url(lambda url: '/login' not in url, timeout=15000)
    page.wait_for_load_state('networkidle')


def shot(page, name):
    path = os.path.join(OUT, name + '.png')
    page.screenshot(path=path, full_page=True)
    print('saved ' + path)


def no_h_overflow(page, tag):
    over = page.evaluate('''() => {
      const d = document.documentElement;
      return { sw: d.scrollWidth, cw: d.clientWidth };
    }''')
    check('no-h-overflow:' + tag, over['sw'] <= over['cw'] + 1, json.dumps(over))


def modal_geometry(page, tag, selector):
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


def run(theme, suffix):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={'width': 1440, 'height': 900}, device_scale_factor=2)
        page = ctx.new_page()
        if theme == 'dark':
            page.add_init_script("localStorage.setItem('oa-theme','dark')")
        login(page)

        # 1) 审批中心列表（待办 · 六类类型徽标）
        page.goto(BASE + '/approvals', wait_until='networkidle')
        page.wait_for_selector('[data-testid="apr-card-CR-031"]', timeout=15000)
        time.sleep(0.4)
        badges = page.locator('.badge').all_inner_texts()
        for t in ['变更发布', '抽取终审', '插件安装', 'MCP 接入', '记忆升级', '权限申请']:
            check('apr-badge:' + t + suffix, t in ' '.join(badges))
        no_h_overflow(page, 'approvals')
        shot(page, f'approvals-list{suffix}')

        # 2) IX-APR-01 审批详情弹窗（720 · changeset 三色计数 + 审批链）
        page.get_by_test_id('apr-card-CR-031').click()
        page.wait_for_selector('[data-testid="apr-sum-changeset"]', timeout=8000)
        time.sleep(0.4)
        modal_geometry(page, 'apr-detail' + suffix, '[data-testid="apr-sum-changeset"]')
        no_h_overflow(page, 'apr-detail')
        shot(page, f'approvals-detail-modal{suffix}')
        page.keyboard.press('Escape')

        # 3) IX-APR-02 批量审批（勾选高危 MCP-12 → 禁批灰态说明）
        page.get_by_test_id('apr-check-MCP-12').click()
        page.wait_for_selector('[data-testid="apr-batchbar"]', timeout=8000)
        page.get_by_test_id('apr-batch-open').click()
        page.wait_for_selector('[data-testid="apr-batch-blocked"]', timeout=8000)
        time.sleep(0.4)
        modal_geometry(page, 'apr-batch' + suffix, '[role="dialog"]')
        no_h_overflow(page, 'apr-batch')
        shot(page, f'approvals-batch-modal{suffix}')
        page.keyboard.press('Escape')

        # 4) /admin 用户 Tab
        page.goto(BASE + '/admin?tab=users', wait_until='networkidle')
        page.wait_for_selector('[data-testid="adm-user-u-01"]', timeout=15000)
        time.sleep(0.4)
        no_h_overflow(page, 'admin-users')
        shot(page, f'admin-users{suffix}')

        # 5) IX-ADM-01 邀请成员弹窗（chip 化邮箱）
        page.get_by_test_id('adm-invite-open').click()
        page.wait_for_selector('[data-testid="adm-invite-input"]', timeout=8000)
        page.get_by_test_id('adm-invite-input').fill('li.lei@example.com, zhang.san@example.com')
        page.get_by_test_id('adm-invite-input').press('Enter')
        page.wait_for_selector('[data-testid="adm-invite-chip-li.lei@example.com"]', timeout=8000)
        time.sleep(0.3)
        modal_geometry(page, 'adm-invite' + suffix, '[role="dialog"]')
        no_h_overflow(page, 'adm-invite')
        shot(page, f'admin-invite-modal{suffix}')
        page.keyboard.press('Escape')

        # 6) IX-ADM-04 角色矩阵（勾选两格 → 乐观高亮 + 变更摘要条）
        page.goto(BASE + '/admin?tab=roles', wait_until='networkidle')
        page.wait_for_selector('[data-testid="adm-matrix"]', timeout=15000)
        page.get_by_test_id('adm-matrix-guest-playground:use').click()
        page.get_by_test_id('adm-matrix-member-chat:use').click()
        page.wait_for_selector('[data-testid="adm-matrix-bar"]', timeout=8000)
        time.sleep(0.4)
        summary = page.inner_text('[data-testid="adm-matrix-summary"]')
        check('adm-matrix-summary:' + theme, '+1 / −1' in summary, summary)
        no_h_overflow(page, 'adm-roles')
        shot(page, f'admin-roles-matrix{suffix}')

        # 7) IX-ADM-05 模型渠道接入弹窗（连通测试成功态 → 保存解锁）
        page.goto(BASE + '/admin?tab=models', wait_until='networkidle')
        page.wait_for_selector('[data-testid="adm-model-m-01"]', timeout=15000)
        page.get_by_test_id('adm-model-open').click()
        page.wait_for_selector('[data-testid="adm-model-test"]', timeout=8000)
        page.get_by_label('API Key（脱敏）').fill('sk-ds-demo-9f8e7d6c')
        page.get_by_test_id('adm-model-test-btn').click()
        page.wait_for_selector('[data-testid="adm-model-test-ok"]', timeout=10000)
        time.sleep(0.4)
        save_enabled = page.get_by_test_id('adm-model-save').is_enabled()
        check('adm-model-save-unlocked:' + theme, save_enabled)
        modal_geometry(page, 'adm-model' + suffix, '[data-testid="adm-model-test"]')
        no_h_overflow(page, 'adm-model')
        shot(page, f'admin-model-add-modal{suffix}')
        page.keyboard.press('Escape')

        # 8) IX-ADM-07 审计 trace 行展开（瀑布 + 成本 + writeback 台账行）
        page.goto(BASE + '/admin?tab=audit', wait_until='networkidle')
        page.wait_for_selector('[data-testid="adm-audit-row-tr-b71c9e2d"]', timeout=15000)
        page.get_by_test_id('adm-audit-row-tr-b71c9e2d').click()
        page.wait_for_selector('[data-testid="adm-trace-tr-b71c9e2d"]', timeout=8000)
        time.sleep(0.4)
        trace_txt = page.inner_text('[data-testid="adm-trace-tr-b71c9e2d"]')
        for t in ['网关路由', '12ms', 'LLM 推理', '2.8s', 'WB-0311', 'needs_human']:
            check('adm-trace:' + t + suffix, t in trace_txt)
        no_h_overflow(page, 'adm-audit-trace')
        shot(page, f'admin-audit-trace{suffix}')

        # 9) IX-ADM-08 导出弹窗（继承过滤态）
        page.get_by_test_id('adm-audit-export').click()
        page.wait_for_selector('[data-testid="adm-export-filters"]', timeout=8000)
        time.sleep(0.3)
        modal_geometry(page, 'adm-export' + suffix, '[role="dialog"]')
        no_h_overflow(page, 'adm-export')
        shot(page, f'admin-audit-export-modal{suffix}')
        page.keyboard.press('Escape')

        # 10) 任务中心列表
        page.goto(BASE + '/tasks', wait_until='networkidle')
        page.wait_for_selector('[data-testid="tsk-row-job-217"]', timeout=15000)
        time.sleep(0.4)
        no_h_overflow(page, 'tasks')
        shot(page, f'tasks-list{suffix}')

        # 11) IX-TSK-01 任务详情抽屉（七步流水线 + SSE 时间线推进）
        page.get_by_test_id('tsk-row-job-217').click()
        page.wait_for_selector('[data-testid="tsk-drawer"]', timeout=8000)
        page.wait_for_selector('[data-testid="tsk-timeline"] >> text=CHUNK_018', timeout=10000)
        time.sleep(0.4)
        no_h_overflow(page, 'tsk-drawer')
        shot(page, f'tasks-detail-drawer{suffix}')
        page.keyboard.press('Escape')
        # vaul 关闭动画：等抽屉真正离场再点下一行（防遮罩吞点击）
        page.wait_for_selector('[data-testid="tsk-drawer"]', state='detached', timeout=8000)
        time.sleep(0.4)

        # 12) IX-TSK-02 日志抽屉（二级覆盖）
        page.get_by_test_id('tsk-row-job-215').click()
        page.wait_for_selector('[data-testid="tsk-drawer"]', timeout=8000)
        page.get_by_test_id('tsk-retry').wait_for(timeout=8000)
        page.get_by_test_id('tsk-logs').click()
        page.wait_for_selector('[data-testid="tsk-log-stream"]', timeout=8000)
        time.sleep(0.4)
        no_h_overflow(page, 'tsk-logs')
        shot(page, f'tasks-logs-drawer{suffix}')
        page.keyboard.press('Escape')
        time.sleep(0.5)
        page.keyboard.press('Escape')
        time.sleep(0.5)

        # 13) IX-TSK-03 重试确认弹窗（错误体四字段 + 范围单选；重新打开抽屉避免嵌套态残留）
        page.goto(BASE + '/tasks', wait_until='networkidle')
        page.wait_for_selector('[data-testid="tsk-row-job-215"]', timeout=15000)
        page.get_by_test_id('tsk-row-job-215').click()
        page.wait_for_selector('[data-testid="tsk-retry"]', timeout=8000)
        page.get_by_test_id('tsk-retry').click()
        page.wait_for_selector('[data-testid="tsk-retry-error"]', timeout=8000)
        time.sleep(0.3)
        err_txt = page.inner_text('[data-testid="tsk-retry-error"]')
        check('tsk-retry-error-4fields:' + theme,
              all(k in err_txt for k in ['code', 'message', 'target', 'trace_id']), err_txt[:80])
        modal_geometry(page, 'tsk-retry' + suffix, '[data-testid="tsk-retry-error"]')
        shot(page, f'tasks-retry-modal{suffix}')
        page.keyboard.press('Escape')
        time.sleep(0.3)

        # 14) IX-SET-01/02 资料与安全 Tab（2FA 状态卡）
        page.goto(BASE + '/settings?tab=security', wait_until='networkidle')
        page.wait_for_selector('[data-testid="set-2fa-card"]', timeout=15000)
        page.get_by_test_id('set-2fa-enable').click()
        page.wait_for_selector('[data-testid="set-2fa-secret"]', timeout=8000)
        time.sleep(0.4)
        no_h_overflow(page, 'set-security')
        shot(page, f'settings-2fa-wizard{suffix}')
        page.keyboard.press('Escape')

        # 15) IX-SET-03 API Key（成功态完整 Key 只显示一次）
        page.goto(BASE + '/settings?tab=keys', wait_until='networkidle')
        page.wait_for_selector('[data-testid="set-key-k-01"]', timeout=15000)
        page.get_by_test_id('set-key-open').click()
        page.wait_for_selector('[data-testid="set-key-name"]', timeout=8000)
        page.get_by_test_id('set-key-name').fill('playwright-runner')
        page.get_by_test_id('set-key-create').click()
        page.wait_for_selector('[data-testid="set-key-plain"]', timeout=8000)
        time.sleep(0.4)
        plain = page.inner_text('[data-testid="set-key-plain"]')
        check('set-key-plain-once:' + theme, plain.startswith('sk-oa-live-'), plain[:40])
        modal_geometry(page, 'set-key' + suffix, '[role="dialog"]')
        no_h_overflow(page, 'set-key')
        shot(page, f'settings-key-success{suffix}')
        page.keyboard.press('Escape')

        # 16) IX-SET-04 通知偏好矩阵（高风险回写行锁定）
        page.goto(BASE + '/settings?tab=notifications', wait_until='networkidle')
        page.wait_for_selector('[data-testid="set-notify-high_risk_writeback"]', timeout=15000)
        time.sleep(0.4)
        no_h_overflow(page, 'set-notify')
        shot(page, f'settings-notifications{suffix}')

        page.close()

        # 17) IX-ADM-03 租户创建（super_admin 专属入口；super@example.com）
        ctx2 = browser.new_context(viewport={'width': 1440, 'height': 900}, device_scale_factor=2)
        page2 = ctx2.new_page()
        if theme == 'dark':
            page2.add_init_script("localStorage.setItem('oa-theme','dark')")
        login(page2, 'super@example.com')
        page2.goto(BASE + '/admin?tab=tenants', wait_until='networkidle')
        page2.wait_for_selector('[data-testid="adm-tenant-open"]', timeout=15000)
        page2.get_by_test_id('adm-tenant-open').click()
        page2.wait_for_selector('[data-testid="adm-tenant-name"]', timeout=8000)
        page2.get_by_test_id('adm-tenant-name').fill('省检修分公司')
        page2.get_by_test_id('adm-tenant-ns').fill('maintenance-co')
        page2.get_by_test_id('adm-tenant-tier-team').click()
        page2.get_by_test_id('adm-tenant-create').click()
        page2.wait_for_selector('[data-testid="adm-tenant-password"]', timeout=8000)
        time.sleep(0.4)
        once = page2.inner_text('[data-testid="adm-tenant-once"]')
        check('adm-tenant-once:' + theme, '仅此一次' in once)
        no_h_overflow(page2, 'adm-tenant')
        shot(page2, f'admin-tenant-create{suffix}')
        ctx2.close()

        browser.close()


if __name__ == '__main__':
    run('light', '')
    run('dark', '-dark')
    fails = [r for r in results if not r['ok']]
    print(json.dumps({'total': len(results), 'failed': len(fails)}, ensure_ascii=False))
    if fails:
        print(json.dumps(fails, ensure_ascii=False, indent=1))
        sys.exit(1)
    print('ALL S6 SHOTS PASS')
