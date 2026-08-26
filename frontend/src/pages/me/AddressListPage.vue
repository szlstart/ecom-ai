<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import areaData from 'china-area-data'

import { apiRequest, createIdempotencyKey, errorMessage } from '@/api/http'
import { useUserAuthStore } from '@/stores/user-auth'

interface Address {
  address_id: string
  recipient_name: string
  phone: string
  country_code: string
  province_code: string
  city_code: string
  district_code: string
  address: string
  is_default: boolean
  version: number
}

interface RegionOption { code: string; name: string }

const excludedProvinceCodes = new Set(['810000', '820000'])
const provinces = Object.entries(areaData['86'] ?? {})
  .filter(([code]) => !excludedProvinceCodes.has(code))
  .map(([code, name]) => ({ code, name }))

const auth = useUserAuthStore()
const items = ref<Address[]>([])
const error = ref('')
const showForm = ref(false)
const pending = ref(false)
const editingAddressId = ref<string | null>(null)
const editingVersion = ref<number | null>(null)
const isEditing = computed(() => editingAddressId.value !== null)
const form = reactive({
  recipient_name: '',
  phone: '',
  country_code: 'CN',
  province_code: '',
  city_code: '',
  district_code: '',
  address: '',
  is_default: false,
})

const cities = computed(() => optionsFor(form.province_code))
const districts = computed(() => optionsFor(form.city_code))

function optionsFor(parentCode: string): RegionOption[] {
  return Object.entries(areaData[parentCode] ?? {}).map(([code, name]) => ({ code, name }))
}

function selectProvince() {
  form.city_code = ''
  form.district_code = ''
}

function selectCity() {
  form.district_code = ''
}

function regionName(code: string): string {
  for (const regions of Object.values(areaData)) {
    if (regions[code]) return regions[code]
  }
  return code
}

function displayRegion(item: Address): string {
  return [regionName(item.province_code), regionName(item.city_code), regionName(item.district_code)]
    .filter((name, index, values) => name !== '市辖区' && values.indexOf(name) === index)
    .join(' ')
}

function resetForm() {
  Object.assign(form, {
    recipient_name: '',
    phone: '',
    country_code: 'CN',
    province_code: '',
    city_code: '',
    district_code: '',
    address: '',
    is_default: false,
  })
}

function openCreateForm() {
  resetForm()
  editingAddressId.value = null
  editingVersion.value = null
  error.value = ''
  showForm.value = true
}

function openEditForm(item: Address) {
  Object.assign(form, {
    recipient_name: item.recipient_name,
    phone: item.phone,
    country_code: item.country_code,
    province_code: item.province_code,
    city_code: item.city_code,
    district_code: item.district_code,
    address: item.address,
    is_default: item.is_default,
  })
  editingAddressId.value = item.address_id
  editingVersion.value = item.version
  error.value = ''
  showForm.value = true
}

function cancelForm() {
  showForm.value = false
  editingAddressId.value = null
  editingVersion.value = null
  error.value = ''
  resetForm()
}

async function load() {
  items.value = (await apiRequest<{ items: Address[] }>('/users/me/addresses', {}, auth.accessToken)).data.items
}

async function save() {
  pending.value = true
  error.value = ''
  try {
    if (editingAddressId.value && editingVersion.value !== null) {
      await apiRequest(`/users/me/addresses/${editingAddressId.value}`, {
        method: 'PATCH',
        headers: { 'If-Match': `"v${editingVersion.value}"` },
        body: JSON.stringify({
          recipient_name: form.recipient_name,
          phone: form.phone,
          country_code: form.country_code,
          province_code: form.province_code,
          city_code: form.city_code,
          district_code: form.district_code,
          address: form.address,
        }),
      }, auth.accessToken)
    } else {
      await apiRequest('/users/me/addresses', {
        method: 'POST',
        headers: { 'Idempotency-Key': createIdempotencyKey('address') },
        body: JSON.stringify({ ...form, postal_code: null, label: null }),
      }, auth.accessToken)
    }
    cancelForm()
    await load()
  } catch (reason) {
    error.value = errorMessage(reason)
  } finally {
    pending.value = false
  }
}

