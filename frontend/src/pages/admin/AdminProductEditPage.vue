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

const props = withDefaults(defineProps<{ portal?: 'admin' | 'merchant' }>(), { portal: 'admin' })

type Section = 'basic' | 'skus' | 'images' | 'attributes' | 'fulfillment' | 'content' | 'faqs' | 'publication'
interface Fulfillment { shipping_template_id: string; origin_region_code: string; dispatch_min_hours: number; dispatch_max_hours: number; purchase_notice: string | null; profile_version: number; version: number }

const route = useRoute(); const router = useRouter(); const auth = useAdminAuthStore()
const isNew = computed(() => route.path.endsWith('/new'))
const productId = computed(() => String(route.params.productId || ''))
const product = ref<AdminProduct | null>(null); const skus = ref<AdminSku[]>([]); const faqs = ref<AdminProductFaq[]>([]); const fulfillment = ref<Fulfillment | null>(null)
const categories = ref<Category[]>([]); const brands = ref<Array<Brand | AdminBrand>>([]); const stores = ref<AdminStore[]>([]); const shippingTemplates = ref<AdminShippingTemplate[]>([])
const section = ref<Section>('basic'); const loading = ref(true); const saving = ref(false); const error = ref(''); const message = ref('')
const basic = reactive({ store_id: '', category_id: '', brand_id: '', product_name: '', subtitle: '', description: '' })
const skuEditing = ref<AdminSku | null>(null); const skuForm = reactive({ merchant_sku_code: '', sku_name: '', specs: '[{"name":"规格","value":"标准"}]', sale_price: '', market_price: '', weight_grams: '', barcode: '', action: 'disable', reason_code: 'MERCHANT_OPERATION', reason: '' })
const imageDraft = ref<AdminProductImage[]>([])
const attributeDraft = ref<AdminProductAttribute[]>([])
const fulfillmentForm = reactive({ shipping_template_id: '', origin_region_code: 'CN', dispatch_min_hours: 24, dispatch_max_hours: 48, purchase_notice: '' })
const contentForm = reactive({ source_format: 'plain_text', source_content: '' }); const contentResult = ref<AdminContentVersion | null>(null)
const faqForm = reactive({ question: '', source_format: 'plain_text', source_content: '', sort_order: 0, publish_reason: '' })
const faqEditing = ref<AdminProductFaq | null>(null)
const commandForm = reactive({ action: 'submit_review', decision: 'approve', reason_code: 'CONTENT_VERIFIED', reason: '' })
const flatCategories = computed(() => flatten(categories.value))
const merchantMode = computed(() => props.portal === 'merchant')
const productListPath = computed(() => merchantMode.value ? '/merchant/products' : '/admin/products')

function token() { return requireAdminToken(auth.accessToken) }
function path(suffix = '') { return `/admin/products/${encodeURIComponent(productId.value)}${suffix}` }
function flatten(nodes: Category[]): Category[] { return nodes.flatMap((item) => [item, ...flatten(item.children)]) }
function minor(value: string): number { return Math.round(Number(value) * 100) }

async function loadReferences() {
  const tasks: Promise<unknown>[] = [
    getCategories().then((result) => { categories.value = result.data }),
    getBrands().then((result) => { brands.value = result.data }),
  ]
  if (auth.has('stores:read')) tasks.push(adminGet<{ items: AdminStore[]; next_cursor: string | null }>('/admin/stores?limit=100', token()).then((result) => { stores.value = result.data.items }))
  await Promise.allSettled(tasks)
}

