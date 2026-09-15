<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import { adminGet, adminQuery, type AdminProductSummary, type AdminSku, type AdminStore } from '@/api/admin-catalog'
import { listAdminUsers, type AdminUserSummary } from '@/api/admin-users'
import { errorMessage, resolveApiAssetUrl } from '@/api/http'
import type { AgentAssetMessageInput } from '@/api/messaging'
import AdminFileUpload from '@/components/AdminFileUpload.vue'

const props = withDefaults(defineProps<{
  open: boolean
  audience: 'merchant' | 'admin'
  accessToken: string
  merchantStoreId?: string | null
  merchantStoreName?: string | null
  busy?: boolean
}>(), { merchantStoreId: null, merchantStoreName: null, busy: false })
const emit = defineEmits<{
  close: []
  submit: [input: AgentAssetMessageInput]
}>()

const upload = ref<InstanceType<typeof AdminFileUpload> | null>(null)
const dialog = ref<HTMLElement | null>(null)
const purpose = ref<'store_logo' | 'product_sku_image' | 'user_avatar'>('store_logo')
const stores = ref<AdminStore[]>([])
const users = ref<AdminUserSummary[]>([])
const products = ref<AdminProductSummary[]>([])
const skus = ref<AdminSku[]>([])
const storeId = ref('')
const productId = ref('')
const skuId = ref('')
const userId = ref('')
const fileId = ref('')
const loading = ref(false)
const uploadBusy = ref(false)
const pasteBusy = ref(false)
const error = ref('')

const selectedStore = computed(() => stores.value.find((item) => item.store_id === storeId.value) ?? null)
const selectedProduct = computed(() => products.value.find((item) => item.product_id === productId.value) ?? null)
const selectedSku = computed(() => skus.value.find((item) => item.sku_id === skuId.value) ?? null)
const selectedUser = computed(() => users.value.find((item) => item.user_id === userId.value) ?? null)
const uploadPurpose = computed(() => purpose.value === 'store_logo' ? 'store_logo' : purpose.value === 'user_avatar' ? 'user_avatar' : 'product')
const uploadBusinessContextId = computed(() => purpose.value === 'user_avatar' ? userId.value : storeId.value)
const previewUrl = computed(() => fileId.value ? resolveApiAssetUrl(`/api/v1/files/${fileId.value}`) : null)
const canUpload = computed(() => Boolean(
  purpose.value === 'user_avatar'
    ? userId.value
    : storeId.value && (purpose.value === 'store_logo' || skuId.value),
))
const canSubmit = computed(() => Boolean(
  fileId.value
  && (purpose.value === 'user_avatar'
    ? userId.value
    : storeId.value && (purpose.value === 'store_logo' || (productId.value && skuId.value)))
  && !props.busy
  && !uploadBusy.value
  && !pasteBusy.value,
))

async function initialize() {
  purpose.value = 'store_logo'
  products.value = []
  skus.value = []
  productId.value = ''
  skuId.value = ''
  userId.value = ''
  fileId.value = ''
  error.value = ''
  loading.value = true
  try {
    if (props.audience === 'merchant' && props.merchantStoreId) {
      const store = (await adminGet<AdminStore>(`/admin/stores/${encodeURIComponent(props.merchantStoreId)}`, props.accessToken)).data
      stores.value = [store]
      storeId.value = store.store_id
    } else {
      const [storeResult, userResult] = await Promise.all([
        adminGet<{ items: AdminStore[] }>(`/admin/stores${adminQuery({ limit: 100 })}`, props.accessToken),
        listAdminUsers(props.accessToken),
      ])
      stores.value = storeResult.data.items
      users.value = userResult.data.items
      storeId.value = stores.value[0]?.store_id ?? ''
      userId.value = users.value[0]?.user_id ?? ''
    }
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
    await nextTick()
    dialog.value?.focus({ preventScroll: true })
  }
}

