<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <form aria-labelledby="register-title" @submit.prevent>
    <p class="eyebrow">用户端</p>
    <h1 id="register-title">注册账号</h1>
    <label>用户名<input autocomplete="username" disabled placeholder="后续阶段接入" /></label>
    <label>手机号或邮箱<input autocomplete="email" disabled /></label>
    <label>验证码<input autocomplete="one-time-code" disabled inputmode="numeric" /></label>
    <label>密码<input autocomplete="new-password" disabled type="password" /></label>
    <label>确认密码<input autocomplete="new-password" disabled type="password" /></label>
    <button disabled type="submit">同意协议并注册</button>
    <p class="form-help">已有账号？<RouterLink to="/login">返回登录</RouterLink></p>
  </form>
</template>