async function load() {
  loading.value = true; error.value = ''
  await loadReferences()
  if (isNew.value) {
    product.value = null
    const onlyStore = stores.value.length === 1 ? stores.value.at(0) : undefined
    if (merchantMode.value && onlyStore) basic.store_id = onlyStore.store_id
    loading.value = false
    return
  }
  try {
    const [detail, skuResult, imageResult, attributeResult, fulfillmentResult, faqResult] = await Promise.all([
      adminGet<AdminProduct>(path(), token()), adminGet<AdminSku[]>(path('/skus'), token()), adminGet<AdminProductImage[]>(path('/images'), token()), adminGet<AdminProductAttribute[]>(path('/attributes'), token()), adminGet<Fulfillment | null>(path('/fulfillment-profile'), token()), adminGet<AdminProductFaq[]>(path('/faqs'), token()),
    ])
    product.value = detail.data; skus.value = skuResult.data; imageDraft.value = imageResult.data.map((item) => ({ ...item })); attributeDraft.value = attributeResult.data.map((item) => ({ ...item })); fulfillment.value = fulfillmentResult.data; faqs.value = faqResult.data
    Object.assign(basic, { store_id: product.value.store_id, category_id: product.value.category_id, brand_id: product.value.brand_id ?? '', product_name: product.value.product_name, subtitle: product.value.subtitle ?? '', description: product.value.description ?? '' })
    if (fulfillment.value) Object.assign(fulfillmentForm, { shipping_template_id: fulfillment.value.shipping_template_id, origin_region_code: fulfillment.value.origin_region_code, dispatch_min_hours: fulfillment.value.dispatch_min_hours, dispatch_max_hours: fulfillment.value.dispatch_max_hours, purchase_notice: fulfillment.value.purchase_notice ?? '' })
    const nextCommand = product.value.available_actions.find((action) => ['submit_review', 'moderate', 'publish', 'off_shelf'].includes(action))
    commandForm.action = nextCommand || ''
    await loadShippingTemplates()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function loadShippingTemplates() {
  if (!basic.store_id) return
  try { shippingTemplates.value = (await adminGet<AdminShippingTemplate[]>(`/admin/stores/${encodeURIComponent(basic.store_id)}/shipping-templates`, token())).data }
  catch { shippingTemplates.value = [] }
}

async function run(action: () => Promise<unknown>, success: string, reload = true) {
  saving.value = true; error.value = ''; message.value = ''
  try { await action(); message.value = success; if (reload) await load() }
  catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

async function saveBasic() {
  if (isNew.value) {
    saving.value = true; error.value = ''
    try {
      const created = (await adminCreate<AdminProduct>('/admin/products', { store_id: basic.store_id, category_id: basic.category_id, brand_id: basic.brand_id || null, product_name: basic.product_name, subtitle: basic.subtitle || null, description: basic.description || null }, token(), 'product-create')).data
      await router.replace(`${productListPath.value}/${created.product_id}`)
    } catch (cause) { error.value = errorMessage(cause) }
    finally { saving.value = false }
    return
  }
  if (!product.value) return
  await run(() => adminUpdate(path(), { category_id: basic.category_id, brand_id: basic.brand_id || null, product_name: basic.product_name, subtitle: basic.subtitle || null, description: basic.description || null }, token(), product.value!.version), '商品基础信息已保存。')
}

function resetSku() { skuEditing.value = null; Object.assign(skuForm, { merchant_sku_code: '', sku_name: '', specs: '[{"name":"规格","value":"标准"}]', sale_price: '', market_price: '', weight_grams: '', barcode: '', action: 'disable', reason_code: 'MERCHANT_OPERATION', reason: '' }) }
function editSku(item: AdminSku) { skuEditing.value = item; Object.assign(skuForm, { merchant_sku_code: item.merchant_sku_code ?? '', sku_name: item.sku_name, specs: JSON.stringify(item.spec_values, null, 2), sale_price: item.sale_price, market_price: item.market_price, weight_grams: item.weight_grams == null ? '' : String(item.weight_grams), barcode: item.barcode ?? '', action: item.status === 'active' ? 'disable' : 'enable', reason_code: 'MERCHANT_OPERATION', reason: '' }) }
async function saveSku() {
  let specs: Array<{ name: string; value: string }>
  try { specs = JSON.parse(skuForm.specs) as Array<{ name: string; value: string }> } catch { error.value = '规格必须是有效 JSON 数组。'; return }
  const payload = { merchant_sku_code: skuForm.merchant_sku_code || null, sku_name: skuForm.sku_name, spec_values: specs, sale_price_amount: minor(skuForm.sale_price), market_price_amount: minor(skuForm.market_price), weight_grams: skuForm.weight_grams === '' ? null : Number(skuForm.weight_grams), barcode: skuForm.barcode || null }
  await run(async () => { if (skuEditing.value) await adminUpdate(path(`/skus/${encodeURIComponent(skuEditing.value.sku_id)}`), payload, token(), skuEditing.value.version); else await adminCreate(path('/skus'), { ...payload, currency: 'CNY' }, token(), 'sku-create'); resetSku() }, skuEditing.value ? 'SKU 已更新。' : 'SKU 已创建。')
}
function changeSkuStatus() { if (!skuEditing.value) return; return run(() => adminCommand(path(`/skus/${encodeURIComponent(skuEditing.value!.sku_id)}/status-changes`), { action: skuForm.action, reason_code: skuForm.reason_code, reason: skuForm.reason }, token(), skuEditing.value!.version, 'sku-status'), 'SKU 状态已更新。') }

function addImage(fileId: string) { imageDraft.value.push({ file_id: fileId, sku_id: null, image_type: imageDraft.value.some((item) => item.image_type === 'main' && item.sku_id === null) ? 'gallery' : 'main', alt_text: product.value?.product_name ?? '', sort_order: imageDraft.value.length, image_url: `/api/v1/files/${fileId}`, width: 0, height: 0, status: 'active' }) }
function removeImage(index: number) { imageDraft.value.splice(index, 1); imageDraft.value.forEach((item, order) => { item.sort_order = order }) }
function saveImages() { if (!product.value) return; return run(() => adminReplace(path('/images'), { items: imageDraft.value.map(({ file_id, sku_id, image_type, alt_text, sort_order }) => ({ file_id, sku_id, image_type, alt_text: alt_text || null, sort_order })) }, token(), product.value!.version), '商品图片集合已替换。') }

function addAttribute() { attributeDraft.value.push({ attribute_code: '', attribute_name: '', value_text: '', value_normalized: null, unit: null, is_searchable: false, sort_order: attributeDraft.value.length }) }
function saveAttributes() { if (!product.value) return; return run(() => adminReplace(path('/attributes'), { items: attributeDraft.value.map((item, index) => ({ ...item, value_normalized: item.value_normalized || null, unit: item.unit || null, sort_order: index })) }, token(), product.value!.version), '商品属性集合已替换。') }

function saveFulfillment() { if (!product.value) return; return run(() => adminReplace(path('/fulfillment-profile'), { shipping_template_id: fulfillmentForm.shipping_template_id, origin_region_code: fulfillmentForm.origin_region_code, dispatch_min_hours: fulfillmentForm.dispatch_min_hours, dispatch_max_hours: fulfillmentForm.dispatch_max_hours, purchase_notice: fulfillmentForm.purchase_notice || null }, token(), product.value!.version), '履约资料已保存。') }

async function createContent() { await run(async () => { contentResult.value = (await adminCreate<AdminContentVersion>(path('/detail-content-versions'), { source_format: contentForm.source_format, source_content: contentForm.source_content }, token(), 'product-content-create')).data }, '详情内容安全版本已生成。') }
async function loadContentVersion(versionId: string | null) { if (!versionId) return; error.value = ''; try { contentResult.value = (await adminGet<AdminContentVersion>(path(`/detail-content-versions/${encodeURIComponent(versionId)}`), token())).data } catch (cause) { error.value = errorMessage(cause) } }
function editFaq(item: AdminProductFaq) { faqEditing.value = item; Object.assign(faqForm, { question: item.question, source_format: 'plain_text', source_content: '', sort_order: item.sort_order }) }
function resetFaq() { faqEditing.value = null; Object.assign(faqForm, { question: '', source_format: 'plain_text', source_content: '', sort_order: 0 }) }
async function createFaq() {
  await run(async () => {
    if (faqEditing.value) await adminCreate(path(`/faqs/${encodeURIComponent(faqEditing.value.faq_id)}/versions`), { source_format: faqForm.source_format, source_content: faqForm.source_content }, token(), 'product-faq-version')
    else await adminCreate(path('/faqs'), { question: faqForm.question, sort_order: faqForm.sort_order, source_format: faqForm.source_format, source_content: faqForm.source_content }, token(), 'product-faq-create')
    resetFaq()
  }, faqEditing.value ? 'FAQ 新答案版本已创建。' : 'FAQ 草稿已创建。')
}
async function publishFaq(item: AdminProductFaq) { if (!item.current_version_id || faqForm.publish_reason.trim().length < 2) { error.value = 'FAQ 发布需要当前内容版本和至少 2 个字的发布原因。'; return } await run(() => adminCreate(path(`/faqs/${encodeURIComponent(item.faq_id)}/publications`), { version_id: item.current_version_id, reason: faqForm.publish_reason }, token(), 'product-faq-publish'), 'FAQ 已发布。') }

async function productCommand() {
  if (!product.value) return
  const map = { submit_review: '/review-submissions', moderate: '/moderation-decisions', publish: '/publications', off_shelf: '/off-shelf-commands' } as const
  const payload: Record<string, string> = { reason_code: commandForm.reason_code, reason: commandForm.reason }
  if (commandForm.action === 'moderate') payload.decision = commandForm.decision
  await run(() => adminCommand(path(map[commandForm.action as keyof typeof map]), payload, token(), product.value!.version, `product-${commandForm.action}`), '商品状态命令已执行。')
}

watch(() => route.params.productId, load)
onMounted(() => { resetSku(); void load() })
</script>

<template>
  <section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">{{ merchantMode ? '商品中心' : '商品管理' }}</p><h1>{{ isNew ? '新建商品' : product?.product_name || '商品详情' }}</h1><p class="muted">{{ isNew ? '先创建商品草稿，再按步骤补齐销售信息并提交上架审核。' : `${productId} · ${product?.store_name || ''}` }}</p></div><RouterLink :to="productListPath">返回商品列表</RouterLink></header><p v-if="message" class="alert success" aria-live="polite">{{ message }}</p><PageState :loading="loading" :error="error" :empty="!loading && !isNew && !product" empty-title="商品不存在" @retry="load"><template v-if="isNew || product"><nav v-if="!isNew" class="tab-list" aria-label="商品编辑分区"><button v-for="entry in ([['basic','基础信息'],['skus','销售规格'],['images','商品图片'],['attributes','商品参数'],['fulfillment','发货设置'],['content','商品详情'],['faqs','常见问题'],['publication','上架检查']] as const)" :key="entry[0]" type="button" :class="{ active: section === entry[0] }" @click="section = entry[0]">{{ entry[1] }}</button></nav>
      <form v-if="isNew || section === 'basic'" class="card admin-editor wide-editor" @submit.prevent="saveBasic"><h2>基础信息</h2><label v-if="!merchantMode">店铺公开 ID<input v-model.trim="basic.store_id" required maxlength="40" list="store-options" :disabled="!isNew" placeholder="sto_…" /><datalist id="store-options"><option v-for="item in stores" :key="item.store_id" :value="item.store_id">{{ item.store_name }}</option></datalist></label><div v-else class="merchant-store-indicator"><span>所属店铺</span><strong>{{ stores.find((item) => item.store_id === basic.store_id)?.store_name || product?.store_name || '当前店铺' }}</strong></div><div class="field-grid"><label>平台分类<select v-model="basic.category_id" required><option value="">请选择</option><option v-for="item in flatCategories" :key="item.category_id" :value="item.category_id">{{ '—'.repeat(Math.max(0, item.level - 1)) }} {{ item.category_name }}</option></select></label><label>品牌<select v-model="basic.brand_id"><option value="">无品牌</option><option v-for="item in brands" :key="item.brand_id" :value="item.brand_id">{{ item.brand_name }}</option></select></label></div><label>商品名称<input v-model.trim="basic.product_name" required maxlength="255" /></label><label>副标题<input v-model.trim="basic.subtitle" maxlength="500" /></label><label>商品简介<textarea v-model.trim="basic.description" maxlength="2000" /></label><button :disabled="saving || !basic.store_id">{{ isNew ? '创建商品草稿' : '保存基础信息' }}</button></form>
      <div v-else-if="section === 'skus'" class="admin-split"><div class="card-list"><article v-for="item in skus" :key="item.sku_id" class="card"><div class="card-heading"><div><h2>{{ item.sku_name }}</h2><p class="muted">{{ item.sku_id }}</p></div><span class="badge">{{ item.status }}</span></div><p>售价 ¥{{ item.sale_price }} · 市场价 ¥{{ item.market_price }}</p><p>{{ item.spec_values.map((spec) => `${spec.name}:${spec.value}`).join(' / ') }}</p><button type="button" class="secondary small" @click="editSku(item)">编辑</button></article></div><form class="card admin-editor" @submit.prevent="saveSku"><h2>{{ skuEditing ? '编辑 SKU' : '新建 SKU' }}</h2><label>SKU 名称<input v-model.trim="skuForm.sku_name" required maxlength="255" /></label><label>商家 SKU 编码<input v-model.trim="skuForm.merchant_sku_code" maxlength="64" /></label><label>规格 JSON<textarea v-model="skuForm.specs" required rows="6" /></label><div class="field-grid"><label>售价（元）<input v-model="skuForm.sale_price" type="number" min="0" step="0.01" required /></label><label>市场价（元）<input v-model="skuForm.market_price" type="number" min="0" step="0.01" required /></label><label>重量（克）<input v-model="skuForm.weight_grams" type="number" min="0" /></label><label>条码<input v-model.trim="skuForm.barcode" maxlength="64" /></label></div><div class="actions"><button :disabled="saving">保存 SKU</button><button v-if="skuEditing" type="button" class="secondary" @click="resetSku">取消</button></div><fieldset v-if="skuEditing" class="command-box"><legend>SKU 状态命令</legend><label>动作<select v-model="skuForm.action"><option value="enable">启用</option><option value="disable">禁用</option></select></label><label>原因码<input v-model.trim="skuForm.reason_code" pattern="[A-Z][A-Z0-9_]{1,63}" /></label><label>原因<textarea v-model.trim="skuForm.reason" minlength="2" maxlength="500" /></label><button type="button" class="danger" :disabled="skuForm.reason.length < 2 || saving" @click="changeSkuStatus">执行状态命令</button></fieldset></form></div>
      <div v-else-if="section === 'images'" class="admin-page-stack"><AdminFileUpload purpose="product" :business-context-id="product?.store_id" label="上传商品图片" @uploaded="addImage" /><div class="image-admin-grid"><article v-for="(item, index) in imageDraft" :key="`${item.file_id}-${index}`" class="card"><img :src="resolveApiAssetUrl(item.image_url) || undefined" :alt="item.alt_text || ''" /><label>用途<select v-model="item.image_type"><option value="main">主图</option><option value="gallery">图库</option><option value="detail">详情</option><option value="spec">规格图</option></select></label><label>关联 SKU<select v-model="item.sku_id"><option :value="null">公共图片</option><option v-for="sku in skus" :key="sku.sku_id" :value="sku.sku_id">{{ sku.sku_name }}</option></select></label><label>替代文本<input v-model.trim="item.alt_text" maxlength="255" /></label><button type="button" class="danger small" @click="removeImage(index)">移除</button></article></div><button :disabled="saving || imageDraft.length === 0" @click="saveImages">完整替换图片集合</button></div>
      <div v-else-if="section === 'attributes'" class="admin-page-stack"><div class="table-wrap"><table><thead><tr><th>编码</th><th>名称</th><th>值</th><th>单位</th><th>可搜索</th><th>操作</th></tr></thead><tbody><tr v-for="(item, index) in attributeDraft" :key="index"><td><input v-model.trim="item.attribute_code" pattern="[a-z][a-z0-9_]{1,63}" required /></td><td><input v-model.trim="item.attribute_name" required /></td><td><input v-model.trim="item.value_text" required /></td><td><input v-model.trim="item.unit" /></td><td><input v-model="item.is_searchable" type="checkbox" /></td><td><button type="button" class="danger small" @click="attributeDraft.splice(index, 1)">移除</button></td></tr></tbody></table></div><div class="actions"><button type="button" class="secondary" @click="addAttribute">新增属性</button><button :disabled="saving" @click="saveAttributes">完整替换属性集合</button></div></div>
      <form v-else-if="section === 'fulfillment'" class="card admin-editor wide-editor" @submit.prevent="saveFulfillment"><h2>履约资料</h2><label>已生效配送模板<select v-model="fulfillmentForm.shipping_template_id" required><option value="">请选择</option><option v-for="item in shippingTemplates.filter((value) => value.status === 'effective')" :key="item.template_id" :value="item.template_id">{{ item.template_name }} · v{{ item.policy_version }}</option></select></label><label>发货地区码<input v-model.trim="fulfillmentForm.origin_region_code" required pattern="[A-Z0-9_-]{2,32}" /></label><div class="field-grid"><label>最短发货小时<input v-model.number="fulfillmentForm.dispatch_min_hours" type="number" min="0" max="8760" required /></label><label>最长发货小时<input v-model.number="fulfillmentForm.dispatch_max_hours" type="number" min="0" max="8760" required /></label></div><label>购买须知<textarea v-model.trim="fulfillmentForm.purchase_notice" maxlength="3000" /></label><button :disabled="saving">保存履约资料</button></form>
      <div v-else-if="section === 'content'" class="admin-detail-grid"><form class="card admin-editor" @submit.prevent="createContent"><h2>创建不可变详情版本</h2><label>源格式<select v-model="contentForm.source_format"><option value="plain_text">纯文本</option><option value="structured">结构化内容</option><option value="html">HTML</option></select></label><label>源内容<textarea v-model="contentForm.source_content" required rows="16" maxlength="100000" /></label><p class="muted">服务端将重新执行 Sanitization 与内容安全扫描；公开接口永不返回原始源内容。</p><button :disabled="saving">创建安全版本</button></form><article class="card"><h2>当前版本</h2><p>当前：{{ product?.current_detail_content_version_id || '未创建' }}</p><p>已发布：{{ product?.published_detail_content_version_id || '未发布' }}</p><div class="actions"><button type="button" class="secondary small" :disabled="!product?.current_detail_content_version_id" @click="loadContentVersion(product?.current_detail_content_version_id || null)">查看当前版本</button><button type="button" class="secondary small" :disabled="!product?.published_detail_content_version_id" @click="loadContentVersion(product?.published_detail_content_version_id || null)">查看已发布版本</button></div><template v-if="contentResult"><h3>安全派生结果</h3><p><span class="badge">{{ contentResult.security_scan_status }}</span> {{ contentResult.public_content_format }}</p><pre class="safe-preview">{{ contentResult.safe_text }}</pre></template></article></div>
      <div v-else-if="section === 'faqs'" class="admin-split"><div class="card-list"><article v-for="item in faqs" :key="item.faq_id" class="card"><div class="card-heading"><h2>{{ item.question }}</h2><span class="badge">{{ item.status }}</span></div><p class="muted">当前 {{ item.current_version_id || '—' }} · 已发布 {{ item.published_version_id || '—' }}</p><div class="actions"><button type="button" class="secondary small" @click="editFaq(item)">新建答案版本</button><button type="button" :disabled="!item.current_version_id || faqForm.publish_reason.trim().length < 2 || saving" @click="publishFaq(item)">发布当前版本</button></div></article></div><form class="card admin-editor" @submit.prevent="createFaq"><h2>{{ faqEditing ? '新建 FAQ 答案版本' : '新建 FAQ' }}</h2><label>问题<input v-model.trim="faqForm.question" required maxlength="1000" :disabled="Boolean(faqEditing)" /></label><label>答案源格式<select v-model="faqForm.source_format"><option value="plain_text">纯文本</option><option value="structured">结构化</option><option value="html">HTML</option></select></label><label>答案内容<textarea v-model="faqForm.source_content" required rows="10" maxlength="100000" /></label><label v-if="!faqEditing">排序<input v-model.number="faqForm.sort_order" type="number" min="0" /></label><label>发布原因<textarea v-model.trim="faqForm.publish_reason" maxlength="500" placeholder="发布已有 FAQ 时使用" /></label><div class="actions"><button :disabled="saving">{{ faqEditing ? '创建答案版本' : '创建 FAQ 草稿' }}</button><button v-if="faqEditing" type="button" class="secondary" @click="resetFaq">取消</button></div></form></div>
      <div v-else class="admin-detail-grid"><article class="card"><h2>发布完整性检查</h2><ul class="check-list"><li :class="{ pass: product?.completeness.basic }">基础信息</li><li :class="{ pass: product?.completeness.sku }">至少一个有效 SKU</li><li :class="{ pass: product?.completeness.main_image }">公共主图</li><li :class="{ pass: product?.completeness.attributes }">商品属性</li><li :class="{ pass: product?.completeness.fulfillment }">履约资料</li><li :class="{ pass: product?.completeness.detail_content }">安全详情版本</li></ul><div v-if="product?.completeness.missing_requirements.length" class="alert error"><strong>仍缺少：</strong>{{ product.completeness.missing_requirements.join('、') }}</div><p>当前状态：<span class="badge">{{ product?.status }}</span> · 资源版本 v{{ product?.version }}</p><p>服务端允许动作：{{ product?.available_actions.join('、') || '无' }}</p></article><form class="card admin-editor" @submit.prevent="productCommand"><h2>显式状态命令</h2><label>动作<select v-model="commandForm.action"><option v-if="product?.available_actions.includes('submit_review')" value="submit_review">提交审核</option><option v-if="product?.available_actions.includes('moderate') && auth.has('products:review')" value="moderate">审核决定</option><option v-if="product?.available_actions.includes('publish') && auth.has('products:publish')" value="publish">正式发布</option><option v-if="product?.available_actions.includes('off_shelf') && auth.has('products:publish')" value="off_shelf">下架</option></select></label><label v-if="commandForm.action === 'moderate'">审核决定<select v-model="commandForm.decision"><option value="approve">通过</option><option value="reject">拒绝</option><option value="request_changes">要求修改</option></select></label><label>原因码<input v-model.trim="commandForm.reason_code" required pattern="[A-Z][A-Z0-9_]{1,63}" /></label><label>原因<textarea v-model.trim="commandForm.reason" required minlength="2" maxlength="500" /></label><button class="danger" :disabled="saving || !commandForm.action">确认执行</button></form></div>
    </template></PageState></section>
</template>
