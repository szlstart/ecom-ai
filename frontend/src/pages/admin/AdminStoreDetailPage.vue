<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import {
  adminCommand,
  adminCreate,
  adminGet,
  adminReplace,
  adminUpdate,
  requireAdminToken,
  type AdminFeaturedProduct,
  type AdminShippingTemplate,
  type AdminStore,
  type AdminStoreAnnouncement,
  type AdminStoreGroup,
} from '@/api/admin-catalog'
import { apiRequest, errorMessage } from '@/api/http'
import AdminFileUpload from '@/components/AdminFileUpload.vue'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

type Section = 'profile' | 'groups' | 'shipping' | 'announcements' | 'featured'

const route = useRoute(); const router = useRouter(); const auth = useAdminAuthStore()
const storeId = computed(() => String(route.params.storeId))
const store = ref<AdminStore | null>(null)
const groups = ref<AdminStoreGroup[]>([])
const shipping = ref<AdminShippingTemplate[]>([])
const announcements = ref<AdminStoreAnnouncement[]>([])
const featured = ref<AdminFeaturedProduct[]>([])
const section = ref<Section>('profile'); const loading = ref(true); const saving = ref(false)
const error = ref(''); const message = ref('')
const profile = reactive({ store_name: '', description: '', logo_file_id: '' })
const statusForm = reactive({ action: 'suspend', reason_code: 'OPERATIONS_REVIEW', reason: '' })
const groupForm = reactive({ group_name: '', parent_group_id: '', sort_order: 0, product_ids: '' })
const selectedGroup = ref<AdminStoreGroup | null>(null)
const shippingEditing = ref<AdminShippingTemplate | null>(null)
const shippingForm = reactive({ template_name: '', delivery_type: 'express', charge_mode: 'by_item', dispatch_min_hours: 24, dispatch_max_hours: 48, region_scope: '{"country":"CN"}', first_unit: 1, additional_unit: 1, first_fee_amount: 0, additional_fee_amount: 0, estimated_min_days: 1, estimated_max_days: 3, reason: '' })
const announcementForm = reactive({ title: '', content: '', status: 'draft', starts_at: '', ends_at: '', sort_order: 0 })
const announcementEditing = ref<AdminStoreAnnouncement | null>(null)
const featuredForm = reactive({ slot_type: 'recommended', product_ids: '' })
const deleteOpen = ref(false)
const deleteReason = ref('')
const deleteConfirmation = ref('')

function token() { return requireAdminToken(auth.accessToken) }
function endpoint(suffix: string) { return `/admin/stores/${encodeURIComponent(storeId.value)}${suffix}` }
function iso(value: string) { return value ? new Date(value).toISOString() : null }

