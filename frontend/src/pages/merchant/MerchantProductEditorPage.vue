<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import areaData from 'china-area-data'

import {
  adminCommand,
  adminCreate,
  adminGet,
  adminReplace,
  adminUpdate,
  requireAdminToken,
  type AdminContentVersion,
  type AdminInventory,
  type AdminProduct,
  type AdminProductAttribute,
  type AdminProductFaq,
  type AdminProductImage,
  type AdminShippingTemplate,
  type AdminSku,
  type AdminStore,
} from '@/api/admin-catalog'
import { getCategories, type Category } from '@/api/catalog'
import { listAdminReviews, replyAdminReview, type AdminReview } from '@/api/admin-reviews'
import { errorMessage, resolveApiAssetUrl } from '@/api/http'
import AdminFileUpload from '@/components/AdminFileUpload.vue'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'
import { imageFileFromClipboard } from '@/utils/clipboard-image'

interface Fulfillment { shipping_template_id: string; origin_region_code: string; dispatch_min_hours: number; dispatch_max_hours: number; purchase_notice: string | null; profile_version: number; version: number }
interface FileUploadHandle { uploadFile: (file: File) => Promise<void> }
interface RegionOption { code: string; name: string }
interface DetailBlock {
  key: string
  type: 'paragraph' | 'heading' | 'bullet_list' | 'image'
  text: string
  items: string[]
  file_id: string
  alt: string
  level: 2 | 3
}
interface FaqDraft { key: string; faq_id: string | null; question: string; answer: string }

const route = useRoute(); const router = useRouter(); const auth = useAdminAuthStore()
const isNew = computed(() => route.path.endsWith('/new'))
const productId = computed(() => String(route.params.productId || ''))
const product = ref<AdminProduct | null>(null)
const store = ref<AdminStore | null>(null)
const skus = ref<AdminSku[]>([])
const inventories = ref<AdminInventory[]>([])
const images = ref<AdminProductImage[]>([])
const attributes = ref<AdminProductAttribute[]>([])
const faqs = ref<AdminProductFaq[]>([])
const reviews = ref<AdminReview[]>([])
const categories = ref<Category[]>([])
const shippingTemplates = ref<AdminShippingTemplate[]>([])
const loading = ref(true); const saving = ref(false); const error = ref(''); const notice = ref('')
const selectedImage = ref(0); const selectedSkuId = ref(''); const skuEditing = ref<AdminSku | null>(null)
const showSkuEditor = ref(false)
const deletingSku = ref<AdminSku | null>(null)
const skuDeleting = ref(false)
const skuDeleteError = ref('')
const skuNameInput = ref<HTMLInputElement | null>(null)
const mainImageArea = ref<HTMLElement | null>(null)
const imageUpload = ref<FileUploadHandle | null>(null)
const detailPasteArea = ref<HTMLElement | null>(null)
const detailImageUpload = ref<FileUploadHandle | null>(null)
const pasteFocused = ref(false)
const pasteBusy = ref(false)
const uploadBusy = ref(false)
const imageSaving = ref(false)
const imageSaveFailed = ref(false)
const pasteError = ref('')
const pasteNotice = ref('')
const detailPasteFocused = ref(false)
const detailPasteBusy = ref(false)
const detailUploadBusy = ref(false)
const detailUploadError = ref('')
const detailUploadNotice = ref('')
let imageSavePromise: Promise<void> | null = null
const replyDrafts = reactive<Record<string, string>>({})
const replyingReviewId = ref('')
const basic = reactive({ store_id: '', category_id: '', brand_id: '', product_name: '' })
const skuForm = reactive({ name: '', sale_price: '', stock: 0 })
const detailBlocks = ref<DetailBlock[]>([])
const faqDrafts = ref<FaqDraft[]>([])
const fulfillment = reactive({ shipping_template_id: '', origin_region_code: '', dispatch_min_hours: 24, dispatch_max_hours: 48, purchase_notice: '' })
const originProvinceCode = ref('')
const originCityCode = ref('')
const excludedProvinceCodes = new Set(['810000', '820000'])
const originProvinces = Object.entries(areaData['86'] ?? {})
  .filter(([code]) => !excludedProvinceCodes.has(code))
  .map(([code, name]) => ({ code, name }))
const originCities = computed<RegionOption[]>(() => Object.entries(areaData[originProvinceCode.value] ?? {}).map(([code, name]) => ({ code, name })))
const flatCategories = computed(() => flatten(categories.value))
const activeSkus = computed(() => skus.value.filter((item) => item.status === 'active'))
const activeSku = computed(() => {
  if (showSkuEditor.value && !skuEditing.value) return null
  return activeSkus.value.find((item) => item.sku_id === selectedSkuId.value) ?? activeSkus.value[0] ?? null
})
const deletingLastActiveSku = computed(() => product.value?.status === 'on_sale' && activeSkus.value.length <= 1)
const activeImages = computed(() => {
  const skuId = activeSku.value?.sku_id
  return skuId ? images.value.filter((item) => item.sku_id === skuId) : []
})
const displayImage = computed(() => activeImages.value[selectedImage.value] ?? activeImages.value[0] ?? null)
const canEdit = computed(() => !product.value || ['draft', 'rejected', 'off_shelf', 'on_sale'].includes(product.value.status))
const editorBusy = computed(() => saving.value || pasteBusy.value || uploadBusy.value || imageSaving.value || detailPasteBusy.value || detailUploadBusy.value)
let detailBlockSequence = 0
let faqDraftSequence = 0

function captureEditorDrafts() {
  return {
    basic: { ...basic },
    skuForm: { ...skuForm },
    skuEditingId: skuEditing.value?.sku_id ?? null,
    showSkuEditor: showSkuEditor.value,
    selectedSkuId: selectedSkuId.value,
    selectedImage: selectedImage.value,
    attributes: attributes.value.map((item) => ({ ...item })),
    detailBlocks: detailBlocks.value.map((item) => ({ ...item, items: [...item.items] })),
    faqDrafts: faqDrafts.value.map((item) => ({ ...item })),
    fulfillment: { ...fulfillment },
    originProvinceCode: originProvinceCode.value,
    originCityCode: originCityCode.value,
  }
}

