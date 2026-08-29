import { describe, expect, it } from 'vitest'

import { resolveAgentHealth, type ReadinessHealth } from './health'

function readiness(agentRuntime: string, agentModel: string): ReadinessHealth {
  return {
    status: 'ready',
    dependencies: {
      agent_runtime: { status: agentRuntime as 'up' },
      agent_model: { status: agentModel as 'up' },
    },
  }
}

describe('resolveAgentHealth', () => {
  it('requires both a published runtime and a working model', () => {
    expect(resolveAgentHealth(readiness('up', 'up'))).toBe('available')
    expect(resolveAgentHealth(readiness('degraded', 'up'))).toBe('degraded')
    expect(resolveAgentHealth(readiness('up', 'down'))).toBe('unavailable')
  })

  it('does not claim online before health is known', () => {
    expect(resolveAgentHealth(null)).toBe('unknown')
  })
})
