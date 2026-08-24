import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'
export interface DeliveryEstimate { type: 'delivery'; status: 'available' | 'unavailable'; min_at: string | null; max_at: string | null; source: 'shipping_template' | 'carrier' | null; updated_at: string | null; disclaimer: string }
export interface ShipmentTrack { track_status: string; description: string; location_text: string | null; occurred_at: string; received_at: string }
export interface ShipmentItem { order_item_id: string; product_name: string; sku_name: string; quantity: number }
export interface ShipmentSummary { shipment_id: string; carrier_code: string; carrier_name: string; tracking_no_masked: string; shipment_status: string; items: ShipmentItem[]; delivery_estimate: DeliveryEstimate; last_track: ShipmentTrack | null; last_synced_at: string | null }
export interface ShipmentDetail extends ShipmentSummary { order_id: string; tracking_no: string; latest_tracks: ShipmentTrack[]; version: number }
export function listOrderShipments(orderId: string, token: string): Promise<ApiResult<{ order_id: string; items: ShipmentSummary[] }>> { return apiRequest(`/orders/${encodeURIComponent(orderId)}/shipments`, {}, token) }
export function getShipment(shipmentId: string, token: string): Promise<ApiResult<ShipmentDetail>> { return apiRequest(`/shipments/${encodeURIComponent(shipmentId)}`, {}, token) }
export function listShipmentTracks(shipmentId: string, token: string): Promise<ApiResult<{ shipment_id: string; items: ShipmentTrack[] }>> { return apiRequest(`/shipments/${encodeURIComponent(shipmentId)}/tracks`, {}, token) }
export function refreshShipment(shipmentId: string, token: string): Promise<ApiResult<{ shipment_id: string; status: 'queued'; requested_at: string }>> { return apiRequest(`/shipments/${encodeURIComponent(shipmentId)}/refreshes`, { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('shipment-refresh') } }, token) }