function restoreEditorDrafts(snapshot: ReturnType<typeof captureEditorDrafts>) {
  Object.assign(basic, snapshot.basic)
  Object.assign(skuForm, snapshot.skuForm)
  skuEditing.value = snapshot.skuEditingId ? (skus.value.find((item) => item.sku_id === snapshot.skuEditingId) ?? null) : null
  showSkuEditor.value = snapshot.showSkuEditor
  selectedSkuId.value = activeSkus.value.some((item) => item.sku_id === snapshot.selectedSkuId) ? snapshot.selectedSkuId : (activeSkus.value[0]?.sku_id ?? '')
  selectedImage.value = Math.max(0, Math.min(snapshot.selectedImage, activeImages.value.length - 1))
  attributes.value = snapshot.attributes
  detailBlocks.value = snapshot.detailBlocks
  faqDrafts.value = snapshot.faqDrafts
  Object.assign(fulfillment, snapshot.fulfillment)
  originProvinceCode.value = snapshot.originProvinceCode
  originCityCode.value = snapshot.originCityCode
}

function token() { return requireAdminToken(auth.accessToken) }
function path(suffix = '') { return `/admin/products/${encodeURIComponent(productId.value)}${suffix}` }
function flatten(nodes: Category[]): Category[] { return nodes.flatMap((item) => [item, ...flatten(item.children)]) }
function minor(value: string) { return Math.round(Number(value || 0) * 100) }
function statusLabel(value?: string) { return ({ draft: '草稿', pending_review: '审核中', approved: '审核通过，待上架', rejected: '审核退回', on_sale: '销售中', off_shelf: '已下架' } as Record<string, string>)[value || ''] ?? value }
function inventoryFor(skuId: string) { return inventories.value.find((item) => item.sku_id === skuId) }
function detailKey() { detailBlockSequence += 1; return `detail-${detailBlockSequence}` }
function faqKey() { faqDraftSequence += 1; return `faq-draft-${faqDraftSequence}` }
function addFaq() { faqDrafts.value.push({ key: faqKey(), faq_id: null, question: '', answer: '' }) }
function blankDetailBlock(type: DetailBlock['type'] = 'paragraph'): DetailBlock { return { key: detailKey(), type, text: '', items: [], file_id: '', alt: '', level: 2 } }
function parseDetailBlocks(version: AdminContentVersion): DetailBlock[] {
  if (version.source_format === 'structured') {
    try {
      const parsed: unknown = JSON.parse(version.source_content)
      if (Array.isArray(parsed)) {
        const blocks = parsed.flatMap((candidate): DetailBlock[] => {
          if (!candidate || typeof candidate !== 'object') return []
          const raw = candidate as Record<string, unknown>
          if (raw.type === 'paragraph' && typeof raw.text === 'string') return [{ ...blankDetailBlock('paragraph'), text: raw.text }]
          if (raw.type === 'heading' && typeof raw.text === 'string') return [{ ...blankDetailBlock('heading'), text: raw.text, level: raw.level === 3 ? 3 : 2 }]
          if (raw.type === 'bullet_list' && Array.isArray(raw.items)) return [{ ...blankDetailBlock('bullet_list'), items: raw.items.filter((item): item is string => typeof item === 'string') }]
          if (raw.type === 'image' && typeof raw.file_id === 'string') return [{ ...blankDetailBlock('image'), file_id: raw.file_id, alt: typeof raw.alt === 'string' ? raw.alt : basic.product_name }]
          return []
        })
        if (blocks.length) return blocks
      }
    } catch { /* Use the safe text fallback below. */ }
  }
  return [{ ...blankDetailBlock('paragraph'), text: version.safe_text || version.source_content }]
}
function addDetailText() { detailBlocks.value.push(blankDetailBlock('paragraph')) }
function moveDetailBlock(index: number, offset: -1 | 1) {
  const target = index + offset
  if (target < 0 || target >= detailBlocks.value.length) return
  const [block] = detailBlocks.value.splice(index, 1)
  if (block) detailBlocks.value.splice(target, 0, block)
}
function removeDetailBlock(index: number) { detailBlocks.value.splice(index, 1) }
function detailBlockLabel(block: DetailBlock) { return ({ paragraph: '文字', heading: '标题', bullet_list: '列表', image: '图片' } as const)[block.type] }
function detailImageUrl(fileId: string) { return resolveApiAssetUrl(`/api/v1/files/${fileId}`) }
function selectOriginProvince() { originCityCode.value = ''; fulfillment.origin_region_code = originProvinceCode.value }
function selectOriginCity() { fulfillment.origin_region_code = originCityCode.value || originProvinceCode.value }
function restoreOriginSelection(regionCode: string) {
  const code = regionCode.replace(/^CN[_-]/, '')
  if (!/^\d{6}$/.test(code)) { originProvinceCode.value = ''; originCityCode.value = ''; return }
  const provinceCode = `${code.slice(0, 2)}0000`
  originProvinceCode.value = originProvinces.some((item) => item.code === provinceCode) ? provinceCode : ''
  originCityCode.value = originCities.value.some((item) => item.code === code) ? code : ''
}

async function loadReferences() {
  const [categoryResult, storeResult] = await Promise.all([
    getCategories(), adminGet<{ items: AdminStore[]; next_cursor: string | null }>('/admin/stores?limit=20', token()),
  ])
  categories.value = categoryResult.data; store.value = storeResult.data.items[0] ?? null
  if (store.value) basic.store_id = store.value.store_id
}

