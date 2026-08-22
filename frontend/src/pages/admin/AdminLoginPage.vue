<template>
  <form aria-labelledby="admin-login-title" @submit.prevent>
    <p class="eyebrow">独立 Admin Audience</p>
    <h1 id="admin-login-title">管理端登录</h1>
    <label>管理员账号<input autocomplete="username" disabled /></label>
    <label>密码<input autocomplete="current-password" disabled type="password" /></label>
    <button disabled type="submit">登录</button>
  </form>
</template>

