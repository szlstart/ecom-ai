import { createClientMessageId, type ChatMessage, type Conversation, type MessagePage, type ReadCursor } from '@/api/messaging'
import { apiRequest, createIdempotencyKey, retryTransientNetworkRequest, type ApiResult } from '@/api/http'

export function getMerchantExclusiveConversation(token: string): Promise<ApiResult<Conversation>> {
  return apiRequest('/merchant/support/exclusive-conversation', { method: 'PUT' }, token)
}

export function listMerchantExclusiveMessages(token: string, options: { cursor?: string; afterSequence?: number } = {}): Promise<ApiResult<MessagePage>> {
  const query = new URLSearchParams({ limit: '100' })
  if (options.cursor) query.set('cursor', options.cursor)
  if (options.afterSequence) query.set('after_sequence', String(options.afterSequence))
  return apiRequest(`/merchant/support/exclusive-conversation/messages?${query}`, {}, token)
}

export function putMerchantExclusiveReadCursor(message: ChatMessage, token: string): Promise<ApiResult<ReadCursor>> {
  return apiRequest('/merchant/support/exclusive-conversation/read-cursor', {
    method: 'PUT',
    body: JSON.stringify({ last_read_message_id: message.message_id, last_read_sequence_no: message.sequence_no }),
  }, token)
}

export function sendMerchantExclusiveMessage(text: string, token: string, clientMessageId = createClientMessageId()): Promise<ApiResult<ChatMessage>> {
  return apiRequest('/merchant/support/exclusive-conversation/messages', {
    method: 'POST',
    body: JSON.stringify({
      client_message_id: clientMessageId,
      content: { type: 'text', text },
    }),
  }, token)
}

export function sendMerchantExclusiveMessageResilient(
  text: string,
  token: string,
  clientMessageId = createClientMessageId(),
): Promise<ApiResult<ChatMessage>> {
  return retryTransientNetworkRequest(
    () => sendMerchantExclusiveMessage(text, token, clientMessageId),
  )
}

export function ensureMerchantHumanService(text: string, messageId: string, token: string) {
  return apiRequest('/merchant/support/exclusive-conversation/human-service-tickets', {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('merchant-platform-support') },
    body: JSON.stringify({ ticket_type: 'general', summary: text, message_refs: [messageId] }),
  }, token)
}
