import { apiRequest, createIdempotencyKey, retryTransientNetworkRequest, type ApiResult } from '@/api/http'

export interface Conversation {
  conversation_id: string
  conversation_type: 'exclusive' | 'store'
  conversation_status: 'active' | 'human_pending' | 'human_active' | 'closed'
  store_id: string | null
  title: string
  is_fixed: boolean
  fixed_rank: number | null
  last_message_preview: string | null
  last_message_at: string | null
  last_sequence_no: number
  unread_count: number
  version: number
  active_contexts: ConversationContext[]
}

export interface ConversationContext {
  context_id: string
  context_type: 'product' | 'order' | 'shipment' | 'refund' | 'store' | 'checkout_store_group'
  resource_id: string
  resource_version: number | null
  context_version: number
  status: 'active' | 'inactive' | 'expired'
  display_snapshot: Record<string, unknown>
  expires_at: string | null
}

export interface ChatMessage {
  message_id: string
  sequence_no: number
  sender_type: 'user' | 'agent' | 'human' | 'system' | 'tool'
  message_type: 'text' | 'product_card' | 'order_card' | 'system' | string
  text: string | null
  message_status: string
  moderation_status: string
  content: Record<string, unknown> | null
  viewer_reaction: 'thumb_up' | 'thumb_down' | null
  sent_at: string
}

export interface MessagePage {
  items: ChatMessage[]
  previous_cursor: string | null
}

export interface AiFeedback {
  feedback_id: string | null
  message_id: string
  feedback_type: 'thumb_up' | 'thumb_down' | 'report' | 'correction' | null
  status: 'submitted' | 'withdrawn' | 'reviewed' | 'resolved' | 'dismissed' | null
  created_at: string | null
}

export interface ReadCursor {
  conversation_id: string
  last_read_message_id: string
  last_read_sequence_no: number
  unread_count: number
  total_unread_count: number
  cursor_version: number
}

export interface HumanServiceTicket {
  ticket_id: string
  conversation_id: string
  queue_type: 'store' | 'platform'
  ticket_status: 'queued' | 'assigned' | 'active' | 'waiting_user' | 'resolved' | 'closed'
  assigned_user_id: string | null
  resolution_summary: string | null
  queue_position: number | null
  estimated_response_at: string | null
  can_cancel: boolean
}

export interface ConversationArchive {
  conversation_id: string
  archived_at: string
  version: number
}

export interface ConversationDeletion {
  conversation_id: string
  deleted_at: string
  memory_cleared: boolean
}

export function deleteConversation(conversationId: string, token: string): Promise<ApiResult<ConversationDeletion>> {
  return apiRequest(`/conversations/${encodeURIComponent(conversationId)}`, { method: 'DELETE' }, token)
}

export function listConversations(token: string): Promise<ApiResult<{ items: Conversation[] }>> {
  return apiRequest('/conversations', {}, token)
}

export function ensureExclusiveConversation(token: string): Promise<ApiResult<Conversation>> {
  return apiRequest('/users/me/exclusive-conversation', { method: 'PUT' }, token)
}

export function ensureStoreConversation(storeId: string, token: string): Promise<ApiResult<Conversation>> {
  return apiRequest(`/stores/${encodeURIComponent(storeId)}/customer-service-conversation`, { method: 'PUT' }, token)
}

export function getConversation(conversationId: string, token: string): Promise<ApiResult<Conversation>> {
  return apiRequest(`/conversations/${encodeURIComponent(conversationId)}`, {}, token)
}

export function setConversationContext(
  conversationId: string,
  version: number,
  contextType: ConversationContext['context_type'],
  resourceId: string,
  resourceVersion: number | null,
  token: string,
): Promise<ApiResult<ConversationContext>> {
  return apiRequest(
    `/conversations/${encodeURIComponent(conversationId)}/contexts/${encodeURIComponent(contextType)}`,
    {
      method: 'PUT',
      headers: { 'If-Match': `"v${version}"` },
      body: JSON.stringify({ resource_id: resourceId, resource_version: resourceVersion }),
    },
    token,
  )
}

export function listMessages(
  conversationId: string,
  token: string,
  options: { afterSequence?: number; cursor?: string; limit?: number } = {},
): Promise<ApiResult<MessagePage>> {
  const query = new URLSearchParams({ limit: String(options.limit ?? 50) })
  if (options.afterSequence) query.set('after_sequence', String(options.afterSequence))
  if (options.cursor) query.set('cursor', options.cursor)
  return apiRequest(`/conversations/${encodeURIComponent(conversationId)}/messages?${query}`, {}, token)
}

export function createClientMessageId(): string {
  return `cmsg_${crypto.randomUUID().replaceAll('-', '').toUpperCase()}`
}