async function remove(item: Address) {
  if (!confirm('确定删除这个地址吗？')) return
  try {
    await apiRequest(`/users/me/addresses/${item.address_id}`, {
      method: 'DELETE',
      headers: { 'If-Match': `"v${item.version}"` },
    }, auth.accessToken)
    await load()
  } catch (reason) {
    error.value = errorMessage(reason)
  }
}

async function setDefault(item: Address) {
  try {
    await apiRequest('/users/me/default-address', {
      method: 'PUT',
      body: JSON.stringify({ address_id: item.address_id }),
    }, auth.accessToken)
    await load()
  } catch (reason) {
    error.value = errorMessage(reason)
  }
}

onMounted(() => load().catch((reason) => { error.value = errorMessage(reason) }))
</script>

<template>
  <section>
    <div class="page-heading">
      <div><p class="eyebrow">我的</p><h1>收货地址</h1></div>
      <button type="button" @click="showForm ? cancelForm() : openCreateForm()">{{ showForm ? '取消' : '新增地址' }}</button>
    </div>
    <p v-if="error" class="alert error">{{ error }}</p>

    <form v-if="showForm" class="card address-form" @submit.prevent="save">
      <h2>{{ isEditing ? '编辑收货地址' : '新增收货地址' }}</h2>
      <label>收货人<input v-model.trim="form.recipient_name" autocomplete="name" minlength="1" maxlength="64" required /></label>
      <label>联系电话<input v-model="form.phone" autocomplete="tel" required /></label>
      <fieldset class="region-selector">
        <legend>地区</legend>
        <div class="field-row">
          <label>省份<select v-model="form.province_code" required @change="selectProvince"><option value="" disabled>请选择省份</option><option v-for="item in provinces" :key="item.code" :value="item.code">{{ item.name }}</option></select></label>
          <label>城市<select v-model="form.city_code" required :disabled="!form.province_code" @change="selectCity"><option value="" disabled>请选择城市</option><option v-for="item in cities" :key="item.code" :value="item.code">{{ item.name }}</option></select></label>
          <label>区 / 县<select v-model="form.district_code" required :disabled="!form.city_code"><option value="" disabled>请选择区或县</option><option v-for="item in districts" :key="item.code" :value="item.code">{{ item.name }}</option></select></label>
        </div>
      </fieldset>
      <label>详细地址<textarea v-model="form.address" autocomplete="street-address" maxlength="500" placeholder="请输入街道、门牌号、小区、楼栋及房间号" required /></label>
      <label v-if="!isEditing" class="check-row"><input v-model="form.is_default" type="checkbox" />设为默认地址</label>
      <div class="actions form-actions">
        <button :disabled="pending" type="submit">{{ pending ? '正在保存…' : isEditing ? '保存修改' : '保存地址' }}</button>
        <button class="secondary" :disabled="pending" type="button" @click="cancelForm">取消</button>
      </div>
    </form>

    <div class="stack">
      <article v-for="item in items" :key="item.address_id" class="card address-card">
        <div><span v-if="item.is_default" class="badge">默认</span><strong>{{ item.recipient_name }}</strong> · {{ item.phone }}<p>{{ displayRegion(item) }} {{ item.address }}</p></div>
        <div class="actions"><button class="secondary small" type="button" @click="openEditForm(item)">编辑</button><button v-if="!item.is_default" class="secondary small" type="button" @click="setDefault(item)">设为默认</button><button class="danger small" type="button" @click="remove(item)">删除</button></div>
      </article>
      <p v-if="!items.length" class="empty-state card">暂无收货地址</p>
    </div>
  </section>
</template>
