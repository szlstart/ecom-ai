<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <form aria-labelledby="login-title" @submit.prevent>
    <p class="eyebrow">用户端</p>
    <h1 id="login-title">登录</h1>
    <label>账号<input autocomplete="username" disabled placeholder="后续阶段接入" /></label>
    <label>密码<input autocomplete="current-password" disabled type="password" /></label>
    <button disabled type="submit">登录</button>
    <p class="form-help">还没有账号？<RouterLink to="/register">注册账号</RouterLink></p>
  </form>
</template>
