<template>
  <main class="system-state" aria-labelledby="gone-title">
    <p class="eyebrow">410</p>
    <h1 id="gone-title">内容已失效或被撤回</h1>
    <p>该链接不再可用。你可以返回首页，或从原列表重新查找最新内容。</p>
    <nav class="actions" aria-label="恢复操作">
      <RouterLink class="button-link" to="/">返回首页</RouterLink>
      <button class="secondary" type="button" @click="$router.back()">返回原列表</button>
    </nav>
  </main>
</template>