async function loadProducts() {
  products.value = []
  skus.value = []
  productId.value = ''
  skuId.value = ''
  fileId.value = ''
  if (!storeId.value || purpose.value !== 'product_sku_image') return
  loading.value = true
  error.value = ''
  try {
    const result = await adminGet<{ items: AdminProductSummary[] }>(
      `/admin/products${adminQuery({ store_id: storeId.value, limit: 100 })}`,
      props.accessToken,
    )
    products.value = result.data.items.filter((item) => ['draft', 'rejected', 'off_shelf', 'on_sale'].includes(item.status))
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function loadSkus() {
  skus.value = []
  skuId.value = ''
  fileId.value = ''
  if (!productId.value) return
  loading.value = true
  error.value = ''
  try {
    const result = await adminGet<AdminSku[]>(
      `/admin/products/${encodeURIComponent(productId.value)}/skus`,
      props.accessToken,
    )
    skus.value = result.data.filter((item) => item.status === 'active')
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

function uploaded(value: string) {
  fileId.value = value
  error.value = ''
}

async function pasteImage(event: ClipboardEvent) {
  if (!canUpload.value || uploadBusy.value || pasteBusy.value) return
  const file = Array.from(event.clipboardData?.items ?? [])
    .find((item) => item.kind === 'file' && item.type.startsWith('image/'))
    ?.getAsFile()
  if (!file) {
    error.value = '剪贴板中没有可用图片。请先复制图片，再在此区域粘贴。'
    return
  }
  event.preventDefault()
  pasteBusy.value = true
  error.value = ''
  try { await upload.value?.uploadFile(file) }
  catch (cause) { error.value = errorMessage(cause) }
  finally { pasteBusy.value = false }
}

function submit() {
  if (!canSubmit.value) return
  emit('submit', {
    purpose: purpose.value,
    file_id: fileId.value,
    store_id: purpose.value === 'user_avatar' ? null : storeId.value,
    user_id: purpose.value === 'user_avatar' ? userId.value : null,
    product_id: purpose.value === 'product_sku_image' ? productId.value : null,
    sku_id: purpose.value === 'product_sku_image' ? skuId.value : null,
  })
}

watch(() => props.open, (open) => { if (open) void initialize() }, { immediate: true })
watch(purpose, () => { fileId.value = ''; void loadProducts() })
watch(storeId, () => { if (purpose.value === 'product_sku_image') void loadProducts(); else fileId.value = '' })
watch(productId, () => { void loadSkus() })
watch(skuId, () => { fileId.value = '' })
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="agent-asset-overlay" @click.self="emit('close')">
      <section ref="dialog" class="agent-asset-dialog" role="dialog" aria-modal="true" aria-label="给 Agent 一张图片" tabindex="-1" @keydown.esc="emit('close')">
        <header>
          <div><small>受控图片操作</small><h2>给 {{ audience === 'merchant' ? 'AI 经营助理' : 'AI 管家' }} 一张图片</h2><p>先选择用途和业务对象。上传只进入安全暂存区，Agent 核对后还会展示确认卡，不会立即覆盖线上图片。</p></div>
          <button type="button" aria-label="关闭" @click="emit('close')">×</button>
        </header>

        <div class="agent-asset-body">
          <nav aria-label="图片用途">
            <button type="button" :class="{ active: purpose === 'store_logo' }" @click="purpose = 'store_logo'"><span>店</span><strong>更新店铺 Logo</strong><small>店铺页、商品卡片与消息头像</small></button>
            <button type="button" :class="{ active: purpose === 'product_sku_image' }" @click="purpose = 'product_sku_image'"><span>款</span><strong>替换款式图片</strong><small>只修改所选商品的所选款式</small></button>
            <button v-if="audience === 'admin'" type="button" :class="{ active: purpose === 'user_avatar' }" @click="purpose = 'user_avatar'"><span>人</span><strong>更新用户头像</strong><small>用户端导航、个人中心与消息头像</small></button>
          </nav>

          <div class="agent-asset-fields">
            <label v-if="purpose === 'user_avatar'">目标用户<select v-model="userId" :disabled="loading"><option v-for="item in users" :key="item.user_id" :value="item.user_id">{{ item.username }}</option></select></label>
            <label v-else-if="audience === 'admin'">目标店铺<select v-model="storeId" :disabled="loading"><option v-for="item in stores" :key="item.store_id" :value="item.store_id">{{ item.store_name }}</option></select></label>
            <label v-else>当前店铺<input :value="selectedStore?.store_name || merchantStoreName || '正在读取店铺…'" disabled /></label>
            <template v-if="purpose === 'product_sku_image'">
              <label>目标商品<select v-model="productId" :disabled="loading || !storeId"><option value="" disabled>请选择商品</option><option v-for="item in products" :key="item.product_id" :value="item.product_id">{{ item.product_name }} · {{ item.status === 'on_sale' ? '销售中' : item.status === 'draft' ? '草稿' : item.status === 'off_shelf' ? '已下架' : '需修改' }}</option></select></label>
              <label>目标款式<select v-model="skuId" :disabled="loading || !productId"><option value="" disabled>请选择款式</option><option v-for="item in skus" :key="item.sku_id" :value="item.sku_id">{{ item.sku_name }} · ¥{{ item.sale_price }}</option></select></label>
            </template>
          </div>

          <div v-if="canUpload" class="agent-asset-upload" tabindex="0" @paste="pasteImage">
            <div class="agent-asset-preview"><img v-if="previewUrl" :src="previewUrl" alt="本次待核对图片" /><span v-else>＋</span></div>
            <div><strong>{{ fileId ? '图片已经通过安全扫描' : '选择或粘贴图片' }}</strong><p>点击下方选择本地图片，或聚焦此区域后按 Command + V / Ctrl + V。上传完成后仍需提交给 Agent 并核对确认卡。</p><AdminFileUpload ref="upload" :purpose="uploadPurpose" :business-context-id="uploadBusinessContextId" :access-token="accessToken" label="从本地选择图片" :disabled="busy" @uploaded="uploaded" @busy-changed="uploadBusy = $event" /></div>
          </div>
          <div v-else class="agent-asset-empty">请先选择完整的{{ purpose === 'user_avatar' ? '用户' : purpose === 'store_logo' ? '店铺' : '商品和款式' }}，再上传图片。</div>
          <p v-if="selectedProduct && selectedSku" class="agent-asset-target">将交给 Agent 核对：{{ selectedStore?.store_name }} / {{ selectedProduct.product_name }} / {{ selectedSku.sku_name }}</p>
          <p v-else-if="purpose === 'user_avatar' && selectedUser" class="agent-asset-target">将交给 Agent 核对：用户 {{ selectedUser.username }} 的公开头像</p>
          <p v-if="error" class="alert error" role="alert">{{ error }}</p>
        </div>

        <footer><button type="button" class="secondary" :disabled="busy" @click="emit('close')">取消</button><button type="button" :disabled="!canSubmit" @click="submit">{{ busy ? '正在发送…' : '交给 Agent 核对' }}</button></footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.agent-asset-overlay{position:fixed;z-index:1700;inset:0;padding:24px;display:grid;place-items:center;background:rgb(9 17 31 / 64%);backdrop-filter:blur(8px)}
.agent-asset-dialog{width:min(880px,calc(100vw - 32px));max-height:calc(100vh - 40px);overflow:hidden;display:grid;grid-template-rows:auto minmax(0,1fr) auto;border:1px solid rgb(255 255 255 / 70%);border-radius:24px;outline:0;background:#f7f9fc;box-shadow:0 34px 110px rgb(4 12 27 / 45%)}
.agent-asset-dialog>header{padding:20px 24px;display:flex;align-items:flex-start;justify-content:space-between;gap:20px;border-bottom:1px solid #e2e7ef;background:#fff}.agent-asset-dialog>header div{display:grid;gap:5px}.agent-asset-dialog>header small{color:#3158d8;font-size:.7rem;font-weight:850;letter-spacing:.1em}.agent-asset-dialog h2,.agent-asset-dialog p{margin:0}.agent-asset-dialog h2{font-size:1.2rem}.agent-asset-dialog p{color:#667286;font-size:.76rem;line-height:1.6}.agent-asset-dialog>header>button{width:38px;height:38px;padding:0;color:#596576;border:1px solid #dce2eb;border-radius:11px;background:#fff;font-size:1.4rem}
.agent-asset-body{padding:22px;overflow-y:auto;display:grid;gap:18px}.agent-asset-body>nav{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.agent-asset-body>nav button{padding:14px;display:grid;grid-template-columns:42px minmax(0,1fr);grid-template-rows:auto auto;column-gap:11px;text-align:left;color:#253246;border:1px solid #dfe5ee;border-radius:15px;background:#fff}.agent-asset-body>nav button>span{grid-row:1/3;width:42px;height:42px;display:grid;place-items:center;color:#fff;border-radius:12px;background:#7b8799;font-weight:900}.agent-asset-body>nav button>strong{align-self:end;font-size:.85rem}.agent-asset-body>nav button>small{color:#7b8696;font-size:.66rem}.agent-asset-body>nav button.active{border-color:#3158d8;box-shadow:0 0 0 3px rgb(49 88 216 / 12%)}.agent-asset-body>nav button.active>span{background:linear-gradient(145deg,#3158d8,#6e8ff2)}
.agent-asset-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.agent-asset-fields label{display:grid;gap:6px;color:#4d596b;font-size:.72rem;font-weight:750}.agent-asset-fields select,.agent-asset-fields input{min-height:44px;padding:9px 11px;border:1px solid #dce2eb;border-radius:10px;background:#fff;color:#273447}
.agent-asset-upload{padding:16px;display:grid;grid-template-columns:150px minmax(0,1fr);align-items:center;gap:18px;border:2px dashed #cbd4e3;border-radius:18px;background:#fff}.agent-asset-upload:focus{border-color:#3158d8;outline:3px solid rgb(49 88 216 / 11%)}.agent-asset-preview{width:150px;height:150px;display:grid;place-items:center;overflow:hidden;border-radius:15px;background:linear-gradient(145deg,#eef2f7,#e0e7f0)}.agent-asset-preview img{width:100%;height:100%;object-fit:cover}.agent-asset-preview span{color:#8290a4;font-size:2.3rem}.agent-asset-upload>div:last-child{display:grid;gap:7px}.agent-asset-upload strong{font-size:.9rem}.agent-asset-upload p{font-size:.7rem}.agent-asset-empty{padding:34px;text-align:center;color:#748095;border:2px dashed #d9e0ea;border-radius:16px;background:#fff}.agent-asset-target{padding:10px 12px;color:#3158d8!important;border-radius:10px;background:#eef3ff}.agent-asset-dialog>footer{padding:14px 22px;display:flex;justify-content:flex-end;gap:10px;border-top:1px solid #e2e7ef;background:#fff}.agent-asset-dialog>footer button{min-height:40px;padding:8px 18px}.agent-asset-dialog>footer button:disabled{opacity:.5}
@media(max-width:680px){.agent-asset-overlay{padding:8px}.agent-asset-dialog{width:100%;max-height:calc(100vh - 16px);border-radius:18px}.agent-asset-body{padding:15px}.agent-asset-body>nav,.agent-asset-fields{grid-template-columns:1fr}.agent-asset-upload{grid-template-columns:1fr}.agent-asset-preview{width:100%;height:210px}}
</style>
