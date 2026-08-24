<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { listAdminAppeals } from '@/api/admin-after-sales'
import type { RefundAppeal } from '@/api/after-sales'
import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore(), items = ref<RefundAppeal[]>([]), error = ref('')
onMounted(async () => { try { items.value = (await listAdminAppeals(auth.accessToken!)).data.items } catch (cause) { error.value = errorMessage(cause) } })
</script>
<template><section><p class="eyebrow">平台复核</p><h1>售后申诉</h1><p v-if="error" class="alert error">{{ error }}</p><div class="stack"><RouterLink v-for="item in items" :key="item.appeal_id" class="card" :to="`/admin/refund-appeals/${item.appeal_id}`"><strong>{{ item.appeal_id }}</strong><span class="badge">{{ item.appeal_status }}</span><p>{{ item.reason }}</p></RouterLink><p v-if="!items.length && !error">暂无申诉。</p></div></section></template>
