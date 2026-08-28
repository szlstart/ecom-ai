import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import { useUserAuthStore } from './stores/user-auth'
import './styles/base.css'

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)

// Restore the shopper session before the router performs its initial protected
// route check. Without this ordering, directly opening /me or /cart could briefly
// redirect to the login dialog even though the refresh cookie is still valid.
const initialPath = window.location.pathname
const isManagementPortal = initialPath === '/merchant'
  || initialPath.startsWith('/merchant/')
  || initialPath === '/admin'
  || initialPath.startsWith('/admin/')
const userAuth = useUserAuthStore(pinia)
if (!isManagementPortal && userAuth.csrfToken) await userAuth.refresh()

app.use(router).mount('#app')
