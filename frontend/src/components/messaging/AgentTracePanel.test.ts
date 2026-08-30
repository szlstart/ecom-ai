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
        orchestration_mode: 'multi_agent', answer_mode: 'model_grounded', confidence: 'high',
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
    expect(wrapper.text()).toContain('旧任务')
    expect(wrapper.text()).not.toContain('用户治理 Agent')
    expect(wrapper.text()).toContain('多智能体协作')
    expect(wrapper.text()).toContain('工具调用：1 次')
    expect(wrapper.text()).toContain('耗时：28 ms')
    expect(wrapper.text()).toContain('不显示模型的私密推理文本')
    expect(wrapper.text()).not.toContain('运行编号')
    expect(wrapper.findAll('details')).toHaveLength(2)
    expect(wrapper.find('details').attributes('open')).toBeUndefined()
  })
})
