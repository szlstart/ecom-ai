<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import {
  adminCommand,
  adminCreate,
  adminGet,
  adminReplace,
  adminUpdate,
  requireAdminToken,
  type AdminBrand,
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
import { getBrands, getCategories, type Brand, type Category } from '@/api/catalog'
import { errorMessage, resolveApiAssetUrl } from '@/api/http'
import AdminFileUpload from '@/components/AdminFileUpload.vue'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

interface Fulfillment { shipping_template_id: string; origin_region_code: string; dispatch_min_hours: number; dispatch_max_hours: number; purchase_notice: string | null; profile_version: number; version: number }
interface SpecRow { name: string; value: string }

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
const categories = ref<Category[]>([])
const brands = ref<Array<Brand | AdminBrand>>([])
const shippingTemplates = ref<AdminShippingTemplate[]>([])
const loading = ref(true); const saving = ref(false); const error = ref(''); const notice = ref('')
const selectedImage = ref(0); const selectedSkuId = ref(''); const skuEditing = ref<AdminSku | null>(null)
const newStock = ref<number | null>(null)
const basic = reactive({ store_id: '', category_id: '', brand_id: '', product_name: '', subtitle: '', description: '' })
const skuForm = reactive({ code: '', name: '', sale_price: '', market_price: '', weight_grams: '', barcode: '' })
const specs = ref<SpecRow[]>([{ name: '款式', value: '标准款' }])
const content = ref('')
const faqForm = reactive({ question: '', answer: '' })
const faqEditing = ref<AdminProductFaq | null>(null)
const fulfillment = reactive({ shipping_template_id: '', origin_region_code: 'CN', dispatch_min_hours: 24, dispatch_max_hours: 48, purchase_notice: '' })
const flatCategories = computed(() => flatten(categories.value))
const activeSku = computed(() => skus.value.find((item) => item.sku_id === selectedSkuId.value) ?? skus.value[0] ?? null)
const activeInventory = computed(() => inventories.value.find((item) => item.sku_id === activeSku.value?.sku_id) ?? null)
const activeImages = computed(() => {
  const skuId = activeSku.value?.sku_id
  const matching = images.value.filter((item) => item.sku_id === skuId)
  return matching.length ? matching : images.value.filter((item) => item.sku_id === null)
})
const displayImage = computed(() => activeImages.value[selectedImage.value] ?? activeImages.value[0] ?? null)
const canEdit = computed(() => !product.value || ['draft', 'rejected', 'off_shelf'].includes(product.value.status))

function token() { return requireAdminToken(auth.accessToken) }
function path(suffix = '') { return `/admin/products/${encodeURIComponent(productId.value)}${suffix}` }
function flatten(nodes: Category[]): Category[] { return nodes.flatMap((item) => [item, ...flatten(item.children)]) }
function minor(value: string) { return Math.round(Number(value || 0) * 100) }
function statusLabel(value?: string) { return ({ draft: '草稿', pending_review: '审核中', approved: '审核通过，待上架', rejected: '审核退回', on_sale: '销售中', off_shelf: '已下架' } as Record<string, string>)[value || ''] ?? value }
function inventoryFor(skuId: string) { return inventories.value.find((item) => item.sku_id === skuId) }

async function loadReferences() {
  const [categoryResult, brandResult, storeResult] = await Promise.all([
    getCategories(), getBrands(), adminGet<{ items: AdminStore[]; next_cursor: string | null }>('/admin/stores?limit=20', token()),
  ])
  categories.value = categoryResult.data; brands.value = brandResult.data; store.value = storeResult.data.items[0] ?? null
  if (store.value) basic.store_id = store.value.store_id
}

async function load() {
  loading.value = true; error.value = ''; notice.value = ''
  try {
    await loadReferences()
    if (isNew.value) { product.value = null; return }
    const [detailResult, skuResult, imageResult, attributeResult, faqResult, inventoryResult, fulfillmentResult] = await Promise.all([
      adminGet<AdminProduct>(path(), token()),
      adminGet<AdminSku[]>(path('/skus'), token()),
      adminGet<AdminProductImage[]>(path('/images'), token()),
      adminGet<AdminProductAttribute[]>(path('/attributes'), token()),
      adminGet<AdminProductFaq[]>(path('/faqs'), token()),
      adminGet<{ items: AdminInventory[] }>(`/admin/inventories?product_id=${encodeURIComponent(productId.value)}&limit=100`, token()),
      adminGet<Fulfillment | null>(path('/fulfillment-profile'), token()),
    ])
    product.value = detailResult.data; skus.value = skuResult.data; images.value = imageResult.data; attributes.value = attributeResult.data; faqs.value = faqResult.data; inventories.value = inventoryResult.data.items
    Object.assign(basic, { store_id: product.value.store_id, category_id: product.value.category_id, brand_id: product.value.brand_id ?? '', product_name: product.value.product_name, subtitle: product.value.subtitle ?? '', description: product.value.description ?? '' })
    const firstSku = skus.value[0]
    if (firstSku && !skus.value.some((item) => item.sku_id === selectedSkuId.value)) selectedSkuId.value = firstSku.sku_id
    if (detailResult.data.current_detail_content_version_id) {
      content.value = (await adminGet<AdminContentVersion>(path(`/detail-content-versions/${encodeURIComponent(detailResult.data.current_detail_content_version_id)}`), token())).data.source_content
    }
    if (fulfillmentResult.data) Object.assign(fulfillment, { ...fulfillmentResult.data, purchase_notice: fulfillmentResult.data.purchase_notice ?? '' })
    if (store.value) shippingTemplates.value = (await adminGet<AdminShippingTemplate[]>(`/admin/stores/${encodeURIComponent(store.value.store_id)}/shipping-templates`, token())).data
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function perform(action: () => Promise<unknown>, success: string, reload = true) {
  saving.value = true; error.value = ''; notice.value = ''
  try { await action(); notice.value = success; if (reload) await load() }
  catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

async function saveBasic() {
  if (!basic.product_name.trim() || !basic.category_id) { error.value = '请先填写商品名称并选择商品分类。'; return }
  if (isNew.value) {
    saving.value = true; error.value = ''
    try {
      const created = (await adminCreate<AdminProduct>('/admin/products', { store_id: basic.store_id, category_id: basic.category_id, brand_id: basic.brand_id || null, product_name: basic.product_name, subtitle: basic.subtitle || null, description: basic.description || null }, token(), 'merchant-product-create')).data
      await router.replace(`/merchant/products/${created.product_id}`)
    } catch (cause) { error.value = errorMessage(cause) }
    finally { saving.value = false }
    return
  }
  await perform(() => adminUpdate(path(), { category_id: basic.category_id, brand_id: basic.brand_id || null, product_name: basic.product_name, subtitle: basic.subtitle || null, description: basic.description || null }, token(), product.value!.version), '商品信息已保存。')
}

function resetSku() { skuEditing.value = null; Object.assign(skuForm, { code: '', name: '', sale_price: '', market_price: '', weight_grams: '', barcode: '' }); specs.value = [{ name: '款式', value: '标准款' }]; newStock.value = null }
function editSku(item: AdminSku) { skuEditing.value = item; selectedSkuId.value = item.sku_id; Object.assign(skuForm, { code: item.merchant_sku_code ?? '', name: item.sku_name, sale_price: item.sale_price, market_price: item.market_price, weight_grams: item.weight_grams == null ? '' : String(item.weight_grams), barcode: item.barcode ?? '' }); specs.value = item.spec_values.map((spec) => ({ ...spec })); newStock.value = inventoryFor(item.sku_id)?.on_hand_quantity ?? 0 }
function addSpec() { specs.value.push({ name: '', value: '' }) }
function removeSpec(index: number) { if (specs.value.length > 1) specs.value.splice(index, 1) }
async function saveSku() {
  const specValues = specs.value.map((item) => ({ name: item.name.trim(), value: item.value.trim() })).filter((item) => item.name && item.value)
  if (!skuForm.name.trim() || !specValues.length) { error.value = '请填写款式名称，并至少保留一组完整的规格名称和规格值。'; return }
  if (Number(skuForm.market_price) < Number(skuForm.sale_price)) { error.value = '市场价不能低于销售价。'; return }
  const payload = { merchant_sku_code: skuForm.code || null, sku_name: skuForm.name, spec_values: specValues, sale_price_amount: minor(skuForm.sale_price), market_price_amount: minor(skuForm.market_price), weight_grams: skuForm.weight_grams === '' ? null : Number(skuForm.weight_grams), barcode: skuForm.barcode || null }
  await perform(async () => {
    if (skuEditing.value) await adminUpdate(path(`/skus/${encodeURIComponent(skuEditing.value.sku_id)}`), payload, token(), skuEditing.value.version)
    else await adminCreate(path('/skus'), { ...payload, currency: 'CNY' }, token(), 'merchant-sku-create')
    resetSku()
  }, skuEditing.value ? '款式已更新。' : '新款式已添加。')
}

async function saveStock() {
  if (!activeInventory.value || newStock.value === null) return
  const delta = newStock.value - activeInventory.value.on_hand_quantity
  if (!delta) { notice.value = '库存数量没有变化。'; return }
  await perform(() => adminCreate('/admin/inventory-adjustments', { sku_id: activeInventory.value!.sku_id, on_hand_delta: delta, reason_code: 'MERCHANT_DIRECT_EDIT', reason: '商家在商品详情模板中直接修改库存', reference_no: `merchant-ui-${Date.now()}`, expected_version: activeInventory.value!.version }, token(), 'merchant-stock-adjust'), '库存已更新。')
}

function addImage(fileId: string) { images.value.push({ file_id: fileId, sku_id: selectedSkuId.value || null, image_type: images.value.length ? 'gallery' : 'main', alt_text: basic.product_name, sort_order: images.value.length, image_url: `/api/v1/files/${fileId}`, width: 0, height: 0, status: 'active' }); selectedImage.value = activeImages.value.length - 1 }
function removeImage(index: number) { const item = activeImages.value[index]; if (!item) return; const actualIndex = images.value.indexOf(item); if (actualIndex >= 0) images.value.splice(actualIndex, 1) }
async function saveImages() {
  if (!images.value.length) { error.value = '请至少保留一张商品图片。'; return }
  await perform(() => adminReplace(path('/images'), { items: images.value.map((item, index) => ({ file_id: item.file_id, sku_id: item.sku_id || null, image_type: item.image_type, alt_text: item.alt_text || basic.product_name, sort_order: index })) }, token(), product.value!.version), '商品图片已保存。')
}

function addAttribute() { attributes.value.push({ attribute_code: `property_${attributes.value.length + 1}`, attribute_name: '', value_text: '', value_normalized: null, unit: null, is_searchable: false, sort_order: attributes.value.length }) }
async function saveAttributes() {
  const items = attributes.value.filter((item) => item.attribute_name.trim() && item.value_text.trim()).map((item, index) => ({ ...item, attribute_code: item.attribute_code.trim().toLowerCase().replace(/[^a-z0-9_]/g, '_'), value_normalized: item.value_normalized || null, unit: item.unit || null, sort_order: index }))
  await perform(() => adminReplace(path('/attributes'), { items }, token(), product.value!.version), '商品参数已保存。')
}
async function saveContent() { if (!content.value.trim()) { error.value = '商品详情不能为空。'; return }; await perform(() => adminCreate(path('/detail-content-versions'), { source_format: 'plain_text', source_content: content.value }, token(), 'merchant-detail-create'), '商品详情新版本已保存。') }
function editFaq(item: AdminProductFaq) { faqEditing.value = item; faqForm.question = item.question; faqForm.answer = item.current_answer_text ?? '' }
function resetFaq() { faqEditing.value = null; faqForm.question = ''; faqForm.answer = '' }
async function saveFaq() {
  if (!faqForm.question.trim() || !faqForm.answer.trim()) { error.value = '问题和答案都需要填写。'; return }
  await perform(async () => {
    let faq: AdminProductFaq
    if (faqEditing.value) faq = (await adminCreate<AdminProductFaq>(path(`/faqs/${encodeURIComponent(faqEditing.value.faq_id)}/versions`), { source_format: 'plain_text', source_content: faqForm.answer }, token(), 'merchant-faq-version')).data
    else faq = (await adminCreate<AdminProductFaq>(path('/faqs'), { question: faqForm.question, sort_order: faqs.value.length, source_format: 'plain_text', source_content: faqForm.answer }, token(), 'merchant-faq-create')).data
    if (faq.current_version_id) await adminCreate(path(`/faqs/${encodeURIComponent(faq.faq_id)}/publications`), { version_id: faq.current_version_id, reason: '商家更新商品常见问题' }, token(), 'merchant-faq-publish')
    resetFaq()
  }, faqEditing.value ? '常见问题已更新并发布。' : '常见问题已新增并发布。')
}
async function saveFulfillment() { if (!fulfillment.shipping_template_id) { error.value = '请先选择运费模板。'; return }; await perform(() => adminReplace(path('/fulfillment-profile'), { shipping_template_id: fulfillment.shipping_template_id, origin_region_code: fulfillment.origin_region_code, dispatch_min_hours: fulfillment.dispatch_min_hours, dispatch_max_hours: fulfillment.dispatch_max_hours, purchase_notice: fulfillment.purchase_notice || null }, token(), product.value!.version), '发货设置已保存。') }
async function productCommand(action: 'submit_review' | 'publish' | 'off_shelf') {
  const endpoint = { submit_review: '/review-submissions', publish: '/publications', off_shelf: '/off-shelf-commands' }[action]
  const reason = { submit_review: '商家完成商品资料并提交审核', publish: '商家确认商品开始销售', off_shelf: '商家主动停止商品销售' }[action]
  await perform(() => adminCommand(path(endpoint), { reason_code: 'MERCHANT_OPERATION', reason }, token(), product.value!.version, `merchant-product-${action}`), action === 'submit_review' ? '商品已提交审核。' : action === 'publish' ? '商品已上架。' : '商品已下架。')
}

watch(() => route.params.productId, () => void load())
watch(selectedSkuId, () => { selectedImage.value = 0; const inventory = activeInventory.value; newStock.value = inventory?.on_hand_quantity ?? null })
onMounted(() => { resetSku(); void load() })
</script>

<template>
  <section class="merchant-page-stack merchant-product-editor">
    <header class="merchant-editor-header"><RouterLink to="/merchant/products">← 返回我的商品</RouterLink><div v-if="product" class="merchant-editor-status"><span :class="`status-${product.status}`">{{ statusLabel(product.status) }}</span><small v-if="!canEdit">当前状态下资料只读；下架或审核退回后可继续编辑。</small></div></header>
    <p v-if="notice" class="alert success" aria-live="polite">{{ notice }}</p>
    <PageState :loading="loading" :error="error" :empty="!loading && !isNew && !product" empty-title="商品不存在" @retry="load">
      <form v-if="isNew" class="merchant-new-product card" @submit.prevent="saveBasic"><span>＋</span><div><p class="eyebrow">创建第一张商品卡片</p><h1>先填写商品最基本的信息</h1><p>创建草稿后，你会进入和顾客商品详情页相似的编辑模板，再添加图片、款式、库存、详情与常见问题。</p></div><label>商品名称<input v-model.trim="basic.product_name" required maxlength="255" placeholder="例如：轻量透气跑步鞋" /></label><div class="field-grid"><label>商品分类<select v-model="basic.category_id" required><option value="">请选择分类</option><option v-for="item in flatCategories" :key="item.category_id" :value="item.category_id">{{ '—'.repeat(Math.max(0, item.level - 1)) }} {{ item.category_name }}</option></select></label><label>品牌<select v-model="basic.brand_id"><option value="">无品牌</option><option v-for="item in brands" :key="item.brand_id" :value="item.brand_id">{{ item.brand_name }}</option></select></label></div><label>一句话卖点<input v-model.trim="basic.subtitle" maxlength="500" placeholder="顾客在商品卡片上首先看到的介绍" /></label><label>简短描述<textarea v-model.trim="basic.description" rows="4" maxlength="2000" /></label><button :disabled="saving || !basic.store_id">{{ saving ? '正在创建…' : '创建商品草稿，继续完善' }}</button></form>

      <template v-else-if="product">
        <div class="merchant-live-editor">
          <section class="merchant-gallery-editor">
            <div class="merchant-main-image"><img v-if="displayImage" :src="resolveApiAssetUrl(displayImage.image_url) || undefined" :alt="displayImage.alt_text || basic.product_name" /><div v-else><b>上传这件商品的第一张图片</b><p>选择款式后上传，图片会自动关联到该款式。</p></div></div>
            <div v-if="activeImages.length" class="merchant-thumbnails"><button v-for="(item, index) in activeImages" :key="`${item.file_id}-${index}`" type="button" :class="{ active: selectedImage === index }" @click="selectedImage = index"><img :src="resolveApiAssetUrl(item.image_url) || undefined" alt="" /></button></div>
            <div v-if="canEdit" class="merchant-image-actions"><label>图片属于<select v-model="selectedSkuId"><option value="">全部款式</option><option v-for="sku in skus" :key="sku.sku_id" :value="sku.sku_id">{{ sku.sku_name }}</option></select></label><AdminFileUpload purpose="product" :business-context-id="product.store_id" label="选择图片" @uploaded="addImage" /><div class="actions"><button type="button" :disabled="!images.length || saving" @click="saveImages">保存图片</button><button v-if="displayImage" type="button" class="danger small" @click="removeImage(selectedImage)">移除当前图片</button></div></div>
          </section>

          <form class="merchant-product-info-editor" @submit.prevent="saveBasic"><p class="eyebrow">顾客看到的商品信息</p><label class="merchant-title-input">商品名称<input v-model.trim="basic.product_name" required maxlength="255" :disabled="!canEdit" /></label><label>一句话卖点<input v-model.trim="basic.subtitle" maxlength="500" :disabled="!canEdit" /></label><label>商品简介<textarea v-model.trim="basic.description" rows="4" maxlength="2000" :disabled="!canEdit" /></label><div class="field-grid"><label>分类<select v-model="basic.category_id" :disabled="!canEdit"><option v-for="item in flatCategories" :key="item.category_id" :value="item.category_id">{{ item.category_name }}</option></select></label><label>品牌<select v-model="basic.brand_id" :disabled="!canEdit"><option value="">无品牌</option><option v-for="item in brands" :key="item.brand_id" :value="item.brand_id">{{ item.brand_name }}</option></select></label></div><button v-if="canEdit" :disabled="saving">保存商品信息</button>
            <div class="merchant-style-picker"><header><div><span>选择款式</span><small>点一张款式卡就能编辑</small></div><button v-if="canEdit" type="button" class="secondary small" @click="resetSku">＋ 新增款式</button></header><div><button v-for="sku in skus" :key="sku.sku_id" type="button" :class="{ active: activeSku?.sku_id === sku.sku_id }" @click="selectedSkuId = sku.sku_id; editSku(sku)"><strong>{{ sku.sku_name }}</strong><small>{{ sku.spec_values.map((spec) => spec.value).join(' · ') }}</small><b>¥{{ sku.sale_price }}</b></button></div><p v-if="!skus.length">还没有款式，请点击“新增款式”。</p></div>
          </form>
        </div>

        <section v-if="canEdit" class="merchant-inline-editor card"><header><div><p class="eyebrow">{{ skuEditing ? '编辑当前款式' : '新增款式' }}</p><h2>{{ skuEditing?.sku_name || '设置新款式与价格' }}</h2></div><button v-if="skuEditing" type="button" class="secondary" @click="resetSku">取消编辑</button></header><form @submit.prevent="saveSku"><div class="field-grid"><label>款式名称<input v-model.trim="skuForm.name" required placeholder="例如：曜石黑 / 42 码" /></label><label>店内编码（可选）<input v-model.trim="skuForm.code" maxlength="64" /></label><label>销售价（元）<input v-model="skuForm.sale_price" type="number" min="0" step="0.01" required /></label><label>市场价（元）<input v-model="skuForm.market_price" type="number" min="0" step="0.01" required /></label></div><div class="merchant-spec-editor"><div v-for="(spec, index) in specs" :key="index"><input v-model.trim="spec.name" placeholder="规格名，如颜色" maxlength="64" /><input v-model.trim="spec.value" placeholder="规格值，如曜石黑" maxlength="128" /><button type="button" class="secondary small" @click="removeSpec(index)">移除</button></div><button type="button" class="secondary small" @click="addSpec">＋ 增加一项规格</button></div><div class="field-grid"><label>重量（克，可选）<input v-model="skuForm.weight_grams" type="number" min="0" /></label><label>条形码（可选）<input v-model.trim="skuForm.barcode" maxlength="64" /></label></div><button :disabled="saving">{{ skuEditing ? '保存这款' : '新增这款' }}</button></form></section>

        <section class="merchant-stock-board card"><header><div><p class="eyebrow">实时库存</p><h2>每个款式还剩多少</h2><p>顾客成功购买后库存会自动减少，不需要手动记账。</p></div></header><div class="merchant-stock-grid"><button v-for="sku in skus" :key="sku.sku_id" type="button" :class="{ active: activeSku?.sku_id === sku.sku_id }" @click="selectedSkuId = sku.sku_id"><span>{{ sku.sku_name }}</span><strong>{{ inventoryFor(sku.sku_id)?.available_quantity ?? 0 }}</strong><small>可售 / 账面 {{ inventoryFor(sku.sku_id)?.on_hand_quantity ?? 0 }}</small></button></div><form v-if="activeInventory && canEdit" class="merchant-direct-stock" @submit.prevent="saveStock"><label>直接把「{{ activeSku?.sku_name }}」账面库存改为<input v-model.number="newStock" type="number" min="0" step="1" /></label><button :disabled="saving || newStock === null">确认修改</button><small>已被订单预占的 {{ activeInventory.reserved_quantity }} 件不会被覆盖；页面展示的可售数会自动扣除预占和安全库存。</small></form></section>

        <section class="merchant-detail-editors">
          <form class="card" @submit.prevent="saveAttributes"><header><div><p class="eyebrow">商品参数</p><h2>顾客对比商品时会看这里</h2></div><button v-if="canEdit" type="button" class="secondary small" @click="addAttribute">＋ 新增参数</button></header><div class="merchant-attribute-rows"><div v-for="(item, index) in attributes" :key="index"><input v-model.trim="item.attribute_name" placeholder="参数名" :disabled="!canEdit" /><input v-model.trim="item.value_text" placeholder="参数值" :disabled="!canEdit" /><button v-if="canEdit" type="button" class="danger small" @click="attributes.splice(index, 1)">删除</button></div></div><button v-if="canEdit" :disabled="saving">保存商品参数</button></form>
          <form class="card" @submit.prevent="saveFulfillment"><header><div><p class="eyebrow">发货与购买须知</p><h2>让顾客在下单前了解</h2></div></header><label>运费模板<select v-model="fulfillment.shipping_template_id" :disabled="!canEdit"><option value="">请选择</option><option v-for="item in shippingTemplates" :key="item.template_id" :value="item.template_id">{{ item.template_name }}</option></select></label><div class="field-grid"><label>最早发货（小时）<input v-model.number="fulfillment.dispatch_min_hours" type="number" min="0" :disabled="!canEdit" /></label><label>最晚发货（小时）<input v-model.number="fulfillment.dispatch_max_hours" type="number" min="0" :disabled="!canEdit" /></label></div><label>购买须知<textarea v-model.trim="fulfillment.purchase_notice" rows="4" maxlength="3000" :disabled="!canEdit" /></label><button v-if="canEdit" :disabled="saving">保存发货设置</button></form>
        </section>

        <section class="merchant-content-editor card"><header><div><p class="eyebrow">商品详情</p><h2>像写商品介绍一样直接编辑</h2><p>可以写材质、功能、适用场景、保养方式等完整信息。</p></div></header><textarea v-model="content" rows="12" maxlength="100000" :disabled="!canEdit" placeholder="从这里开始介绍商品…" /><button v-if="canEdit" :disabled="saving" @click="saveContent">保存商品详情</button></section>

        <section class="merchant-faq-editor card"><header><div><p class="eyebrow">常见问题</p><h2>提前回答顾客最常问的问题</h2></div><button v-if="canEdit" type="button" class="secondary" @click="resetFaq">＋ 新增问题</button></header><div class="merchant-faq-list"><button v-for="item in faqs" :key="item.faq_id" type="button" @click="editFaq(item)"><strong>{{ item.question }}</strong><p>{{ item.current_answer_text || '尚未填写答案' }}</p><small>{{ item.status === 'published' ? '已对顾客展示' : '草稿' }} · 点击编辑</small></button></div><form v-if="canEdit" @submit.prevent="saveFaq"><label>问题<input v-model.trim="faqForm.question" required maxlength="1000" placeholder="例如：尺码偏大还是偏小？" :disabled="Boolean(faqEditing)" /></label><label>回答<textarea v-model.trim="faqForm.answer" required rows="5" maxlength="100000" /></label><div class="actions"><button :disabled="saving">{{ faqEditing ? '更新并发布回答' : '新增并发布问题' }}</button><button v-if="faqEditing" type="button" class="secondary" @click="resetFaq">取消</button></div></form></section>

        <footer class="merchant-publication-bar"><div><strong>{{ statusLabel(product.status) }}</strong><span v-if="product.completeness.missing_requirements.length">还需补充：{{ product.completeness.missing_requirements.join('、') }}</span><span v-else>商品资料已完整</span></div><div class="actions"><RouterLink v-if="product.status === 'on_sale'" :to="`/products/${product.product_id}`" target="_blank">预览商品 ↗</RouterLink><button v-if="product.available_actions.includes('submit_review')" :disabled="saving" @click="productCommand('submit_review')">提交平台审核</button><button v-if="product.available_actions.includes('publish')" :disabled="saving" @click="productCommand('publish')">立即上架</button><button v-if="product.available_actions.includes('off_shelf')" class="danger" :disabled="saving" @click="productCommand('off_shelf')">下架商品</button></div></footer>
      </template>
    </PageState>
  </section>
</template>