async function load(options: { preserveDrafts?: boolean } = {}) {
  const draftSnapshot = options.preserveDrafts ? captureEditorDrafts() : null
  let loaded = false
  loading.value = true; error.value = ''
  if (!options.preserveDrafts) notice.value = ''
  try {
    await loadReferences()
    if (isNew.value) {
      const defaultCategory = flatCategories.value.find((item) => item.category_name.includes('其他')) ?? flatCategories.value.at(-1)
      if (!store.value || !defaultCategory) throw new Error('当前没有可用的店铺或商品基础归类，请联系平台管理员。')
      const created = (await adminCreate<AdminProduct>('/admin/products', { store_id: store.value.store_id, category_id: defaultCategory.category_id, brand_id: null, product_name: '未命名商品', subtitle: null, description: null }, token(), 'merchant-product-create')).data
      await router.replace(`/merchant/products/${created.product_id}`)
      return
    }
    const [detailResult, skuResult, imageResult, attributeResult, faqResult, inventoryResult, fulfillmentResult, reviewResult] = await Promise.all([
      adminGet<AdminProduct>(path(), token()),
      adminGet<AdminSku[]>(path('/skus'), token()),
      adminGet<AdminProductImage[]>(path('/images'), token()),
      adminGet<AdminProductAttribute[]>(path('/attributes'), token()),
      adminGet<AdminProductFaq[]>(path('/faqs'), token()),
      adminGet<{ items: AdminInventory[] }>(`/admin/inventories?product_id=${encodeURIComponent(productId.value)}&limit=100`, token()),
      adminGet<Fulfillment | null>(path('/fulfillment-profile'), token()),
      listAdminReviews(token(), 'published', productId.value),
    ])
    product.value = detailResult.data; skus.value = skuResult.data; images.value = imageResult.data; attributes.value = attributeResult.data; faqs.value = faqResult.data; inventories.value = inventoryResult.data.items; reviews.value = reviewResult.data.items
    faqDrafts.value = faqs.value.map((item) => ({ key: faqKey(), faq_id: item.faq_id, question: item.question, answer: item.current_answer_text ?? '' }))
    Object.assign(basic, { store_id: product.value.store_id, category_id: product.value.category_id, brand_id: product.value.brand_id ?? '', product_name: product.value.product_name })
    const firstSku = activeSkus.value[0]
    if (firstSku && !activeSkus.value.some((item) => item.sku_id === selectedSkuId.value)) selectedSkuId.value = firstSku.sku_id
    if (!firstSku) selectedSkuId.value = ''
    if (detailResult.data.current_detail_content_version_id) {
      const version = (await adminGet<AdminContentVersion>(path(`/detail-content-versions/${encodeURIComponent(detailResult.data.current_detail_content_version_id)}`), token())).data
      detailBlocks.value = parseDetailBlocks(version)
    } else detailBlocks.value = [blankDetailBlock('paragraph')]
    if (store.value) shippingTemplates.value = (await adminGet<AdminShippingTemplate[]>(`/admin/stores/${encodeURIComponent(store.value.store_id)}/shipping-templates`, token())).data
    if (fulfillmentResult.data) Object.assign(fulfillment, { ...fulfillmentResult.data, purchase_notice: fulfillmentResult.data.purchase_notice ?? '' })
    if (!fulfillment.shipping_template_id) fulfillment.shipping_template_id = shippingTemplates.value.find((item) => item.status === 'effective')?.template_id ?? ''
    restoreOriginSelection(fulfillment.origin_region_code)
    loaded = true
  } catch (cause) { error.value = errorMessage(cause) }
  finally {
    if (draftSnapshot && loaded) restoreEditorDrafts(draftSnapshot)
    loading.value = false
  }
}

