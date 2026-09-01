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
  it('shows the selected run as expandable Chinese audit sections', () => {
    const wrapper = mount(AgentTracePanel, {
      props: {
        messages: [agentMessage(1, 'run_OLD', '旧任务'), agentMessage(2, 'run_NEW', '用户治理 Agent')],
        selectedRunId: 'run_OLD',
      },
    })
    expect(wrapper.text()).toContain('先拆解问题，再核对权限并执行只读查询')
    expect(wrapper.text()).toContain('旧任务')
    expect(wrapper.text()).toContain('结果')
    expect(wrapper.findAll('details').length).toBeGreaterThanOrEqual(3)
  })

  it('streams the public reasoning summary returned by the provider', () => {
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
          reasoning: '用户在询问当前商品，我需要先找出最相关的规格。',
          chunkIndex: 2,
        },
      },
    })
    expect(wrapper.text()).toContain('用户在询问当前商品')
    expect(wrapper.text()).toContain('思考中')
    expect(wrapper.text()).not.toContain('hello')
    expect(wrapper.text()).not.toContain('正在识别问题')
  })

  it('does not render an English-only provider summary and falls back to Chinese audit text', () => {
    const message = agentMessage(1, 'run_EN', '核对当前商品')
    const trace = message.content!.execution_trace as Record<string, unknown>
    trace.analysis_summary = 'I need to inspect the product and answer the user.'
    const wrapper = mount(AgentTracePanel, { props: { messages: [message] } })
    expect(wrapper.text()).not.toContain('I need to inspect')
    expect(wrapper.text()).toContain('只在当前身份')
  })

  it('clears the previous summary while a new run is waiting for its first delta', () => {
    const wrapper = mount(AgentTracePanel, {
      props: {
        messages: [agentMessage(1, 'run_OLD', '旧任务')],
        running: true,
        liveTrace: {
          runId: 'run_NEW',
          question: '',
          stage: 'understanding',
          label: '',
          summary: '',
          reasoning: '',
          chunkIndex: 0,
        },
      },
    })
    expect(wrapper.text()).toContain('思考中')
    expect(wrapper.text()).not.toContain('先拆解问题，再核对权限并执行只读查询')
  })

  it('keeps the reasoning body empty before the first request', () => {
    const wrapper = mount(AgentTracePanel, { props: { messages: [] } })
    expect(wrapper.text()).toContain('等待中')
    expect(wrapper.find('.agent-reasoning-body').text()).toBe('')
  })
})