async function load() {
  loading.value = true; error.value = ''
  try {
    const [storeResult, groupResult, shippingResult, announcementResult, featuredResult] = await Promise.all([
      adminGet<AdminStore>(endpoint(''), token()),
      adminGet<AdminStoreGroup[]>(endpoint('/product-groups'), token()),
      adminGet<AdminShippingTemplate[]>(endpoint('/shipping-templates'), token()),
      adminGet<AdminStoreAnnouncement[]>(endpoint('/announcements'), token()),
      adminGet<AdminFeaturedProduct[]>(endpoint(`/featured-products?slot_type=${featuredForm.slot_type}`), token()),
    ])
    store.value = storeResult.data; groups.value = groupResult.data; shipping.value = shippingResult.data; announcements.value = announcementResult.data; featured.value = featuredResult.data
    Object.assign(profile, { store_name: store.value.store_name, description: store.value.description ?? '', logo_file_id: '' })
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function run(action: () => Promise<unknown>, success: string) {
  saving.value = true; error.value = ''; message.value = ''
  try { await action(); message.value = success; await load() }
  catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

function saveProfile() {
  if (!store.value) return
  const payload: Record<string, unknown> = { store_name: profile.store_name, description: profile.description || null }
  if (profile.logo_file_id) payload.logo_file_id = profile.logo_file_id
  return run(() => adminUpdate<AdminStore>(endpoint(''), payload, token(), store.value!.version), '店铺资料已更新。')
}

function changeStatus() {
  if (!store.value) return
  return run(() => adminCommand<AdminStore>(endpoint('/status-changes'), { ...statusForm }, token(), store.value!.version, 'store-status'), '店铺状态命令已执行。')
}

async function deleteStore() {
  if (!store.value || deleteConfirmation.value !== 'DELETE_STORE' || deleteReason.value.trim().length < 2) return
  saving.value = true; error.value = ''
  try {
    await apiRequest(endpoint(''), { method: 'DELETE', headers: { 'If-Match': `"v${store.value.version}"` }, body: JSON.stringify({ reason: deleteReason.value.trim(), confirmation: deleteConfirmation.value }) }, token())
    await router.replace({ path: '/admin/stores', query: { deleted: storeId.value } })
  } catch (cause) { error.value = errorMessage(cause); deleteOpen.value = false }
  finally { saving.value = false }
}

function chooseGroup(item: AdminStoreGroup) { selectedGroup.value = item; Object.assign(groupForm, { group_name: item.group_name, parent_group_id: item.parent_group_id ?? '', sort_order: item.sort_order, product_ids: item.product_ids.join('\n') }) }
function resetGroup() { selectedGroup.value = null; Object.assign(groupForm, { group_name: '', parent_group_id: '', sort_order: 0, product_ids: '' }) }
async function saveGroup() {
  const payload = { group_name: groupForm.group_name, parent_group_id: groupForm.parent_group_id || null, sort_order: groupForm.sort_order }
  await run(async () => {
    let current: AdminStoreGroup
    if (selectedGroup.value) current = (await adminUpdate<AdminStoreGroup>(endpoint(`/product-groups/${encodeURIComponent(selectedGroup.value.group_id)}`), payload, token(), selectedGroup.value.version)).data
    else current = (await adminCreate<AdminStoreGroup>(endpoint('/product-groups'), payload, token(), 'store-group-create')).data
    const productIds = groupForm.product_ids.split(/[\s,]+/).map((value) => value.trim()).filter(Boolean)
    if (productIds.length || selectedGroup.value?.product_ids.length) await adminReplace(endpoint(`/product-groups/${encodeURIComponent(current.group_id)}/products`), { product_ids: productIds }, token(), current.version)
    resetGroup()
  }, selectedGroup.value ? '店铺分组已更新。' : '店铺分组已创建。')
}

function editShipping(item: AdminShippingTemplate) {
  shippingEditing.value = item
  const rule = item.rules[0]
  Object.assign(shippingForm, { template_name: item.template_name, delivery_type: item.delivery_type, charge_mode: item.charge_mode, dispatch_min_hours: item.dispatch_min_hours, dispatch_max_hours: item.dispatch_max_hours, region_scope: JSON.stringify(rule?.region_scope ?? { country: 'CN' }), first_unit: rule?.first_unit ?? 1, additional_unit: rule?.additional_unit ?? 1, first_fee_amount: rule?.first_fee_amount ?? 0, additional_fee_amount: rule?.additional_fee_amount ?? 0, estimated_min_days: rule?.estimated_min_days ?? 1, estimated_max_days: rule?.estimated_max_days ?? 3, reason: '' })
}
function resetShipping() { shippingEditing.value = null; Object.assign(shippingForm, { template_name: '', delivery_type: 'express', charge_mode: 'by_item', dispatch_min_hours: 24, dispatch_max_hours: 48, region_scope: '{"country":"CN"}', first_unit: 1, additional_unit: 1, first_fee_amount: 0, additional_fee_amount: 0, estimated_min_days: 1, estimated_max_days: 3, reason: '' }) }
function saveShipping() {
  let regionScope: Record<string, unknown>
  try { regionScope = JSON.parse(shippingForm.region_scope) as Record<string, unknown> }
  catch { error.value = '区域范围必须是有效 JSON。'; return }
  const payload = { template_name: shippingForm.template_name, delivery_type: shippingForm.delivery_type, charge_mode: shippingForm.charge_mode, dispatch_min_hours: shippingForm.dispatch_min_hours, dispatch_max_hours: shippingForm.dispatch_max_hours, rules: [{ region_scope: regionScope, first_unit: shippingForm.first_unit, additional_unit: shippingForm.additional_unit, first_fee_amount: shippingForm.first_fee_amount, additional_fee_amount: shippingForm.additional_fee_amount, estimated_min_days: shippingForm.estimated_min_days, estimated_max_days: shippingForm.estimated_max_days }] }
  return run(async () => { if (shippingEditing.value) await adminUpdate(endpoint(`/shipping-templates/${encodeURIComponent(shippingEditing.value.template_id)}`), payload, token(), shippingEditing.value.version); else await adminCreate(endpoint('/shipping-templates'), { ...payload, template_family_id: null, currency: 'CNY' }, token(), 'shipping-template-create'); resetShipping() }, shippingEditing.value ? '配送模板草稿已更新。' : '配送模板草稿已创建。')
}
function publishShipping(item: AdminShippingTemplate) { return run(() => adminCommand(endpoint(`/shipping-templates/${encodeURIComponent(item.template_id)}/publications`), { reason: shippingForm.reason }, token(), item.version, 'shipping-template-publish'), '配送模板版本已发布。') }

function editAnnouncement(item: AdminStoreAnnouncement) { announcementEditing.value = item; Object.assign(announcementForm, { title: item.title, content: item.content, status: item.status, starts_at: item.starts_at?.slice(0, 16) ?? '', ends_at: item.ends_at?.slice(0, 16) ?? '', sort_order: item.sort_order }) }
function resetAnnouncement() { announcementEditing.value = null; Object.assign(announcementForm, { title: '', content: '', status: 'draft', starts_at: '', ends_at: '', sort_order: 0 }) }
function saveAnnouncement() {
  const payload = { ...announcementForm, starts_at: iso(announcementForm.starts_at), ends_at: iso(announcementForm.ends_at) }
  return run(async () => { if (announcementEditing.value) await adminUpdate(endpoint(`/announcements/${encodeURIComponent(announcementEditing.value.announcement_id)}`), payload, token(), announcementEditing.value.version); else await adminCreate(endpoint('/announcements'), payload, token(), 'store-announcement-create'); resetAnnouncement() }, announcementEditing.value ? '店铺公告已更新。' : '店铺公告已创建。')
}

async function loadFeatured() {
  try { featured.value = (await adminGet<AdminFeaturedProduct[]>(endpoint(`/featured-products?slot_type=${featuredForm.slot_type}`), token())).data }
  catch (cause) { error.value = errorMessage(cause) }
}
function replaceFeatured() {
  if (!store.value) return
  const items = featuredForm.product_ids.split(/[\s,]+/).map((product_id) => product_id.trim()).filter(Boolean).map((product_id) => ({ product_id, starts_at: null, ends_at: null }))
  return run(() => adminReplace(endpoint('/featured-products'), { slot_type: featuredForm.slot_type, items }, token(), store.value!.version), '推荐位已完整替换。')
}

onMounted(load)
</script>

<template>
  <section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">店铺运营 · {{ storeId }}</p><h1>{{ store?.store_name || '店铺详情' }}</h1><p class="muted">当前页面的全部关联商品和配置都由服务端再次校验店铺 Scope。</p></div><div class="actions"><RouterLink :to="`/stores/${storeId}`" target="_blank">查看公开店铺</RouterLink><RouterLink to="/admin/stores">返回列表</RouterLink></div></header><p v-if="message" class="alert success" aria-live="polite">{{ message }}</p><PageState :loading="loading" :error="error" :empty="!loading && !store" empty-title="店铺不存在" @retry="load"><template v-if="store"><nav class="tab-list" aria-label="店铺运营分区"><button v-for="entry in ([['profile','店铺资料'],['groups','商品分组'],['shipping','配送模板'],['announcements','店铺公告'],['featured','推荐位']] as const)" :key="entry[0]" type="button" :class="{ active: section === entry[0] }" @click="section = entry[0]">{{ entry[1] }}</button><RouterLink :to="`/admin/stores/${storeId}/policies`">服务政策</RouterLink></nav>
      <div v-if="section === 'profile'" class="admin-detail-grid"><form class="card admin-editor" @submit.prevent="saveProfile"><h2>基础资料</h2><label>店铺名称<input v-model.trim="profile.store_name" minlength="2" maxlength="128" required /></label><label>店铺简介<textarea v-model.trim="profile.description" maxlength="2000" /></label><AdminFileUpload purpose="store_logo" :business-context-id="storeId" label="上传店铺 Logo" @uploaded="profile.logo_file_id = $event" /><p v-if="profile.logo_file_id" class="muted">待绑定文件：{{ profile.logo_file_id }}</p><button :disabled="saving || !auth.has('stores:manage')">保存资料</button><div class="actions"><RouterLink :to="{ path: '/admin/products', query: { store_id: storeId } }">管理该店商品</RouterLink><RouterLink :to="{ path: '/admin/orders', query: { store_id: storeId } }">查看该店订单</RouterLink></div></form><form class="card admin-editor" @submit.prevent="changeStatus"><h2>状态命令</h2><p>当前：<span class="badge">{{ store.status }}</span> · 版本 v{{ store.version }}</p><div class="alert info">暂停或关闭会影响新商品购买与公开展示；历史订单和售后事实不删除。</div><label>动作<select v-model="statusForm.action"><option value="activate">开通</option><option value="suspend">暂停</option><option value="resume">恢复</option><option value="close">关闭</option></select></label><label>原因码<input v-model.trim="statusForm.reason_code" required pattern="[A-Z][A-Z0-9_]{1,63}" /></label><label>原因<textarea v-model.trim="statusForm.reason" required minlength="2" maxlength="500" /></label><button class="danger" :disabled="saving || !auth.has('stores:manage')">执行状态命令</button><hr /><h3>注销店铺与商家账号</h3><p class="muted">从未产生交易时可以物理删除；有交易历史时系统会阻止，只能关闭店铺。</p><button type="button" class="danger" :disabled="saving || !auth.has('stores:manage')" @click="deleteOpen = true">删除店铺</button></form></div>
      <div v-else-if="section === 'groups'" class="admin-split"><div class="card-list"><article v-for="item in groups" :key="item.group_id" class="card"><h2>{{ item.group_name }}</h2><p><span class="badge">{{ item.status }}</span> · {{ item.product_ids.length }} 件商品 · 排序 {{ item.sort_order }}</p><button type="button" class="secondary small" @click="chooseGroup(item)">编辑完整集合</button></article></div><form class="card admin-editor" @submit.prevent="saveGroup"><h2>{{ selectedGroup ? '编辑商品分组' : '新建商品分组' }}</h2><label>分组名称<input v-model.trim="groupForm.group_name" required maxlength="64" /></label><label>父分组<select v-model="groupForm.parent_group_id"><option value="">无</option><option v-for="item in groups.filter((value) => value.group_id !== selectedGroup?.group_id)" :key="item.group_id" :value="item.group_id">{{ item.group_name }}</option></select></label><label>排序<input v-model.number="groupForm.sort_order" type="number" min="0" max="100000" /></label><label>商品公开 ID（每行一个）<textarea v-model.trim="groupForm.product_ids" rows="8" placeholder="prd_…" /></label><p class="muted">保存时完整替换分组商品集合，服务端拒绝跨店商品。</p><div class="actions"><button :disabled="saving">保存分组</button><button v-if="selectedGroup" type="button" class="secondary" @click="resetGroup">取消</button></div></form></div>
      <div v-else-if="section === 'shipping'" class="admin-split"><div class="card-list"><article v-for="item in shipping" :key="item.template_id" class="card"><div class="card-heading"><h2>{{ item.template_name }}</h2><span class="badge">{{ item.status }}</span></div><p>{{ item.delivery_type }} · {{ item.charge_mode }} · 发货 {{ item.dispatch_min_hours }}–{{ item.dispatch_max_hours }} 小时</p><p class="muted">版本 {{ item.policy_version }} · {{ item.rules.length }} 条区域规则</p><div class="actions"><button type="button" class="secondary small" :disabled="item.status !== 'draft'" @click="editShipping(item)">编辑草稿</button><button type="button" class="small" :disabled="item.status !== 'draft' || shippingForm.reason.trim().length < 2 || saving" @click="publishShipping(item)">发布此版本</button></div></article></div><form class="card admin-editor" @submit.prevent="saveShipping"><h2>{{ shippingEditing ? '编辑配送模板草稿' : '新建配送模板草稿' }}</h2><label>模板名称<input v-model.trim="shippingForm.template_name" required maxlength="128" /></label><div class="field-grid"><label>配送类型<select v-model="shippingForm.delivery_type"><option value="express">快递</option><option value="same_day">当日达</option><option value="self_pickup">自提</option></select></label><label>计费方式<select v-model="shippingForm.charge_mode"><option value="fixed">固定</option><option value="by_item">按件</option><option value="by_weight">按重量</option></select></label><label>最短发货小时<input v-model.number="shippingForm.dispatch_min_hours" type="number" min="0" max="8760" /></label><label>最长发货小时<input v-model.number="shippingForm.dispatch_max_hours" type="number" min="0" max="8760" /></label></div><label>区域范围 JSON<textarea v-model="shippingForm.region_scope" required /></label><div class="field-grid"><label>首单位<input v-model.number="shippingForm.first_unit" type="number" min="1" /></label><label>续单位<input v-model.number="shippingForm.additional_unit" type="number" min="1" /></label><label>首费（分）<input v-model.number="shippingForm.first_fee_amount" type="number" min="0" /></label><label>续费（分）<input v-model.number="shippingForm.additional_fee_amount" type="number" min="0" /></label><label>预计最短天<input v-model.number="shippingForm.estimated_min_days" type="number" min="0" /></label><label>预计最长天<input v-model.number="shippingForm.estimated_max_days" type="number" min="0" /></label></div><label>发布原因<textarea v-model.trim="shippingForm.reason" maxlength="500" placeholder="发布按钮使用，至少 2 个字" /></label><div class="actions"><button :disabled="saving">保存草稿</button><button v-if="shippingEditing" type="button" class="secondary" @click="resetShipping">取消</button></div></form></div>
      <div v-else-if="section === 'announcements'" class="admin-split"><div class="card-list"><article v-for="item in announcements" :key="item.announcement_id" class="card"><div class="card-heading"><h2>{{ item.title }}</h2><span class="badge">{{ item.status }}</span></div><p>{{ item.content }}</p><p class="muted">{{ item.starts_at || '立即' }} 至 {{ item.ends_at || '长期' }}</p><button type="button" class="secondary small" @click="editAnnouncement(item)">编辑</button></article></div><form class="card admin-editor" @submit.prevent="saveAnnouncement"><h2>{{ announcementEditing ? '编辑公告' : '新建公告' }}</h2><label>标题<input v-model.trim="announcementForm.title" required maxlength="128" /></label><label>内容<textarea v-model.trim="announcementForm.content" required maxlength="2000" /></label><label>状态<select v-model="announcementForm.status"><option value="draft">草稿</option><option value="published">发布</option><option value="disabled">停用</option></select></label><div class="field-grid"><label>开始<input v-model="announcementForm.starts_at" type="datetime-local" /></label><label>结束<input v-model="announcementForm.ends_at" type="datetime-local" /></label></div><label>排序<input v-model.number="announcementForm.sort_order" type="number" min="0" /></label><div class="actions"><button :disabled="saving">保存公告</button><button v-if="announcementEditing" type="button" class="secondary" @click="resetAnnouncement">取消</button></div></form></div>
      <div v-else class="admin-split"><div class="card-list"><article v-for="item in featured" :key="`${item.slot_type}-${item.product_id}`" class="card"><strong>{{ item.product_id }}</strong><p>{{ item.slot_type }} · 排序 {{ item.sort_order }}</p></article><p v-if="!featured.length" class="empty-state">当前推荐位为空</p></div><form class="card admin-editor" @submit.prevent="replaceFeatured"><h2>完整替换推荐位</h2><label>推荐位<select v-model="featuredForm.slot_type" @change="loadFeatured"><option value="recommended">推荐</option><option value="hot">热门</option></select></label><label>商品公开 ID（每行一个，最多 12 个）<textarea v-model.trim="featuredForm.product_ids" rows="10" placeholder="prd_…" /></label><p class="alert info">提交的是目标完整集合，不做增量追加；跨店或不可见商品会被拒绝。</p><button :disabled="saving">确认替换</button></form></div>
    </template></PageState><div v-if="deleteOpen" class="admin-form-overlay" @click.self="deleteOpen = false"><form class="admin-form-dialog admin-delete-dialog" @submit.prevent="deleteStore"><header><div><p class="eyebrow">DANGEROUS ACTION</p><h2>删除店铺与商家账号</h2><p>操作不可恢复；如存在交易，后端会阻止删除。</p></div><button type="button" @click="deleteOpen = false">×</button></header><div class="admin-form-fields"><label class="wide">删除原因<textarea v-model.trim="deleteReason" required minlength="2" maxlength="500" /></label><label class="wide">输入 DELETE_STORE 确认<input v-model.trim="deleteConfirmation" required /></label></div><footer><button type="button" class="secondary" @click="deleteOpen = false">取消</button><button class="danger" :disabled="saving || deleteConfirmation !== 'DELETE_STORE'">永久删除</button></footer></form></div></section>
</template>
