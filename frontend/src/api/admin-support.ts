import { createClientMessageId, type ChatMessage, type Conversation, type ConversationClear, type ConversationContext, type ConversationDeletion, type MessagePage } from '@/api/messaging'
import { apiRequest, createIdempotencyKey, retryTransientNetworkRequest, type ApiResult } from '@/api/http'

export type SupportTicketStatus = 'queued' | 'assigned' | 'active' | 'waiting_user' | 'resolved' | 'closed'

export interface SupportTicket {
  ticket_id: string
  conversation_id: string
  queue_type: 'store' | 'platform'
  queue_code: string
  ticket_type: string
  priority: 'low' | 'normal' | 'high' | 'urgent'
  ticket_status: SupportTicketStatus
  assigned_user_id: string | null
  handoff_summary: string
  sla_due_at: string | null
  waiting_reason_code: string | null
  unread_count: number
  created_at: string
  updated_at: string
  version: number
  handoff_message_refs?: Array<Record<string, unknown>>
  handoff_policy_version?: string
  resolution_summary?: string | null
}

export interface SupportWorkspace {
  ticket: SupportTicket
  user: { user_id: string; nickname: string; account_status: string }
  referenced_messages: ChatMessage[]
  business_contexts: ConversationContext[]
  events: Array<{ event_id: string; event_type: string; from_status: string | null; to_status: string; reason_code: string | null; reason: string | null; occurred_at: string }>
}

export interface SupportConversation {
  conversation_id: string
  conversation_type: 'exclusive' | 'store'
  participant_type: 'user' | 'merchant'
  participant_id: string
  participant_name: string
  participant_avatar_url: string | null
  store_id: string | null
  conversation_status: 'active' | 'human_pending' | 'human_active' | 'closed'
  last_message_preview: string | null
  last_message_at: string | null
  unread_count: number
  requires_human: boolean
  active_ticket_id: string | null
  active_ticket_status: SupportTicketStatus | null
  assigned_user_id: string | null
}

export function listSupportConversations(filters: { participantType?: 'user' | 'merchant' } = {}, token: string): Promise<ApiResult<{ items: SupportConversation[] }>> {
  const query = new URLSearchParams({ limit: '200' })
  if (filters.participantType) query.set('participant_type', filters.participantType)
  return apiRequest(`/support/conversations?${query}`, {}, token)
}

export interface SupportInternalNote {
  note_id: string
  author_user_id: string
  note_type: string
  text: string
  visibility_scope: string
  created_at: string
}

export function listSupportTickets(filters: { queueType?: string; status?: SupportTicketStatus }, token: string): Promise<ApiResult<{ items: SupportTicket[] }>> {
  const query = new URLSearchParams({ limit: '100' })
  if (filters.queueType) query.set('queue_type', filters.queueType)
  if (filters.status) query.set('ticket_status', filters.status)
  return apiRequest(`/support/human-service-tickets?${query}`, {}, token)
}

export function getSupportWorkspace(ticketId: string, token: string): Promise<ApiResult<SupportWorkspace>> {
  return apiRequest(`/support/human-service-tickets/${encodeURIComponent(ticketId)}/workspace`, {}, token)
}

export function listSupportMessages(conversationId: string, token: string, options: { cursor?: string; afterSequence?: number } = {}): Promise<ApiResult<MessagePage>> {
  const query = new URLSearchParams({ limit: '100' })
  if (options.cursor) query.set('cursor', options.cursor)
  if (options.afterSequence) query.set('after_sequence', String(options.afterSequence))
  return apiRequest(`/support/conversations/${encodeURIComponent(conversationId)}/messages?${query}`, {}, token)
}

export function deleteSupportConversation(conversationId: string, token: string): Promise<ApiResult<ConversationDeletion>> {
  return apiRequest(`/support/conversations/${encodeURIComponent(conversationId)}`, { method: 'DELETE' }, token)
}

export function clearSupportConversationHistory(conversationId: string, token: string): Promise<ApiResult<ConversationClear>> {
  return apiRequest(`/support/conversations/${encodeURIComponent(conversationId)}/messages`, { method: 'DELETE' }, token)
}

export function listSupportNotes(ticketId: string, token: string): Promise<ApiResult<{ items: SupportInternalNote[] }>> {
  return apiRequest(`/support/human-service-tickets/${encodeURIComponent(ticketId)}/internal-notes`, {}, token)
}