export function sendText(
  conversationId: string,
  text: string,
  token: string,
  clientMessageId = createClientMessageId(),
): Promise<ApiResult<ChatMessage>> {
  return apiRequest(`/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: 'POST',
    body: JSON.stringify({ client_message_id: clientMessageId, content: { type: 'text', text } }),
  }, token)
}

/**
 * Recover a chat send from a brief API restart or local network handover.
 * Every retry reuses client_message_id, so the server can return the already
 * persisted message instead of creating a duplicate when the first response
 * was lost after the write committed.
 */
export async function sendTextResilient(
  conversationId: string,
  text: string,
  token: string,
  clientMessageId = createClientMessageId(),
  retryDelays: readonly number[] = [400, 900, 1_800, 3_600],
  onRetry?: () => void,
): Promise<ApiResult<ChatMessage>> {
  return retryTransientNetworkRequest(
    () => sendText(conversationId, text, token, clientMessageId),
    retryDelays,
    onRetry,
  )
}

export function sendProductCard(
  conversationId: string,
  productId: string,
  skuId: string | null,
  token: string,
  clientMessageId = createClientMessageId(),
): Promise<ApiResult<ChatMessage>> {
  return apiRequest(`/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: 'POST',
    body: JSON.stringify({
      client_message_id: clientMessageId,
      content: { type: 'product_card', product_id: productId, sku_id: skuId },
    }),
  }, token)
}

export function sendOrderCard(
  conversationId: string,
  orderId: string,
  token: string,
  clientMessageId = createClientMessageId(),
): Promise<ApiResult<ChatMessage>> {
  return apiRequest(`/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: 'POST',
    body: JSON.stringify({
      client_message_id: clientMessageId,
      content: { type: 'order_card', order_id: orderId },
    }),
  }, token)
}

export function respondResolutionCheck(
  conversationId: string,
  messageId: string,
  resolved: boolean,
  token: string,
  clientMessageId = createClientMessageId(),
): Promise<ApiResult<MessagePage>> {
  return apiRequest(`/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/resolution-responses`, {
    method: 'POST',
    body: JSON.stringify({ client_message_id: clientMessageId, resolved }),
  }, token)
}

export function setAiMessageReaction(
  conversationId: string,
  messageId: string,
  reaction: 'thumb_up' | 'thumb_down',
  token: string,
): Promise<ApiResult<AiFeedback>> {
  return apiRequest(
    `/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/reaction`,
    { method: 'PUT', body: JSON.stringify({ reaction }) },
    token,
  )
}

export function removeAiMessageReaction(
  conversationId: string,
  messageId: string,
  token: string,
): Promise<ApiResult<AiFeedback>> {
  return apiRequest(
    `/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/reaction`,
    { method: 'DELETE' },
    token,
  )
}

export function submitAiMessageFeedback(
  conversationId: string,
  messageId: string,
  kind: 'reports' | 'corrections',
  reasonCode: string,
  comment: string,
  token: string,
): Promise<ApiResult<AiFeedback>> {
  return apiRequest(
    `/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/${kind}`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': createIdempotencyKey(`ai-feedback-${kind}`) },
      body: JSON.stringify({ reason_code: reasonCode, comment }),
    },
    token,
  )
}

export function putReadCursor(
  conversationId: string,
  message: Pick<ChatMessage, 'message_id' | 'sequence_no'>,
  token: string,
): Promise<ApiResult<ReadCursor>> {
  return apiRequest(`/conversations/${encodeURIComponent(conversationId)}/read-cursor`, {
    method: 'PUT',
    body: JSON.stringify({
      last_read_message_id: message.message_id,
      last_read_sequence_no: message.sequence_no,
    }),
  }, token)
}

export function archiveConversation(
  conversationId: string,
  version: number,
  token: string,
): Promise<ApiResult<ConversationArchive>> {
  return apiRequest(`/conversations/${encodeURIComponent(conversationId)}/archivals`, {
    method: 'POST',
    headers: {
      'If-Match': `"v${version}"`,
      'Idempotency-Key': createIdempotencyKey('conversation-archive'),
    },
  }, token)
}

export function requestHumanService(
  conversationId: string,
  summary: string,
  messageRefs: string[],
  token: string,
): Promise<ApiResult<HumanServiceTicket>> {
  return apiRequest(`/conversations/${encodeURIComponent(conversationId)}/human-service-tickets`, {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('human-service') },
    body: JSON.stringify({ ticket_type: 'general', summary, message_refs: messageRefs }),
  }, token)
}

export function getHumanServiceTicket(conversationId: string, token: string): Promise<ApiResult<HumanServiceTicket>> {
  return apiRequest(`/conversations/${encodeURIComponent(conversationId)}/human-service-ticket`, {}, token)
}

export function cancelHumanServiceTicket(ticketId: string, token: string): Promise<ApiResult<HumanServiceTicket>> {
  return apiRequest(`/human-service-tickets/${encodeURIComponent(ticketId)}/cancellations`, {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('human-service-cancel') },
  }, token)
}
