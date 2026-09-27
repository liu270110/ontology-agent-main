# -*- coding: utf-8 -*-
"""S7 协作域截图验收（30 篇 §2 截图闭环 / 26 篇 §15 IX-GRP-01~11）：真实登录 → 12 场景 ×
亮/暗 存 E:/learn-agnet-project/.qa_ix/s7/。DOM 自查：无横向溢出；弹窗/抽屉完整落在视口内。
运行前提：VITE_ENABLE_MOCK=1 npm run build + npm run preview -- --port 4199。"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = 'http://localhost:4199'
OUT = r'E:\learn-agnet-project\.qa_ix\s7'
os.makedirs(OUT, exist_ok=True)

results = []


def check(name, cond, detail=''):
    results.append({'name': name, 'ok': bool(cond), 'detail': detail})
    print(('PASS ' if cond else 'FAIL ') + name + (' :: ' + detail if detail else ''))


def login(page, email='admin@example.com'):
    page.goto(BASE + '/login', wait_until='load')
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

        # 1) 群聊三栏（停电分析群 · 协调者系统行 + 归属着色 + 高风险确认卡）：
        #    发送一条消息 → 协调者模式路由行（GRP-03）随 SSE 流入
        page.goto(BASE + '/chat/group/g-1107', wait_until='load')
        page.wait_for_selector('[data-testid="grp-member-panel"]', timeout=15000)
        page.wait_for_selector('[data-testid="grp-action-confirm"]', timeout=10000)
        page.get_by_test_id('grp-input').fill('梳理 110kV 城西变站本次故障风险，给出检修优先级。')
        page.get_by_test_id('grp-send').click()
        page.wait_for_selector('[data-testid="grp-routing-row-8e21c4"]', timeout=15000)
        time.sleep(1.2)
        check('grp-attribution:模型徽标' + suffix, page.locator('.chipmodel').count() > 0)
        no_h_overflow(page, 'group-chat' + suffix)
        shot(page, f'group-chat-three-cols{suffix}')

        # 2) ResponseGroup 多答对比（负荷会商群 all 模式历史）
        page.goto(BASE + '/chat/group/g-0814', wait_until='load')
        page.wait_for_selector('[data-testid="grp-rg-rg-92"]', timeout=15000)
        time.sleep(0.5)
        rg = page.inner_text('[data-testid="grp-rg-rg-92"]')
        for t in ['ResponseGroup', '2 张答案卡', '已选优', '超时未返回']:
            check('grp-rg:' + t + suffix, t in rg)
        no_h_overflow(page, 'group-rg' + suffix)
        shot(page, f'group-response-group{suffix}')

        # 3) IX-GRP-01 建群/成员选择（Modal 600px 双栏：ACL use 过滤 + 角色指定 + 跨租户审批）
        page.goto(BASE + '/chat/group/g-1107', wait_until='load')
        page.wait_for_selector('[data-testid="grp-add-member"]', timeout=15000)
        page.get_by_test_id('grp-add-member').click()
        page.wait_for_selector('[data-testid="grp-picker-submit"]', timeout=8000)
        # 加成员模式：既有成员（调度/设备/报告）禁用；勾选负荷预测 + 跨租户缺陷分析
        # g-1107 已 4/5，容量余 1：勾选负荷预测；跨租户缺陷分析行保持虚线待审批形态
        page.get_by_test_id('grp-slot-slot:agent-load').click()
        time.sleep(0.4)
        dialog_txt = page.inner_text('[role="dialog"]')
        for t in ['可选 Agent 插槽', 'ACL use 级过滤', '跨租户 · 需审批', '无 use 权限', '协调者全群仅 1 名']:
            check('grp-picker:' + t + suffix, t in dialog_txt)
        modal_geometry(page, 'grp-picker' + suffix, '[role="dialog"]')
        no_h_overflow(page, 'grp-picker' + suffix)
        shot(page, f'group-member-picker{suffix}')
        page.keyboard.press('Escape')

        # 4) IX-GRP-02 路由切换 Popover（四模式卡 + 预算提示 + 宪法 2 注记）
        page.get_by_test_id('grp-routing-open').click()
        page.wait_for_selector('[data-testid="grp-mode-orchestrator"]', timeout=8000)
        time.sleep(0.4)
        pop = page.inner_text('[role="dialog"]')
        for t in ['@点名', '轮询', '多答对比', '协调者', '唯一 LLM 路由', '宪法 2']:
            check('grp-routing:' + t + suffix, t in pop)
        modal_geometry(page, 'grp-routing' + suffix, '[role="dialog"]')
        shot(page, f'group-routing-popover{suffix}')
        page.keyboard.press('Escape')

        # 5) IX-GRP-05 成员菜单（暂停/角色调整/移除确认 + 409 唯一性提示）
        page.get_by_test_id('grp-member-menu-m-3').click()
        page.wait_for_selector('[data-testid="grp-menu-remove"]', timeout=8000)
        time.sleep(0.4)
        menu = page.inner_text('[role="dialog"]')
        for t in ['暂停接收新轮', '角色调整为协调者', '唯一性校验', '移除出群', '历史消息保留归属']:
            check('grp-menu:' + t + suffix, t in menu)
        shot(page, f'group-member-menu{suffix}')
        page.keyboard.press('Escape')

        # 6) /workflows 列表（WorkflowCard 网格：版本状态/成功率/ACL 徽标）
        page.goto(BASE + '/workflows', wait_until='load')
        page.wait_for_selector('[data-testid="wf-card-wf-021"]', timeout=15000)
        time.sleep(0.4)
        cards = page.inner_text('[data-testid="wf-list-page"]')
        for t in ['停电故障研判', '检修申请审批流', '草稿 v3', '96%', 'ACL']:
            check('wf-list:' + t + suffix, t in cards)
        no_h_overflow(page, 'workflows' + suffix)
        shot(page, f'workflows-list{suffix}')

        # 7) IX-GRP-06 新建/从模板（Modal 560px：名称/描述 + 模板三选）
        page.get_by_test_id('wf-new-open').click()
        page.wait_for_selector('[data-testid="wf-tpl-rag_qa"]', timeout=8000)
        time.sleep(0.4)
        modal_geometry(page, 'wf-new' + suffix, '[role="dialog"]')
        shot(page, f'workflows-new-modal{suffix}')
        page.keyboard.press('Escape')

        # 8) 编辑器三栏（条件节点选中 → 检查器表达式编辑器 + 禁裸 LLM 警示）
        page.goto(BASE + '/workflows/wf-021', wait_until='load')
        page.wait_for_selector('[data-testid="wf-node-cond-fault-branch"]', timeout=15000)
        page.get_by_test_id('wf-node-cond-fault-branch').click()
        page.wait_for_selector('[data-testid="wf-llm-warning"]', timeout=8000)
        time.sleep(0.6)
        insp = page.inner_text('[data-testid="wf-inspector"]')
        for t in ['确定性表达式', 'expr.editor · 仅确定性', '禁裸 LLM 分支', '条件路由节点']:
            check('wf-inspector:' + t + suffix, t in insp)
        no_h_overflow(page, 'wf-editor' + suffix)
        shot(page, f'workflow-editor-three-cols{suffix}')

        # 9) IX-GRP-08 试运行面板（底部 Drawer：时间线 + 断点命中暂停）
        page.get_by_test_id('wf-test-run').click()
        page.wait_for_selector('[data-testid="wf-run-panel"]', timeout=10000)
        page.wait_for_selector('[data-testid="wf-step-agent-equipment-paused"]', timeout=20000)
        time.sleep(0.5)
        panel = page.inner_text('[data-testid="wf-run-panel"]')
        for t in ['workflow_test', '断点命中 · 已暂停', '断点 BP-1', '结果摘要', '查看快照']:
            check('wf-run:' + t + suffix, t in panel)
        no_h_overflow(page, 'wf-run' + suffix)
        shot(page, f'workflow-testrun-drawer{suffix}')

        # 10) IX-GRP-09 断点续跑 time-travel（Modal 480px：快照 kv + 修参 + 继续/放弃）
        page.get_by_test_id('wf-run-resume').click()
        page.wait_for_selector('[data-testid="wf-resume-snapshot"]', timeout=8000)
        time.sleep(0.4)
        resume = page.inner_text('[role="dialog"]')
        for t in ['time-travel', '断点节点上下文快照', '上游输入', '修参（仅作用于本 Run', '以新分支恢复']:
            check('wf-resume:' + t + suffix, t in resume)
        modal_geometry(page, 'wf-resume' + suffix, '[data-testid="wf-resume-snapshot"]')
        shot(page, f'workflow-resume-modal{suffix}')
        page.keyboard.press('Escape')

        # 11) IX-GRP-10 提交发布（Modal 560px：版本摘要 + 校验状态 + 治理分流）
        page.get_by_test_id('wf-publish-open').click()
        page.wait_for_selector('[data-testid="wf-publish-note"]', timeout=8000)
        page.get_by_test_id('wf-publish-note').fill('新增并行分支与断点 BP-1；条件阈值按 9 月故障台账调整为 7。')
        time.sleep(0.4)
        pub = page.inner_text('[role="dialog"]')
        for t in ['版本摘要', 'DAG 无环校验', '无裸 LLM 分支', 'workflow_publish 工单', '当前 · team']:
            check('wf-publish:' + t + suffix, t in pub)
        modal_geometry(page, 'wf-publish' + suffix, '[role="dialog"]')
        shot(page, f'workflow-publish-modal{suffix}')
        page.keyboard.press('Escape')

        # 12) IX-GRP-11 版本对比/回滚（Modal：双列版本 + 差异计数 + 以旧版新建草稿）
        page.get_by_test_id('wf-versions-open').click()
        page.wait_for_selector('[data-testid="wf-version-diff"]', timeout=8000)
        time.sleep(0.4)
        ver = page.inner_text('[role="dialog"]')
        for t in ['对比基线', 'v1', 'v2', '回滚 = 以旧版新建草稿', '以 v2 新建草稿']:
            check('wf-version:' + t + suffix, t in ver)
        modal_geometry(page, 'wf-version' + suffix, '[role="dialog"]')
        shot(page, f'workflow-version-modal{suffix}')
        page.keyboard.press('Escape')

        page.close()
        browser.close()


if __name__ == '__main__':
    run('light', '')
    run('dark', '-dark')
    fails = [r for r in results if not r['ok']]
    print(json.dumps({'total': len(results), 'failed': len(fails)}, ensure_ascii=False))
    if fails:
        print(json.dumps(fails, ensure_ascii=False, indent=1))
        sys.exit(1)
    print('ALL S7 SHOTS PASS')
