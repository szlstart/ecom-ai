<template>
  <main class="system-state" aria-labelledby="forbidden-title">
    <p class="eyebrow">403</p>
    <h1 id="forbidden-title">你没有访问此内容的权限</h1>
    <p>请确认当前登录身份，或返回有权限访问的页面。为保护数据安全，这里不会展示目标资源是否存在。</p>
    <nav class="actions" aria-label="恢复操作">
      <RouterLink class="button-link" to="/">返回首页</RouterLink>
      <button class="secondary" type="button" @click="$router.back()">返回上一页</button>
    </nav>
  </main>
</template>
