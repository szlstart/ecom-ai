import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import { useAdminAuthStore } from './stores/admin-auth'
import { useUserAuthStore } from './stores/user-auth'
import './styles/base.css'

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)

// Restore the shopper session before the router performs its initial protected
// route check. Without this ordering, directly opening /me or /cart could briefly
// redirect to the login dialog even though the refresh cookie is still valid.
const initialPath = window.location.pathname
const managementPortal = initialPath === '/merchant' || initialPath.startsWith('/merchant/')
  ? 'merchant'
  : initialPath === '/admin' || initialPath.startsWith('/admin/') ? 'admin' : null
const userAuth = useUserAuthStore(pinia)
const managementAuth = useAdminAuthStore(pinia)
if (managementPortal && managementAuth.hasRefreshHint(managementPortal)) {
  await managementAuth.refresh(managementPortal)
} else if (!managementPortal && userAuth.csrfToken) {
  await userAuth.refresh()
}

app.use(router).mount('#app')
