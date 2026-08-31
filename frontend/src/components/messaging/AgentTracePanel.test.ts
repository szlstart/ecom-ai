import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/api/messaging'

import AgentTracePanel from './AgentTracePanel.vue'

function agentMessage(sequence: number, runId: string, label: string): ChatMessage {
  return {
    message_id: `msg_${sequence}`, sequence_no: sequence, sender_type: 'agent', message_type: 'text',
    text: '受控回复', message_status: 'sent', moderation_status: 'passed', viewer_reaction: null,
    sent_at: '2026-08-30T08:00:00Z',
    content: {
      run_id: runId,
      execution_trace: {
        run_id: runId, agent: 'AI 管家', status: 'completed', intent: 'complex_platform_diagnosis',
        question: '请分析平台用户和店铺情况',
        analysis_summary: '先拆解问题，再核对权限并执行只读查询。',
        analysis_details: ['已识别当前请求。', '业务工具 governance.users 已通过权限网关执行，返回 2 项结果。'],
        result_summary: '已完成 3 个受控步骤，获得 2 项可用结果。',
        orchestration_mode: 'multi_agent', answer_mode: 'model_grounded', confidence: 'high',
        thinking_mode: 'enabled',
        source_ids: ['tool:governance.users'],
        cited_source_ids: ['tool:governance.users'],
        steps: [{ kind: 'delegation', label, status: 'succeeded', specialist: 'governance_users', tool_calls: 1, latency_ms: 28 }],
      },
    },
  }
}

describe('AgentTracePanel', () => {
  it('shows safe multi-agent metadata for the selected run without raw reasoning', () => {
    const wrapper = mount(AgentTracePanel, {
      props: {
        messages: [agentMessage(1, 'run_OLD', '旧任务'), agentMessage(2, 'run_NEW', '用户治理 Agent')],
        selectedRunId: 'run_OLD',
      },
    })
    expect(wrapper.text()).toContain('多智能体协作')
    expect(wrapper.text()).toContain('请分析平台用户和店铺情况')
    expect(wrapper.text()).toContain('先拆解问题，再核对权限并执行只读查询')
    expect(wrapper.text()).toContain('业务工具 governance.users')
    expect(wrapper.text()).toContain('本次实际意图')
    expect(wrapper.text()).toContain('Kimi K2.6 思考模式')
    expect(wrapper.text()).toContain('可信依据')
    expect(wrapper.text()).not.toContain('理解当前消息')
    expect(wrapper.text()).not.toContain('重建最近对话上下文')
    expect(wrapper.text()).not.toContain('生成安全回复')
    expect(wrapper.text()).not.toContain('结果整理完成')
    expect(wrapper.text()).not.toContain('参考内容')
    expect(wrapper.text()).not.toContain('运行编号')
    expect(wrapper.text()).toContain('旧任务')
    expect(wrapper.find('section[aria-label="实际执行步骤"]').exists()).toBe(true)
    expect(wrapper.findAll('details')).toHaveLength(3)
    expect(wrapper.findAll('details').every((item) => item.attributes('open') === undefined)).toBe(true)
  })

  it('shows the real started-event summary without placing a fake tool step', () => {
    const wrapper = mount(AgentTracePanel, {
      props: {
        messages: [],
        running: true,
        liveTrace: {
          runId: 'run_LIVE',
          question: 'hello',
          stage: 'understanding',
          label: '思考开始',
          summary: '正在识别问题、会话上下文、身份范围和可用权限。',
        },
      },
    })
    expect(wrapper.text()).toContain('hello')
    expect(wrapper.text()).toContain('思考开始')
    expect(wrapper.text()).toContain('正在识别问题')
    expect(wrapper.text()).not.toContain('调用工具')
  })

  it('makes a failed server run visible instead of leaving the panel thinking', () => {
    const message = agentMessage(3, 'run_FAILED', '安全停止异常流程')
    ;(message.content!.execution_trace as Record<string, unknown>).status = 'failed'
    const wrapper = mount(AgentTracePanel, { props: { messages: [message] } })
    expect(wrapper.text()).toContain('处理失败')
    expect(wrapper.text()).toContain('分析与计划')
    expect(wrapper.text()).not.toContain('思考中')
  })
})