async function perform(action: () => Promise<unknown>, success: string, reload = true) {
  saving.value = true; error.value = ''; notice.value = ''
  try { await action(); notice.value = success; if (reload) await load({ preserveDrafts: true }) }
  catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

function resetSku() { skuEditing.value = null; Object.assign(skuForm, { name: '', sale_price: '', stock: 0 }) }
function closeSkuEditor() { resetSku(); showSkuEditor.value = false }
async function startNewSku() { resetSku(); selectedSkuId.value = ''; showSkuEditor.value = true; await nextTick(); skuNameInput.value?.focus(); skuNameInput.value?.scrollIntoView?.({ behavior: 'smooth', block: 'center' }) }
function editSku(item: AdminSku) { skuEditing.value = item; showSkuEditor.value = true; selectedSkuId.value = item.sku_id; Object.assign(skuForm, { name: item.sku_name, sale_price: item.sale_price, stock: inventoryFor(item.sku_id)?.on_hand_quantity ?? 0 }) }
function beginSkuDelete(item: AdminSku) { deletingSku.value = item; skuDeleteError.value = '' }
function closeSkuDelete() { if (!skuDeleting.value) { deletingSku.value = null; skuDeleteError.value = '' } }
async function confirmSkuDelete() {
  if (!deletingSku.value || deletingLastActiveSku.value) return
  skuDeleting.value = true; skuDeleteError.value = ''; error.value = ''; notice.value = ''
  try {
    const targetSku = deletingSku.value
    const disabledSku = (await adminCommand<AdminSku>(path(`/skus/${encodeURIComponent(targetSku.sku_id)}/status-changes`), { action: 'disable', reason_code: 'MERCHANT_STYLE_REMOVE', reason: '商家从商品编辑器删除款式；保留历史交易与审计记录。' }, token(), targetSku.version, 'merchant-style-delete')).data
    skus.value = skus.value.map((item) => item.sku_id === disabledSku.sku_id ? disabledSku : item)
    const remainingSkus = activeSkus.value
    const remainingSkuIds = new Set(remainingSkus.map((item) => item.sku_id))
    const prices = remainingSkus.map((item) => Number(item.sale_price)).filter(Number.isFinite)
    if (product.value) {
      const missingRequirements = new Set(product.value.completeness.missing_requirements)
      if (remainingSkus.length) missingRequirements.delete('sku')
      else missingRequirements.add('sku')
      product.value = {
        ...product.value,
        version: product.value.version + 1,
        default_sku_id: product.value.default_sku_id === disabledSku.sku_id ? (remainingSkus[0]?.sku_id ?? null) : product.value.default_sku_id,
        min_price: prices.length ? Math.min(...prices).toFixed(2) : '0.00',
        max_price: prices.length ? Math.max(...prices).toFixed(2) : '0.00',
        sku_count: remainingSkus.length,
        available_quantity: inventories.value
          .filter((item) => remainingSkuIds.has(item.sku_id))
          .reduce((total, item) => total + item.available_quantity, 0),
        completeness: {
          ...product.value.completeness,
          sku: remainingSkus.length > 0,
          missing_requirements: [...missingRequirements],
        },
      }
    }
    selectedSkuId.value = remainingSkus[0]?.sku_id ?? ''
    selectedImage.value = 0
    closeSkuEditor(); deletingSku.value = null
    notice.value = '款式已删除，不再向顾客展示；历史订单记录仍会保留。'
  } catch (cause) { skuDeleteError.value = errorMessage(cause) }
  finally { skuDeleting.value = false }
}
async function saveSku() {
  const styleName = skuForm.name.trim()
  const stock = Number(skuForm.stock)
  if (!styleName) { error.value = '请填写款式名称。'; return }
  if (skuForm.sale_price === '' || Number(skuForm.sale_price) < 0) { error.value = '请填写不小于 0 元的价格。'; return }
  if (!Number.isInteger(stock) || stock < 0) { error.value = '库存必须是大于或等于 0 的整数。'; return }
  const priceAmount = minor(skuForm.sale_price)
  const editing = skuEditing.value
  const payload = { merchant_sku_code: editing?.merchant_sku_code ?? null, sku_name: styleName, spec_values: [{ name: '款式', value: styleName }], sale_price_amount: priceAmount, market_price_amount: priceAmount, weight_grams: editing?.weight_grams ?? null, barcode: editing?.barcode ?? null }
  saving.value = true; error.value = ''; notice.value = ''
  try {
    let targetSku: AdminSku
    let inventory: AdminInventory | undefined
    if (editing) {
      targetSku = (await adminUpdate<AdminSku>(path(`/skus/${encodeURIComponent(editing.sku_id)}`), payload, token(), editing.version)).data
      inventory = inventoryFor(editing.sku_id)
    } else {
      targetSku = (await adminCreate<AdminSku>(path('/skus'), { ...payload, currency: 'CNY' }, token(), 'merchant-sku-create')).data
      inventory = (await adminGet<{ items: AdminInventory[] }>(`/admin/inventories?product_id=${encodeURIComponent(productId.value)}&limit=100`, token())).data.items.find((item) => item.sku_id === targetSku.sku_id)
    }
    if (!inventory) throw new Error('款式已保存，但库存记录尚未准备好，请刷新后重新设置库存。')
    const delta = stock - inventory.on_hand_quantity
    if (delta) await adminCreate('/admin/inventory-adjustments', { sku_id: targetSku.sku_id, on_hand_delta: delta, reason_code: 'MERCHANT_DIRECT_EDIT', reason: '商家在商品款式中直接修改库存', reference_no: `merchant-ui-${Date.now()}`, expected_version: inventory.version }, token(), 'merchant-stock-adjust')
    selectedSkuId.value = targetSku.sku_id
    closeSkuEditor()
    await load({ preserveDrafts: true })
    notice.value = editing ? '款式、价格和库存已更新。' : '新款式、价格和库存已添加。'
  } catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

function imagePayload() {
  const positions = new Map<string, number>()
  return { items: images.value.map((item) => {
    if (!item.sku_id) throw new Error('检测到未绑定款式的旧图片，请刷新页面让系统完成迁移后重试。')
    const sortOrder = positions.get(item.sku_id) ?? 0
    positions.set(item.sku_id, sortOrder + 1)
    return { file_id: item.file_id, sku_id: item.sku_id, image_type: 'spec', alt_text: item.alt_text || basic.product_name, sort_order: sortOrder }
  }) }
}
async function persistImages(success: string) {
  if (!product.value) throw new Error('商品信息尚未加载完成。')
  const result = await adminReplace<AdminProductImage[]>(path('/images'), imagePayload(), token(), product.value.version)
  images.value = result.data
  product.value = (await adminGet<AdminProduct>(path(), token())).data
  selectedImage.value = Math.max(0, Math.min(selectedImage.value, activeImages.value.length - 1))
  pasteNotice.value = success
}
function addImage(fileId: string) {
  const skuId = activeSku.value?.sku_id
  if (!skuId) { pasteError.value = '请先新增并完成一个款式，再为该款式上传图片。'; return }
  images.value.push({ file_id: fileId, sku_id: skuId, image_type: 'spec', alt_text: basic.product_name, sort_order: activeImages.value.length, image_url: `/api/v1/files/${fileId}`, width: 0, height: 0, status: 'active' })
  selectedImage.value = activeImages.value.length - 1
  pasteError.value = ''; pasteNotice.value = '图片正在自动保存…'; imageSaving.value = true; imageSaveFailed.value = false
  imageSavePromise = persistImages('图片已上传并自动保存，刷新页面也不会丢失。')
    .catch((cause) => { imageSaveFailed.value = true; pasteError.value = `图片上传成功，但自动保存失败：${errorMessage(cause)}` })
    .finally(() => { imageSavePromise = null; imageSaving.value = false })
}
async function pasteImage(event: ClipboardEvent) {
  if (!canEdit.value || pasteBusy.value) return
  pasteError.value = ''
  pasteNotice.value = ''
  try {
    if (!activeSku.value) {
      pasteError.value = '请先新增并完成一个款式，再为该款式粘贴图片。'
      return
    }
    const file = imageFileFromClipboard(event.clipboardData)
    if (!file) {
      pasteError.value = '剪贴板中没有图片。请先复制图片本身，再点击左侧大图并按 Command + V 或 Ctrl + V。'
      return
    }
    event.preventDefault()
    if (!imageUpload.value) throw new Error('图片上传组件尚未准备好，请稍后重试。')
    pasteBusy.value = true
    await imageUpload.value.uploadFile(file)
    if (imageSavePromise) await imageSavePromise
  } catch (cause) {
    pasteError.value = cause instanceof Error ? cause.message : errorMessage(cause)
  } finally {
    pasteBusy.value = false
  }
}
async function removeImage(index: number) {
  const item = activeImages.value[index]
  if (!item) return
  const actualIndex = images.value.indexOf(item)
  if (actualIndex < 0) return
  const removed = images.value.splice(actualIndex, 1)[0]
  if (!removed) return
  pasteError.value = ''; pasteNotice.value = '正在移除并自动保存…'
  try { await persistImages('图片已移除并自动保存。') }
  catch (cause) { images.value.splice(actualIndex, 0, removed); pasteError.value = `图片移除失败：${errorMessage(cause)}` }
}

function addDetailImage(fileId: string) {
  detailBlocks.value.push({ ...blankDetailBlock('image'), file_id: fileId, alt: basic.product_name || '商品详情图片' })
  detailUploadError.value = ''
  detailUploadNotice.value = '图片已添加到商品详情末尾；点击页面底部“完成编辑”后统一生效。'
}
async function pasteDetailImage(event: ClipboardEvent) {
  if (!canEdit.value || detailPasteBusy.value || detailUploadBusy.value) return
  detailUploadError.value = ''; detailUploadNotice.value = ''
  try {
    const file = imageFileFromClipboard(event.clipboardData)
    if (!file) {
      detailUploadError.value = '剪贴板中没有图片。请先复制图片本身，再点击详情图片区并粘贴。'
      return
    }
    event.preventDefault()
    if (!detailImageUpload.value) throw new Error('详情图片上传组件尚未准备好，请稍后重试。')
    detailPasteBusy.value = true
    await detailImageUpload.value.uploadFile(file)
  } catch (cause) {
    detailUploadError.value = cause instanceof Error ? cause.message : errorMessage(cause)
  } finally { detailPasteBusy.value = false }
}

function addAttribute() { attributes.value.push({ attribute_code: `property_${attributes.value.length + 1}`, attribute_name: '', value_text: '', value_normalized: null, unit: null, is_searchable: false, sort_order: attributes.value.length }) }
function attributePayload() {
  return attributes.value.filter((item) => item.attribute_name.trim() && item.value_text.trim()).map((item, index) => ({ ...item, attribute_code: item.attribute_code.trim().toLowerCase().replace(/[^a-z0-9_]/g, '_'), value_normalized: item.value_normalized || null, unit: item.unit || null, sort_order: index }))
}
function serializedDetailBlocks() {
  return detailBlocks.value.flatMap((block): Array<Record<string, unknown>> => {
    if (block.type === 'paragraph' && block.text.trim()) return [{ type: 'paragraph', text: block.text.trim() }]
    if (block.type === 'heading' && block.text.trim()) return [{ type: 'heading', text: block.text.trim(), level: block.level }]
    if (block.type === 'bullet_list') {
      const items = block.items.map((item) => item.trim()).filter(Boolean)
      return items.length ? [{ type: 'bullet_list', items }] : []
    }
    if (block.type === 'image' && block.file_id) return [{ type: 'image', file_id: block.file_id, alt: block.alt.trim() || basic.product_name || '商品详情图片' }]
    return []
  })
}
function faqPayload() {
  return faqDrafts.value.map((item, index) => ({ faq_id: item.faq_id, question: item.question.trim(), answer: item.answer.trim(), sort_order: index }))
}
async function ensureEffectiveShippingTemplate(): Promise<string> {
  const existing = shippingTemplates.value.find((item) => item.status === 'effective')
  if (existing) return existing.template_id
  if (!store.value) throw new Error('当前店铺信息尚未加载完成。')
  const storePath = `/admin/stores/${encodeURIComponent(store.value.store_id)}/shipping-templates`
  const created = (await adminCreate<AdminShippingTemplate>(storePath, {
    template_family_id: null,
    template_name: '系统默认配送',
    delivery_type: 'express',
    charge_mode: 'fixed',
    currency: 'CNY',
    dispatch_min_hours: fulfillment.dispatch_min_hours,
    dispatch_max_hours: fulfillment.dispatch_max_hours,
    rules: [{ region_scope: { include: [], exclude: [] }, first_unit: 1, additional_unit: 1, first_fee_amount: 0, additional_fee_amount: 0, estimated_min_days: 1, estimated_max_days: 7 }],
  }, token(), 'merchant-default-shipping-create')).data
  const published = (await adminCommand<AdminShippingTemplate>(`${storePath}/${encodeURIComponent(created.template_id)}/publications`, { reason: '商家首次保存商品发货设置，启用系统默认配送' }, token(), created.version, 'merchant-default-shipping-publish')).data
  shippingTemplates.value.push(published)
  return published.template_id
}
async function replyReview(item: AdminReview) {
  const text = replyDrafts[item.review_id]?.trim() ?? ''
  if (text.length < 2) { error.value = '评价回复至少需要填写 2 个字符。'; return }
  replyingReviewId.value = item.review_id; error.value = ''
  try { await replyAdminReview(item.review_id, `"v${item.version}"`, text, token()); replyDrafts[item.review_id] = ''; await load({ preserveDrafts: true }) }
  catch (cause) { error.value = errorMessage(cause) }
  finally { replyingReviewId.value = '' }
}
async function refreshProduct() {
  product.value = (await adminGet<AdminProduct>(path(), token())).data
}
async function finishEditing(label: string, requireComplete = true) {
  if (!product.value || !basic.product_name.trim()) { error.value = '请填写商品名称。'; return }
  const blocks = serializedDetailBlocks()
  const faqItems = faqPayload()
  if (faqItems.some((item) => !item.question || !item.answer)) { error.value = '每一组常见问题都必须同时填写问题和答案，或直接删除空白项。'; return }
  if (requireComplete && !activeSkus.value.length) { error.value = '请先新增并完成至少一个款式。'; return }
  const skuWithoutImage = activeSkus.value.find((sku) => !images.value.some((image) => image.sku_id === sku.sku_id))
  if (requireComplete && skuWithoutImage) { error.value = `请先为款式“${skuWithoutImage.sku_name}”上传至少一张图片。`; return }
  if (requireComplete && !originProvinceCode.value) { error.value = '请选择发货地。'; return }
  if (requireComplete && !blocks.length) { error.value = '商品详情至少需要一段文字或一张图片。'; return }
  saving.value = true; error.value = ''; notice.value = ''
  try {
    if (imageSavePromise) await imageSavePromise
    if (imageSaveFailed.value) throw new Error('商品图片尚未保存成功，请根据图片区提示处理后再完成编辑。')
    await adminUpdate(path(), { product_name: basic.product_name, subtitle: null, description: null }, token(), product.value.version)
    await refreshProduct()
    await adminReplace(path('/attributes'), { items: attributePayload() }, token(), product.value.version)
    await refreshProduct()
    if (blocks.length) {
      await adminCreate(path('/detail-content-versions'), { source_format: 'structured', source_content: JSON.stringify(blocks) }, token(), `merchant-detail-finish-${Date.now()}`)
      await refreshProduct()
    }
    if (originProvinceCode.value) {
      fulfillment.shipping_template_id = await ensureEffectiveShippingTemplate()
      fulfillment.origin_region_code = originCityCode.value || originProvinceCode.value
      await adminReplace(path('/fulfillment-profile'), { shipping_template_id: fulfillment.shipping_template_id, origin_region_code: fulfillment.origin_region_code, dispatch_min_hours: fulfillment.dispatch_min_hours, dispatch_max_hours: fulfillment.dispatch_max_hours, purchase_notice: fulfillment.purchase_notice || null }, token(), product.value.version)
      await refreshProduct()
    }
    await adminReplace(path('/faqs'), { items: faqItems }, token(), product.value.version)
    notice.value = label
    await router.push('/merchant/products')
  } catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}
async function productCommand(action: 'submit_review' | 'publish' | 'off_shelf') {
  const endpoint = { submit_review: '/review-submissions', publish: '/publications', off_shelf: '/off-shelf-commands' }[action]
  const reason = { submit_review: '商家完成商品资料并提交审核', publish: '商家确认商品开始销售', off_shelf: '商家主动停止商品销售' }[action]
  await perform(() => adminCommand(path(endpoint), { reason_code: 'MERCHANT_OPERATION', reason }, token(), product.value!.version, `merchant-product-${action}`), action === 'submit_review' ? '商品已提交审核。' : action === 'publish' ? '商品已上架。' : '商品已下架。')
}

watch(() => route.params.productId, () => void load())
watch(selectedSkuId, () => { selectedImage.value = 0 })
onMounted(() => { resetSku(); void load() })
</script>

<template>
  <section class="merchant-page-stack merchant-product-editor">
    <header class="merchant-editor-header"><RouterLink to="/merchant/products">← 返回我的商品</RouterLink><div v-if="product" class="merchant-editor-status"><span :class="`status-${product.status}`">{{ statusLabel(product.status) }}</span><small v-if="!canEdit">当前状态下资料只读；下架或审核退回后可继续编辑。</small></div></header>
    <p v-if="notice" class="alert success" aria-live="polite">{{ notice }}</p>
    <p v-if="error && product" class="alert error" role="alert">{{ error }}</p>
    <PageState :loading="loading" :error="product ? '' : error" :empty="!loading && !product" empty-title="商品不存在" @retry="load">
      <template v-if="product">
        <div class="merchant-live-editor">
          <section class="merchant-gallery-editor">
            <div ref="mainImageArea" class="merchant-main-image merchant-paste-image-zone" :class="{ focused: pasteFocused, busy: pasteBusy, disabled: !activeSku }" :tabindex="canEdit && activeSku ? 0 : -1" :role="canEdit && activeSku ? 'button' : undefined" :aria-label="canEdit && activeSku ? `为款式${activeSku.sku_name}粘贴上传图片` : undefined" @click="activeSku && mainImageArea?.focus()" @focus="pasteFocused = true" @blur="pasteFocused = false" @paste="pasteImage"><img v-if="displayImage" :src="resolveApiAssetUrl(displayImage.image_url) || undefined" :alt="displayImage.alt_text || basic.product_name" /><div v-else-if="activeSku"><b>为“{{ activeSku.sku_name }}”添加图片</b><p>这个区域只展示并保存当前选中款式的图片。</p></div><div v-else><b>请先新增并完成一个款式</b><p>创建款式后，才能为该款式上传或粘贴对应图片。</p></div><span v-if="canEdit && activeSku" class="merchant-paste-image-hint" aria-live="polite">{{ pasteBusy ? '正在读取、扫描并上传剪贴板图片…' : '点击大图后，按 Command + V（macOS）或 Ctrl + V（Windows）粘贴图片' }}</span></div>
            <p v-if="pasteNotice" class="success-text merchant-paste-feedback" role="status">{{ pasteNotice }}</p><p v-if="pasteError" class="error-text merchant-paste-feedback" role="alert">{{ pasteError }}</p>
            <div v-if="activeImages.length" class="merchant-thumbnails"><button v-for="(item, index) in activeImages" :key="`${item.file_id}-${index}`" type="button" :class="{ active: selectedImage === index }" @click="selectedImage = index"><img :src="resolveApiAssetUrl(item.image_url) || undefined" alt="" /></button></div>
            <div v-if="canEdit" class="merchant-image-actions"><template v-if="activeSku"><strong>当前款式：{{ activeSku.sku_name }}</strong><AdminFileUpload ref="imageUpload" purpose="product" :business-context-id="product.store_id" label="从本地选择该款式图片" :disabled="imageSaving || saving" @uploaded="addImage" @busy-changed="uploadBusy = $event" /><small>上传或粘贴成功后会自动保存到当前款式；图片处理完成前会锁定当前款式，避免绑定到其他款式。</small><div class="actions"><button v-if="displayImage" type="button" class="danger small" :disabled="editorBusy" @click="removeImage(selectedImage)">移除当前图片</button></div></template><p v-else class="muted">完成右侧“新增一个款式”后，这里会开放图片上传。</p></div>
          </section>

          <section class="merchant-product-info-editor"><p class="eyebrow">顾客看到的商品信息 · 可直接编辑</p><label class="merchant-title-input">商品名称<input v-model.trim="basic.product_name" required maxlength="255" :disabled="!canEdit" /></label>
            <div class="merchant-style-picker"><header><div><strong class="merchant-style-title">款式与价格</strong><small>每个款式直接设置名称、价格和库存；顾客购买后库存会自动减少</small></div><button v-if="canEdit" type="button" class="secondary small" :disabled="editorBusy" @click="startNewSku">＋ 新增款式</button></header><div><button v-for="sku in activeSkus" :key="sku.sku_id" type="button" :class="{ active: activeSku?.sku_id === sku.sku_id }" :disabled="editorBusy" @click="selectedSkuId = sku.sku_id; editSku(sku)"><strong>{{ sku.sku_name }}</strong><b>¥{{ sku.sale_price }}</b><small>库存 {{ inventoryFor(sku.sku_id)?.on_hand_quantity ?? 0 }} · 可售 {{ inventoryFor(sku.sku_id)?.available_quantity ?? 0 }}</small></button></div><p v-if="!activeSkus.length">还没有款式，点击“新增款式”后直接填写名称、价格和库存。</p></div>
            <form v-if="canEdit && showSkuEditor" class="merchant-inline-sku-form" :class="{ active: !skuEditing }" @submit.prevent="saveSku"><header><strong>{{ skuEditing ? `正在编辑：${skuEditing.sku_name}` : '新增一个款式' }}</strong><div class="actions"><button v-if="skuEditing" type="button" class="danger small" :disabled="editorBusy" @click="beginSkuDelete(skuEditing)">删除款式</button><button type="button" class="secondary small" @click="closeSkuEditor">取消</button></div></header><div class="merchant-simple-sku-fields"><label>款式名称<input ref="skuNameInput" v-model.trim="skuForm.name" required maxlength="255" placeholder="例如：曜石黑 / 42 码" /></label><label>价格（元）<input v-model="skuForm.sale_price" type="number" min="0" step="0.01" required /></label><label>库存<input v-model.number="skuForm.stock" type="number" min="0" step="1" required /></label></div><small v-if="skuEditing && (inventoryFor(skuEditing.sku_id)?.reserved_quantity ?? 0) > 0">其中 {{ inventoryFor(skuEditing.sku_id)?.reserved_quantity }} 件已被订单预占；修改账面库存不会取消已有订单。</small><button :disabled="editorBusy">完成</button></form>
          </section>
        </div>

        <section class="merchant-detail-editors">
          <section class="card"><header><div><p class="eyebrow">商品参数</p><h2>顾客对比商品时会看这里</h2></div><button v-if="canEdit" type="button" class="secondary small" @click="addAttribute">＋ 新增参数</button></header><div class="merchant-attribute-rows"><div v-for="(item, index) in attributes" :key="index"><input v-model.trim="item.attribute_name" placeholder="参数名" :disabled="!canEdit" /><input v-model.trim="item.value_text" placeholder="参数值" :disabled="!canEdit" /><button v-if="canEdit" type="button" class="danger small" @click="attributes.splice(index, 1)">删除</button></div></div></section>
          <section class="card"><header><div><p class="eyebrow">发货与购买须知</p><h2>让顾客在下单前了解</h2></div></header><fieldset class="merchant-origin-field"><legend>发货地</legend><div class="field-grid"><label>省份<select v-model="originProvinceCode" required :disabled="!canEdit" @change="selectOriginProvince"><option value="" disabled>请选择省份</option><option v-for="item in originProvinces" :key="item.code" :value="item.code">{{ item.name }}</option></select></label><label>城市<select v-model="originCityCode" :disabled="!canEdit || !originProvinceCode" @change="selectOriginCity"><option value="">全省</option><option v-for="item in originCities" :key="item.code" :value="item.code">{{ item.name }}</option></select></label></div></fieldset><div class="field-grid"><label>最早发货（小时）<input v-model.number="fulfillment.dispatch_min_hours" type="number" min="0" :disabled="!canEdit" /></label><label>最晚发货（小时）<input v-model.number="fulfillment.dispatch_max_hours" type="number" min="0" :disabled="!canEdit" /></label></div><label>购买须知<textarea v-model.trim="fulfillment.purchase_notice" rows="4" maxlength="3000" :disabled="!canEdit" /></label></section>
        </section>

        <section class="merchant-content-editor card"><header><div><p class="eyebrow">商品详情</p><h2>按顾客从上到下看到的顺序编辑</h2><p>文字和图片会严格按照这里的排列顺序展示；可上传图片，也可点击粘贴区后按 Command + V 或 Ctrl + V。</p></div><button v-if="canEdit" type="button" class="secondary" :disabled="editorBusy" @click="addDetailText">＋ 添加文字</button></header><div class="merchant-detail-block-list"><article v-for="(block, index) in detailBlocks" :key="block.key" class="merchant-detail-block" :class="`is-${block.type}`"><header><strong>{{ index + 1 }} · {{ detailBlockLabel(block) }}</strong><div v-if="canEdit" class="actions"><button type="button" class="secondary small" :disabled="index === 0 || editorBusy" @click="moveDetailBlock(index, -1)">上移</button><button type="button" class="secondary small" :disabled="index === detailBlocks.length - 1 || editorBusy" @click="moveDetailBlock(index, 1)">下移</button><button type="button" class="danger small" :disabled="editorBusy" @click="removeDetailBlock(index)">删除</button></div></header><textarea v-if="block.type === 'paragraph'" v-model="block.text" rows="5" maxlength="100000" :disabled="!canEdit" placeholder="输入这一段商品介绍……" /><template v-else-if="block.type === 'heading'"><input v-model.trim="block.text" maxlength="255" :disabled="!canEdit" /><select v-model="block.level" :disabled="!canEdit"><option :value="2">大标题</option><option :value="3">小标题</option></select></template><textarea v-else-if="block.type === 'bullet_list'" :value="block.items.join('\n')" rows="5" :disabled="!canEdit" @input="block.items = ($event.target as HTMLTextAreaElement).value.split('\n')" /><figure v-else-if="block.type === 'image'"><img :src="detailImageUrl(block.file_id) || undefined" :alt="block.alt" /><label>图片说明<input v-model.trim="block.alt" maxlength="255" :disabled="!canEdit" placeholder="例如：面料纹理细节" /></label></figure></article><p v-if="!detailBlocks.length" class="merchant-detail-empty">还没有详情内容。请先添加文字，或在下方上传、粘贴图片。</p></div><div v-if="canEdit" ref="detailPasteArea" class="merchant-detail-image-insert" :class="{ focused: detailPasteFocused, busy: detailPasteBusy || detailUploadBusy }" tabindex="0" role="button" aria-label="商品详情图片粘贴上传区" @click="detailPasteArea?.focus()" @focus="detailPasteFocused = true" @blur="detailPasteFocused = false" @paste="pasteDetailImage"><strong>{{ detailPasteBusy || detailUploadBusy ? '正在读取、扫描并上传详情图片…' : '上传或粘贴一张详情图片' }}</strong><p>新图片会添加到当前详情的最下方，之后可使用“上移 / 下移”调整位置。</p><AdminFileUpload ref="detailImageUpload" purpose="product" :business-context-id="product.store_id" label="从本地选择详情图片" @uploaded="addDetailImage" @busy-changed="detailUploadBusy = $event" /></div><p v-if="detailUploadNotice" class="success-text" role="status">{{ detailUploadNotice }}</p><p v-if="detailUploadError" class="error-text" role="alert">{{ detailUploadError }}</p></section>

        <section class="merchant-faq-editor card"><header><div><p class="eyebrow">常见问题</p><h2>提前回答顾客最常问的问题</h2><p>新增、修改或删除后，点击页面底部“完成编辑”统一更新到商品。</p></div><button v-if="canEdit" type="button" class="secondary" @click="addFaq">＋ 新增</button></header><div class="merchant-faq-draft-list"><article v-for="(item, index) in faqDrafts" :key="item.key"><header><strong>问题 {{ index + 1 }}</strong><button v-if="canEdit" type="button" class="danger small" @click="faqDrafts.splice(index, 1)">删除这组问答</button></header><label>问题<input v-model="item.question" maxlength="1000" :disabled="!canEdit" placeholder="例如：尺码偏大还是偏小？" /></label><label>回答<textarea v-model="item.answer" rows="5" maxlength="100000" :disabled="!canEdit" placeholder="请输入给顾客看的答案" /></label></article><p v-if="!faqDrafts.length" class="muted">暂时没有常见问题，需要时点击“新增”。</p></div></section>

        <section class="merchant-product-reviews card"><header><div><p class="eyebrow">商品评价</p><h2>顾客在这件商品下看到的评价</h2><p>平均 {{ product.rating_score }} 分 · 共 {{ product.review_count }} 条</p></div></header><p v-if="!reviews.length" class="merchant-review-empty">这件商品暂时还没有顾客评价。</p><article v-for="item in reviews" :key="item.review_id"><header><span class="merchant-stars">{{ '★'.repeat(item.rating) }}{{ '☆'.repeat(5 - item.rating) }}</span><strong>{{ item.is_anonymous ? '匿名顾客' : item.user_name }}</strong><time>{{ new Date(item.submitted_at).toLocaleString('zh-CN') }}</time></header><p>{{ item.content || '顾客只留下了星级评分。' }}</p><small>购买款式：{{ item.sku_name }}</small><div v-if="item.merchant_reply" class="merchant-existing-reply"><strong>店铺回复</strong><p>{{ item.merchant_reply.content }}</p></div><form v-else @submit.prevent="replyReview(item)"><label>回复这条评价<textarea v-model="replyDrafts[item.review_id]" rows="3" minlength="2" maxlength="500" placeholder="感谢您的反馈……" /></label><div class="actions"><button type="button" class="secondary small" @click="replyDrafts[item.review_id] = '感谢您的支持与认可，我们会继续认真做好商品和服务。'">感谢好评</button><button :disabled="replyingReviewId === item.review_id || (replyDrafts[item.review_id]?.trim().length ?? 0) < 2">{{ replyingReviewId === item.review_id ? '正在回复…' : '发布回复' }}</button></div></form></article></section>

        <footer class="merchant-publication-bar"><div><strong>{{ statusLabel(product.status) }}</strong><span v-if="product.completeness.missing_requirements.length">还可以继续补充：{{ product.completeness.missing_requirements.join('、') }}</span><span v-else>商品资料已完整</span><small>商品名称、参数、发货设置、详情和常见问题都由这里统一保存；款式需先完成后才能上传对应图片。</small></div><div class="actions"><button v-if="product.status === 'draft'" type="button" class="secondary" :disabled="editorBusy" @click="finishEditing('商品已暂存为草稿。', false)">暂存为草稿</button><button v-if="canEdit" type="button" :disabled="editorBusy" @click="finishEditing('商品编辑已完成。')">完成编辑</button><RouterLink v-if="product.status === 'on_sale'" :to="`/products/${product.product_id}`" target="_blank">查看顾客页面 ↗</RouterLink><button v-if="product.available_actions.includes('submit_review')" :disabled="editorBusy" @click="productCommand('submit_review')">提交平台审核</button><button v-if="product.available_actions.includes('publish')" :disabled="editorBusy" @click="productCommand('publish')">立即上架</button><button v-if="product.available_actions.includes('off_shelf')" class="danger" :disabled="editorBusy" @click="productCommand('off_shelf')">下架商品</button></div></footer>
      </template>
    </PageState>
    <Teleport to="body"><div v-if="deletingSku" class="merchant-delete-overlay" @mousedown.self="closeSkuDelete"><section class="merchant-delete-dialog" role="alertdialog" aria-modal="true" aria-labelledby="merchant-sku-delete-title"><span>!</span><template v-if="deletingLastActiveSku"><h2 id="merchant-sku-delete-title">当前不能删除最后一个在售款式</h2><p>在售商品必须至少保留一个顾客可以购买的款式。请先新增另一个款式，或者先将整个商品下架。</p><div class="actions"><button type="button" class="secondary" @click="closeSkuDelete">知道了</button></div></template><template v-else><h2 id="merchant-sku-delete-title">删除“{{ deletingSku.sku_name }}”款式？</h2><p>删除后，该款式会立即从商家编辑区和顾客购买选项中消失。为了保证订单、评价、库存流水和审计记录完整，历史数据仍会安全保留。</p><p v-if="(inventoryFor(deletingSku.sku_id)?.sold_quantity ?? 0) > 0">这个款式已有 {{ inventoryFor(deletingSku.sku_id)?.sold_quantity }} 件销量，历史订单中的款式名称不会受影响。</p><p v-if="skuDeleteError" class="error-text" role="alert">{{ skuDeleteError }}</p><div class="actions"><button type="button" class="secondary" :disabled="skuDeleting" @click="closeSkuDelete">取消</button><button type="button" class="danger" :disabled="skuDeleting" @click="confirmSkuDelete">{{ skuDeleting ? '正在删除…' : '删除款式' }}</button></div></template></section></div></Teleport>
  </section>
</template>