function command(ticketId: string, action: string, version: number, body: object | undefined, token: string): Promise<ApiResult<SupportTicket>> {
  return apiRequest(`/support/human-service-tickets/${encodeURIComponent(ticketId)}/${action}`, {
    method: 'POST',
    headers: { 'If-Match': `"v${version}"`, 'Idempotency-Key': createIdempotencyKey(`support-${action}`) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  }, token)
}

export function claimSupportTicket(ticket: SupportTicket, token: string) { return command(ticket.ticket_id, 'claims', ticket.version, undefined, token) }
export function waitSupportTicket(ticket: SupportTicket, reasonCode: string, reason: string, token: string) { return command(ticket.ticket_id, 'waits', ticket.version, { reason_code: reasonCode, reason }, token) }
export function resumeSupportTicket(ticket: SupportTicket, token: string) { return command(ticket.ticket_id, 'resumptions', ticket.version, undefined, token) }
export function transferSupportTicket(ticket: SupportTicket, assignedUserId: string, reason: string, token: string) { return command(ticket.ticket_id, 'transfers', ticket.version, { assigned_user_id: assignedUserId, reason }, token) }
export function resolveSupportTicket(ticket: SupportTicket, resolutionCode: string, summary: string, internalNote: string | null, token: string) { return command(ticket.ticket_id, 'resolutions', ticket.version, { resolution_code: resolutionCode, summary, internal_note: internalNote || null }, token) }
export function createSupportNote(ticket: SupportTicket, text: string, noteType: string, token: string) { return command(ticket.ticket_id, 'internal-notes', ticket.version, { text, note_type: noteType, visibility_scope: 'current_queue' }, token) }

export function sendSupportMessage(conversationId: string, text: string, token: string, clientMessageId = createClientMessageId()): Promise<ApiResult<ChatMessage>> {
  return apiRequest(`/support/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: 'POST',
    body: JSON.stringify({ client_message_id: clientMessageId, text }),
  }, token)
}

export function sendSupportMessageResilient(
  conversationId: string,
  text: string,
  token: string,
  clientMessageId = createClientMessageId(),
): Promise<ApiResult<ChatMessage>> {
  return retryTransientNetworkRequest(
    () => sendSupportMessage(conversationId, text, token, clientMessageId),
  )
}

export function sendSupportProductCard(
  conversationId: string,
  productId: string,
  skuId: string | null,
  token: string,
  clientMessageId = createClientMessageId(),
): Promise<ApiResult<ChatMessage>> {
  return apiRequest(`/support/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: 'POST',
    body: JSON.stringify({ client_message_id: clientMessageId, product_id: productId, sku_id: skuId }),
  }, token)
}

export function putSupportReadCursor(conversationId: string, message: ChatMessage, token: string): Promise<ApiResult<{ conversation_id: string; last_read_message_id: string; last_read_sequence_no: number; unread_count: number; cursor_version: number }>> {
  return apiRequest(`/support/conversations/${encodeURIComponent(conversationId)}/read-cursor`, {
    method: 'PUT',
    body: JSON.stringify({ last_read_message_id: message.message_id, last_read_sequence_no: message.sequence_no }),
  }, token)
}

export function getAdminAiConversation(token: string): Promise<ApiResult<Conversation>> {
  return apiRequest('/admin/support/ai-conversation', { method: 'PUT' }, token)
}

export function deleteAdminAiConversation(token: string): Promise<ApiResult<ConversationDeletion>> {
  return apiRequest('/admin/support/ai-conversation', { method: 'DELETE' }, token)
}

export function clearAdminAiConversationHistory(token: string): Promise<ApiResult<ConversationClear>> {
  return apiRequest('/admin/support/ai-conversation/messages', { method: 'DELETE' }, token)
}

export function listAdminAiMessages(token: string, options: { cursor?: string; afterSequence?: number } = {}): Promise<ApiResult<MessagePage>> {
  const query = new URLSearchParams({ limit: '100' })
  if (options.cursor) query.set('cursor', options.cursor)
  if (options.afterSequence) query.set('after_sequence', String(options.afterSequence))
  return apiRequest(`/admin/support/ai-conversation/messages?${query}`, {}, token)
}

export function sendAdminAiMessage(text: string, token: string, clientMessageId = createClientMessageId()): Promise<ApiResult<ChatMessage>> {
  return apiRequest('/admin/support/ai-conversation/messages', {
    method: 'POST',
    body: JSON.stringify({
      client_message_id: clientMessageId,
      content: { type: 'text', text },
    }),
  }, token)
}

export function sendAdminAiMessageResilient(
  text: string,
  token: string,
  clientMessageId = createClientMessageId(),
): Promise<ApiResult<ChatMessage>> {
  return retryTransientNetworkRequest(() => sendAdminAiMessage(text, token, clientMessageId))
}

export function putAdminAiReadCursor(message: ChatMessage, token: string): Promise<ApiResult<{ unread_count: number }>> {
  return apiRequest('/admin/support/ai-conversation/read-cursor', {
    method: 'PUT',
    body: JSON.stringify({
      last_read_message_id: message.message_id,
      last_read_sequence_no: message.sequence_no,
    }),
  }, token)
}
