import { createClientMessageId, type ChatMessage, type Conversation } from '@/api/messaging'
import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'

export function getMerchantExclusiveConversation(token: string): Promise<ApiResult<Conversation>> {
  return apiRequest('/merchant/support/exclusive-conversation', { method: 'PUT' }, token)
}

export function listMerchantExclusiveMessages(token: string): Promise<ApiResult<{ items: ChatMessage[] }>> {
  return apiRequest('/merchant/support/exclusive-conversation/messages?limit=100', {}, token)
}

export function sendMerchantExclusiveMessage(text: string, token: string): Promise<ApiResult<ChatMessage>> {
  return apiRequest('/merchant/support/exclusive-conversation/messages', {
    method: 'POST',
    body: JSON.stringify({
      client_message_id: createClientMessageId(),
      content: { type: 'text', text },
    }),
  }, token)
}

export function ensureMerchantHumanService(text: string, messageId: string, token: string) {
  return apiRequest('/merchant/support/exclusive-conversation/human-service-tickets', {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('merchant-platform-support') },
    body: JSON.stringify({ ticket_type: 'general', summary: text, message_refs: [messageId] }),
  }, token)
}
