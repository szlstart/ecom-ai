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

  it('does not discard the provider analysis summary returned for the selected run', () => {
    const message = agentMessage(1, 'run_EN', '核对当前商品')
    const trace = message.content!.execution_trace as Record<string, unknown>
    trace.analysis_summary = 'I need to inspect the product and answer the user.'
    const wrapper = mount(AgentTracePanel, { props: { messages: [message] } })
    expect(wrapper.text()).toContain('I need to inspect the product and answer the user.')
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

  it('labels a persisted approval checkpoint as waiting for confirmation', () => {
    const message = agentMessage(1, 'run_WAITING', '等待用户确认')
    const trace = message.content!.execution_trace as Record<string, unknown>
    trace.status = 'waiting_confirmation'
    const wrapper = mount(AgentTracePanel, { props: { messages: [message] } })
    expect(wrapper.text()).toContain('等待确认')
    expect(wrapper.text()).not.toContain('思考中')
  })

  it('shows orchestration, exact tool records, RAG, memory, context and model metrics', () => {
    const message = agentMessage(1, 'run_AUDIT', '商品 Agent')
    const trace = message.content!.execution_trace as Record<string, unknown>
    trace.version = 'auditable-agent-trace-v3'
    trace.orchestration_trace = {
      mode: 'multi_agent', supervisor: '专属客服 Supervisor Agent', delegation_count: 1,
      planning_source: 'provider_model_supervisor', execution_strategy: 'parallel_read_only',
      goal_ledger: [{ goal_key: 'goal_1', description: '找出符合条件的蓝色文具', assigned_task_key: 'task_1' }],
      coverage_complete: true,
      subtasks: [{ specialist: '商品选购 Agent', intent: 'product_search', objective: '查找蓝色文具', allowed_tools: ['catalog.search_products'] }],
      delegations: [],
    }
    const toolCalls = [{ tool_code: 'catalog.search_products', arguments: { query: '蓝色文具' }, status: 'succeeded', result: { items: [{ product_id: 'prd_1' }] }, result_count: 1, latency_ms: 16 }]
    trace.tool_calls = toolCalls
    trace.knowledge_trace = { retrieval: { retrieval_mode: 'hybrid' }, matches: [{ document_id: 'kdoc_1', title: '平台规则' }] }
    trace.memory_trace = { recall: { used_count: 1 }, items: [{ memory_id: 'mem_1', value: '喜欢蓝色' }] }
    trace.context_trace = { included_count: 2, recent_turns: [{ role: '用户', text: '帮我找文具' }] }
    trace.model_invocation = { model: 'gpt-test', input_tokens: 100, output_tokens: 30 }
    const step = (trace.steps as Array<Record<string, unknown>>)[0]!
    step.tool_code = 'catalog.search_products'
    step.tool_call = toolCalls[0]

    const wrapper = mount(AgentTracePanel, { props: { messages: [message] } })
    const text = wrapper.text()
    expect(text).toContain('Supervisor 与子 Agent 分配')
    expect(text).toContain('商品选购 Agent')
    expect(text).toContain('provider_model_supervisor')
    expect(text).toContain('parallel_read_only')
    expect(text).toContain('Supervisor 目标账本')
    expect(text).toContain('找出符合条件的蓝色文具')
    expect(text).toContain('覆盖完整')
    expect(text).toContain('全部工具调用（1）')
    expect(text).toContain('蓝色文具')
    expect(text).toContain('RAG 知识库检索')
    expect(text).toContain('长期记忆读取与命中')
    expect(text).toContain('短期上下文与连续对话')
    expect(text).toContain('模型调用与 Token 指标')
    expect(text).toContain('模型调用与 Token 指标 · 已调用')
  })
})
