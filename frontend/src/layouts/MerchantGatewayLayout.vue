<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import MerchantAuthLayout from '@/layouts/MerchantAuthLayout.vue'
import MerchantLayout from '@/layouts/MerchantLayout.vue'

const route = useRoute()
const usesAuthLayout = computed(() => route.meta.layout === 'merchant-auth')
</script>

<template>
  <MerchantAuthLayout v-if="usesAuthLayout" />
  <MerchantLayout v-else />
</template>
